"""Illinois Comptroller, local government units (UnitData.csv): every unit of local government in Illinois.

File: states/il/il_comptroller_unitdata.csv (https://files.illinoiscomptroller.gov/LocGov/UnitData.csv), Windows-1252.
The file is read as published with two exceptions, each checked by a test:
- people data is not kept: the named contacts (names, titles, phones, e-mails of the unit's contact, CEO, CFO,
  purchasing agent, TIF administrator and FOIA officer). Of the contact's e-mail only the domain is kept
  (Email_GOV_domain), because a unit's mail domain is often its website's;
- one record is broken across two lines in the published file (Westchester, an unquoted line break); its two
  parts are joined, with a space where the break was.
"""

from __future__ import annotations

import csv

import duckdb

from .. import catalogue, paths, tables
from . import PROVENANCE, sha256

FILE = "states/il/il_comptroller_unitdata.csv"
EDITION = "Illinois Comptroller, local government units (UnitData.csv), downloaded 2026-10-31"
KEPT = (
    ("County", "the county the unit is in"), ("UnitName", "the unit's name"),
    ("Description", "the kind of unit (Village, Township, Fire Protection District, ...)"),
    ("Code", "the Comptroller's code for the unit"), ("Address", "the unit's address"), ("City", "its city"),
    ("State", "its state"), ("ZIP", "its ZIP code"),
)

SPEC = catalogue.register(catalogue.Table(
    dataset="raw", name="il_comptroller_units", grain="Illinois unit of local government",
    description=f"{EDITION}: the unit's own columns as published, the domain of its contact e-mail, no people data "
                "(see ddwb/raw/illinois.py)",
    columns=tuple(catalogue.Column(c, m, "copy", f"{FILE}:{c}") for c, m in KEPT)
            + (catalogue.Column("Email_GOV_domain", "the domain of the unit's contact e-mail (the address itself is not kept)",
                                "split", f"{FILE}:Email_GOV"),)
            + PROVENANCE,
    inputs=(FILE,),
))


def _rows() -> list[list[str]]:
    with open(paths.DOWNLOADS / FILE, encoding="cp1252", newline="") as handle:
        rows = list(csv.reader(handle))
    header, width, out, i = rows[0], len(rows[0]), [], 1
    while i < len(rows):
        row = rows[i]
        if len(row) < width and i + 1 < len(rows) and len(row) + len(rows[i + 1]) - 1 == width:
            row = row[:-1] + [row[-1] + " " + rows[i + 1][0]] + rows[i + 1][1:]  # a record broken by an unquoted line break
            i += 1
        if len(row) != width:
            raise ValueError(f"{FILE}: row {len(out) + 1} has {len(row)} fields, not {width}")
        record = dict(zip(header, row))
        email = record.get("Email_GOV", "")
        out.append([record[c] or None for c, _ in KEPT] + [email.rsplit("@", 1)[1].strip().lower() if "@" in email else None])
        i += 1
    return out


def build(run_id: str) -> dict[str, int]:
    con = duckdb.connect()
    names = [c for c, _ in KEPT] + ["Email_GOV_domain"]
    con.execute(f"CREATE TEMP TABLE units ({', '.join(f'{n} VARCHAR' for n in names)}, _row BIGINT)")
    con.executemany(f"INSERT INTO units VALUES ({', '.join('?' for _ in names)}, ?)",
                    [row + [i] for i, row in enumerate(_rows(), start=1)])
    file_hash = sha256(paths.DOWNLOADS / FILE)
    select = f"""SELECT {', '.join(names)}, '{FILE}' AS _file, '' AS _sheet, _row, '{file_hash}' AS _file_sha256,
                        '{EDITION}' AS _edition FROM units ORDER BY _row"""
    return {SPEC.key: tables.write(con, SPEC, select, run_id, {FILE: file_hash})}
