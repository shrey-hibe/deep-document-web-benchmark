"""export_release: the registry tables a skill needs to find a body, as a folder with the layout of data/tables.

Writes the latest run of entities/{bodies, identifiers, websites, body_records, relations}, standard/records,
standard/code_* and meta/columns (the catalogue rows of the exported tables), plus registry/name_keys (each record's
name key, words, core words and kinds, so a reader needs neither the Python normaliser nor a pass over every name),
and manifest.json (each file's table, run, rows, bytes and sha256). Nothing is published: it writes a local folder.

People data, checked before anything is written and again on the written files:
- WITHHELD columns are left out (EPA's free-text owner name: some name a person).
- REDACTED values: where an agency typed an e-mail address or a phone number into a website field, the value is
  blanked (records, bodies) or the row dropped (websites); the counts go in the manifest.
- Then no exported column may be named like a people column (e-mail, phone, contact, first/last name, officials)
  and no text value may look like an e-mail address or a phone number; otherwise the export stops.
"""

from __future__ import annotations

import csv
import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from .. import catalogue, paths
from ..catalogue import Column
from . import names, people, snapshot
from .resolver import NAME_KEY_COLUMNS, name_rows

TABLES = (("entities", "bodies"), ("entities", "identifiers"), ("entities", "websites"), ("entities", "body_records"),
          ("entities", "relations"), ("standard", "records"))  # plus every standard/code_* table and meta/columns
WITHHELD = {"standard.records": {
    "owner_name": "EPA's free-text name of a water system's owner: some name a person (a property manager), and the "
                  "resolver does not need it"}}
REDACT = {  # table -> {column: other columns blanked on the same rows}, for contact values in website fields
    "standard.records": {"website": ()},
    "entities.bodies": {"website": ("website_source",)},
}
DROP_ROWS = {"entities.websites": ("website", "host")}  # rows whose website is a contact value are left out
NOTES = ("Not published by this command. Sources keep their own terms; websites from SAM.gov claims "
         "(entities.websites source 'claim:sam_entity_public_2026_10') need SAM's terms checked before publishing.",)

