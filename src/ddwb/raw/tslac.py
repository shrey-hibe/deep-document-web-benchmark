"""Texas State Library and Archives Commission: public library directory (TXPLDirectory20260930.xlsx), main libraries.

File: states/tx/tx_tslac_directory_20260930.xlsx, tab 'Main-Central' (one row per main library). Only the library's
own columns are kept: the director's name and the administrator's e-mail are people data.
"""

from __future__ import annotations

import duckdb

from .. import catalogue, paths, tables
from . import PROVENANCE, sha256

FILE = "states/tx/tx_tslac_directory_20260930.xlsx"
SHEET = "Main-Central"
EDITION = "TSLAC Texas public library directory of 2026-09-30, downloaded 2026-10-31"
KEPT = (
    ("Library Name", "the library's name"), ("Service Population", "population served"),
    ("SFY 2026 Accreditation Status", "accreditation status"), ("Street City", "city"), ("Street ZIP Code", "ZIP code"),
    ("County", "county"), ("Legal Establishment", "how the library is established (city, county, district, nonprofit, ...)"),
    ("Web URL", "the library's website"), ("Library ID", "TSLAC's library ID"), ("FSCS", "the IMLS FSCS ID"),
)

SPEC = catalogue.register(catalogue.Table(
    dataset="raw", name="tx_tslac_libraries", grain="Texas main public library",
    description=f"{EDITION}: tab '{SHEET}', the library's own columns as published, no people data (see ddwb/raw/tslac.py)",
    columns=tuple(catalogue.Column(c, m, "copy", f"{FILE}#{SHEET}:{c}") for c, m in KEPT) + PROVENANCE,
    inputs=(FILE,),
))


def build(run_id: str) -> dict[str, int]:
    path = paths.DOWNLOADS / FILE
    con = duckdb.connect()
    con.execute("LOAD excel; SET threads = 1; SET preserve_insertion_order = true")
    columns = ", ".join(f'"{c}"' for c, _ in KEPT)
    select = f"""SELECT {columns}, '{FILE}' AS _file, '{SHEET}' AS _sheet, row_number() OVER () AS _row,
                        '{sha256(path)}' AS _file_sha256, '{EDITION}' AS _edition
                 FROM read_xlsx('{path}', sheet = '{SHEET}', all_varchar = true)
                 WHERE "Library Name" IS NOT NULL"""
    return {SPEC.key: tables.write(con, SPEC, select, run_id, {FILE: sha256(path)})}
