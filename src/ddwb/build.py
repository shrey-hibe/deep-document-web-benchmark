"""The one build command: every step in dependency order, then the catalogue table and the warehouse views.

A change upstream is carried downstream by building everything below it in the same run; meta.stale lists any
table built from an older run of its inputs than the latest.

Run: uv run python -m ddwb.build   (close the DuckDB UI first: it locks the warehouse file)
"""

from __future__ import annotations

import duckdb

from . import catalogue, tables, warehouse
from .entities import bodies
from .evals import log as eval_log  # noqa: F401  (registers the eval tables; filled by test runs)
from .fetch import live_check  # noqa: F401  (registers web.fetches and web.links; filled by its own command)
from .labels import relations, websites
from .raw import arcgis, census, files, illinois, irs, sam, tslac
from .standard import links, records, site_claims

# (step, function) in dependency order: raw, then standard, labels, entities.
STEPS = (
    ("raw: Census Government Units List 2026", census.build),
    ("raw: agency and state files (NCES, EPA, IMLS, USFA, NTD, Census F-33, .gov, TX, CO, NY, OR, DE, USAC, PA, Wikidata) and the Census 2022 finance unit list", files.build),
    ("raw: SAM.gov public entity extract", sam.build),
    ("raw: Illinois Comptroller local government units", illinois.build),
    ("raw: state map layers (CAL FIRE fire agencies; WI, MA, NJ, VT, UT, WA, MN, VA libraries)", arcgis.build),
    ("raw: IRS Form 990-N filings", irs.build),
    ("raw: Texas State Library directory", tslac.build),
    ("standard: code tables and records", records.build),
    ("standard: links the agencies publish", links.build),
    ("standard: website claims from outside sources", site_claims.build),
    ("labels: which record each website claim belongs to", websites.build),
    ("labels: rule-based candidates for same-body and run-by links", relations.build),
    ("entities: bodies, the records that form them, identifiers, relations, websites", bodies.build),
)

COLUMNS = catalogue.register(catalogue.Table(
    dataset="meta", name="columns", grain="column of a table in this warehouse",
    description="The column catalogue: what every column means, where it comes from and how it is made",
    columns=(
        catalogue.Column("dataset", "the dataset (schema) the table is in", "bookkeeping"),
        catalogue.Column("table_name", "the table", "bookkeeping"),
        catalogue.Column("position", "the column's position in the table", "bookkeeping"),
        catalogue.Column("column_name", "the column", "bookkeeping"),
        catalogue.Column("meaning", "what the column holds", "bookkeeping"),
        catalogue.Column("how", "copy, cast, join_codes, code_table, label or bookkeeping (see ddwb/catalogue.py)", "bookkeeping"),
        catalogue.Column("source", "the upstream table.column or the source file's column it comes from", "bookkeeping"),
        catalogue.Column("table_grain", "one row of the table per what", "bookkeeping"),
        catalogue.Column("table_inputs", "the files or tables the table is built from", "bookkeeping"),
    ),
))


def build() -> None:
    run_id = tables.new_run_id()
    print(f"run {run_id}")
    for step, function in STEPS:
        for key, rows in function(run_id).items():
            print(f"  {key}: {rows:,} rows  ({step})")
    con = duckdb.connect()
    con.execute("CREATE TEMP TABLE catalogue_rows (dataset VARCHAR, table_name VARCHAR, position INTEGER, column_name VARCHAR, "
                "meaning VARCHAR, how VARCHAR, source VARCHAR, table_grain VARCHAR, table_inputs VARCHAR)")
    con.executemany("INSERT INTO catalogue_rows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [tuple(r.values()) for r in catalogue.rows()])
    rows = tables.write(con, COLUMNS, "SELECT * FROM catalogue_rows", run_id, {})
    print(f"  meta.columns: {rows:,} rows")
    warehouse.build()
    print("warehouse rebuilt")


if __name__ == "__main__":
    build()