NAME_KEYS = catalogue.Table(  # not registered: it lives only in releases, never in data/tables
    dataset="registry", name="name_keys", grain="record, with its name normalised for lookup",
    description="Each record's name key, words, core words and kinds, as ddwb/registry/names.py makes them, so a reader "
                "can look bodies up without the Python normaliser",
    columns=(
        Column("record_id", "the record", "copy", "standard.records.record_id"),
        Column("body_id", "the body the record belongs to", "copy", "entities.body_records.body_id"),
        Column("state", "the record's state", "copy", "standard.records.state"),
        Column("name", "the record's name as published", "copy", "standard.records.name"),
        Column("key", "'NAME|KIND': labels.websites.name_key of the name once EPA's inverted names are put back in order "
               "('CORTEZ CITY OF' -> 'CITY OF CORTEZ') and abbreviations expanded; KIND only for general-purpose names "
               "('SUMMIT|COUNTY', 'CORTEZ|CITY')", "label", "standard.records.name, entity_type"),
        Column("tokens", "the name's normalised words, space-separated", "label", "standard.records.name"),
        Column("core", "the words without designators (CITY, COUNTY, DISTRICT, SCHOOL, LIBRARY, WATER, ...), space-separated",
               "label", "standard.records.name"),
        Column("families", "the kinds of body the record is, comma-separated: county, municipality, township, city, town, "
               "village, borough, school, library, fire, water, sewer, transit, housing, park, special, ...", "label",
               "standard.records.entity_type, function, name"),
        Column("is_anchor", "whether this record names the body", "copy", "entities.body_records.is_anchor"),
        Column("normaliser_version", "the version of the rules in ddwb/registry/names.py", "bookkeeping"),
    ),
    inputs=("standard.records", "entities.body_records"),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _inside(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def _select(key: str, files: list[Path]) -> str:
    """The export's SELECT for a table: withheld columns out, contact values in website fields blanked or dropped."""
    withheld = WITHHELD.get(key, {})
    exclude = f" EXCLUDE ({', '.join(withheld)})" if withheld else ""
    sql = f"SELECT *{exclude} FROM {snapshot.scan(files)}"
    if key in REDACT:
        blanks = []
        for column, also in REDACT[key].items():
            blanks += [f"CASE WHEN {people.sql_contact(column)} THEN NULL ELSE {c} END AS {c}" for c in (column, *also)]
        sql = f"SELECT * REPLACE ({', '.join(blanks)}) FROM ({sql})"
    if key in DROP_ROWS:
        sql = f"SELECT * FROM ({sql}) WHERE NOT ({' OR '.join(people.sql_contact(c) for c in DROP_ROWS[key])})"
    return sql


def _redactions(con: duckdb.DuckDBPyConnection, key: str, files: list[Path]) -> dict:
    out = {}
    for column in REDACT.get(key, {}):
        out[f"{column} blanked"] = con.execute(f"SELECT count(*) FROM {snapshot.scan(files)} WHERE {people.sql_contact(column)}").fetchone()[0]
    if key in DROP_ROWS:
        cond = " OR ".join(people.sql_contact(c) for c in DROP_ROWS[key])
        out["rows dropped"] = con.execute(f"SELECT count(*) FROM {snapshot.scan(files)} WHERE {cond}").fetchone()[0]
    return out


def people_check(con: duckdb.DuckDBPyConnection, select: str) -> dict:
    """People-named columns, and counts of text values shaped like an e-mail address or a phone number."""
    cols = con.execute(f"DESCRIBE {select}").fetchall()
    named = [c for c, *_ in cols if people.COLUMN.search(c)]
    text = [c for c, kind, *_ in cols if kind == "VARCHAR"]
    emails = phones = 0
    hits: list[str] = []
    if text:
        parts = ", ".join(f"count(*) FILTER (WHERE regexp_matches(\"{c}\", '{people.EMAIL}')), "
                          f"count(*) FILTER (WHERE regexp_matches(\"{c}\", '{people.PHONE}'))" for c in text)
        counts = con.execute(f"SELECT {parts} FROM ({select})").fetchone()
        for i, c in enumerate(text):
            e, p = counts[2 * i], counts[2 * i + 1]
            emails, phones = emails + e, phones + p
            if e or p:
                hits.append(f"{c}: {e} e-mail-like, {p} phone-like")
    return {"people_columns": named, "email_values": emails, "phone_values": phones, "hits": hits}


def _failures(check: dict) -> dict:
    return {k: v for k, v in check.items() if v["people_columns"] or v["email_values"] or v["phone_values"]}


def export_release(out_dir: str | Path, *, source: str | Path | None = None) -> dict:
    """Write the release folder and return its manifest. out_dir must be new or empty, and outside the source tables."""
    src = Path(source or paths.TABLES).resolve()
    out = Path(out_dir).resolve()
    for folder in {src, paths.TABLES.resolve()}:
        if _inside(out, folder):
            raise ValueError(f"refusing to write a release inside the tables folder {folder}")
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"{out} is not empty")

    con = duckdb.connect()
    plan, redacted = [], {}  # plan: (dataset, table, run_id, select)
    codes = sorted(p.name for p in (src / "standard").glob("code_*") if p.is_dir())
    for dataset, table in (*TABLES, *(("standard", c) for c in codes)):
        found = snapshot.latest(src, dataset, table)
        if not found:
            raise FileNotFoundError(f"{src}/{dataset}/{table} has no Parquet files")
        run, files = found
        key = f"{dataset}.{table}"
        plan.append((dataset, table, run, _select(key, files)))
        if key in REDACT or key in DROP_ROWS:
            redacted[key] = _redactions(con, key, files)
    exported = {f"{d}.{t}" for d, t, _, _ in plan}

    check = {f"{d}.{t}": people_check(con, select) for d, t, _, select in plan}
    if _failures(check):
        raise ValueError(f"people data found, nothing written: {json.dumps(_failures(check))}")

    out.mkdir(parents=True, exist_ok=True)
    written = []

    def copy(dataset: str, table: str, run: str, select: str) -> Path:
        folder = out / dataset / table
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{run}.parquet"
        con.execute(f"COPY ({select}) TO '{target}' (FORMAT parquet, COMPRESSION zstd)")
        written.append((dataset, table, run, target))
        return target

    for dataset, table, run, select in plan:
        copy(dataset, table, run, select)

    # meta/columns: the catalogue rows of the exported tables (minus withheld columns), plus registry.name_keys
    meta_run, meta_files = snapshot.latest(src, "meta", "columns")
    gone = " OR ".join(f"(dataset || '.' || table_name = '{k}' AND column_name IN ({', '.join(repr(c) for c in cols)}))"
                       for k, cols in WITHHELD.items()) or "false"
    con.execute(f"""CREATE TEMP TABLE meta_columns AS
                    SELECT * REPLACE (CAST(row_number() OVER (PARTITION BY dataset, table_name ORDER BY position) AS INTEGER) AS position)
                    FROM {snapshot.scan(meta_files)}
                    WHERE dataset || '.' || table_name IN ({', '.join(repr(k) for k in sorted(exported))}) AND NOT ({gone})""")
    for position, column in enumerate(NAME_KEYS.columns + catalogue.RUN_COLUMNS, start=1):
        con.execute("INSERT INTO meta_columns BY NAME SELECT ? AS dataset, ? AS table_name, ? AS position, ? AS column_name, "
                    "? AS meaning, ? AS how, ? AS source, ? AS table_grain, ? AS table_inputs, ? AS run_id, now() AS written_at",
                    [NAME_KEYS.dataset, NAME_KEYS.name, position, column.name, column.meaning, column.how, column.source,
                     NAME_KEYS.grain, ", ".join(NAME_KEYS.inputs), meta_run])
    copy("meta", "columns", meta_run, "SELECT * FROM meta_columns ORDER BY dataset, table_name, position")

    # registry/name_keys, computed from the same snapshot
    reader, runs = snapshot.open_views(src)
    names.w.STATE_NAMES.update(reader.execute("SELECT postal, upper(state_name) FROM code_states").fetchall())
    rows = name_rows(reader)
    keys_run = runs["bodies"]
    with tempfile.NamedTemporaryFile("w", suffix=".csv", newline="", delete=False, dir=out) as handle:
        csv.writer(handle).writerows(rows)
    types = {"is_anchor": "BOOLEAN"}
    columns = ", ".join(f"'{c}': '{types.get(c, 'VARCHAR')}'" for c in NAME_KEY_COLUMNS)
    try:
        keys_file = copy(NAME_KEYS.dataset, NAME_KEYS.name, keys_run,
                         f"SELECT *, '{names.VERSION}' AS normaliser_version, '{keys_run}' AS run_id, now() AS written_at "
                         f"FROM read_csv('{handle.name}', header = false, quote = '\"', escape = '\"', columns = {{{columns}}})")
    finally:
        Path(handle.name).unlink()  # this function's own temporary file
    catalogue.check(NAME_KEYS, [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{keys_file}')").fetchall()])

    # the same check on what was written
    check = {f"{d}.{t}": people_check(con, f"SELECT * FROM read_parquet('{target}')") for d, t, _, target in written}
    if _failures(check):
        raise ValueError(f"people data found in the written release {out}: {json.dumps(_failures(check))}")

    files = []
    for dataset, table, run, target in written:
        n_rows = con.execute(f"SELECT count(*) FROM read_parquet('{target}')").fetchone()[0]
        n_cols = len(con.execute(f"DESCRIBE SELECT * FROM read_parquet('{target}')").fetchall())
        files.append({"path": str(target.relative_to(out)), "table": f"{dataset}.{table}", "run_id": run, "rows": n_rows,
                      "columns": n_cols, "bytes": target.stat().st_size, "sha256": _sha256(target)})
    manifest = {
        "format": "ddwb registry release",
        "layout": "<dataset>/<table>/<run_id>.parquet: the latest run of each table, as in data/tables; read with "
                  "ddwb.registry (DDWB_REGISTRY_DIR=<this folder>) or any Parquet reader",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "normaliser_version": names.VERSION,
        "name_keys_from": {"standard.records": runs["records"], "entities.body_records": runs["body_records"]},
        "files": sorted(files, key=lambda f: f["path"]),
        "total_bytes": sum(f["bytes"] for f in files),
        "withheld_columns": WITHHELD,
        "redacted": {"why": "an agency typed an e-mail address or a phone number into a website field", **redacted},
        "people_check": {"result": "pass", "column_pattern": people.COLUMN.pattern, "email_pattern": people.EMAIL,
                         "phone_pattern": people.PHONE,
                         "tables": {k: {x: v[x] for x in ("people_columns", "email_values", "phone_values")}
                                    for k, v in sorted(check.items())}},
        "notes": list(NOTES),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest
