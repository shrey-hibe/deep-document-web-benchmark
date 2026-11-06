"""IRS Form 990-N (e-Postcard) filings: small tax-exempt organisations, one row per filing (pipe-delimited, no header).

File: irs/data-download-epostcard.zip (https://apps.irs.gov/pub/epostcard/data-download-epostcard.zip). Columns in
the IRS's published order. Columns 9-16 (the principal officer's name and address) are not kept: people data. The
'Website URL' field is free text as the filer typed it (often an address or a name), so labels check its form.
"""

from __future__ import annotations

import zipfile

import duckdb

from .. import catalogue, paths, tables
from . import PROVENANCE, sha256, unzip_member

ARCHIVE = "irs/data-download-epostcard.zip"
EDITION = "IRS e-Postcard (Form 990-N) data download, file of 2026-10-26"

# (position in the file, column name, meaning). Positions 9-16, the principal officer, are skipped.
COLUMNS = (
    (1, "EIN", "employer identification number"), (2, "Tax Year", "the tax year filed for"),
    (3, "Organization Name", "the organisation's legal name"),
    (4, "Gross Receipts Not Greater Than 50000", "T when gross receipts are normally $50,000 or less"),
    (5, "Organization Has Terminated", "T when the organisation says it has terminated"),
    (6, "Tax Period Begin Date", "MM-DD-YYYY"), (7, "Tax Period End Date", "MM-DD-YYYY"),
    (8, "Website URL", "the organisation's website, free text as filed"),
    (17, "Organization Address Line 1", "the organisation's mailing address"),
    (18, "Organization Address Line 2", "the organisation's mailing address"),
    (19, "Organization Address City", "city"), (20, "Organization Address Province", "province (outside the US)"),
    (21, "Organization Address State", "state"), (22, "Organization Address Zip", "ZIP code"),
    (23, "Organization Address Country", "country"),
    (24, "Doing Business As Name 1", "a name it also uses"), (25, "Doing Business As Name 2", "a name it also uses"),
    (26, "Doing Business As Name 3", "a name it also uses"),
)

SPEC = catalogue.register(catalogue.Table(
    dataset="raw", name="irs_990n_filings", grain="Form 990-N filing (an organisation's e-Postcard for one tax year)",
    description=f"{EDITION}: every filing, the organisation's columns as published; the principal officer (people data) "
                "is not kept (see ddwb/raw/irs.py)",
    columns=tuple(catalogue.Column(name, meaning, "copy", f"{ARCHIVE}:field {position}") for position, name, meaning in COLUMNS)
            + PROVENANCE,
    inputs=(ARCHIVE,),
))


def build(run_id: str) -> dict[str, int]:
    archive = paths.DOWNLOADS / ARCHIVE
    member = zipfile.ZipFile(archive).namelist()[0]
    text = unzip_member(archive, member)
    file_hash = sha256(archive)
    con = duckdb.connect()
    con.execute("SET threads = 1; SET preserve_insertion_order = true")  # keeps rows in file order for _row
    names = ", ".join(f'column{position - 1:02d} AS "{name}"' for position, name, _ in COLUMNS)
    select = f"""
        SELECT {names}, '{ARCHIVE}!{member}' AS _file, '' AS _sheet, row_number() OVER () AS _row,
               '{file_hash}' AS _file_sha256, '{EDITION}' AS _edition
        FROM read_csv('{text}', delim = '|', quote = '', header = false, all_varchar = true, null_padding = true,
                      strict_mode = false, columns = {{{', '.join(f"'column{i:02d}': 'VARCHAR'" for i in range(26))}}})
        WHERE column00 IS NOT NULL"""
    return {SPEC.key: tables.write(con, SPEC, select, run_id, {ARCHIVE: file_hash})}
