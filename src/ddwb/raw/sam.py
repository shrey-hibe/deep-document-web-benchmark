"""SAM.gov entity registrations: the public V2 monthly extract (pipe-delimited, no header, one row per registration).

File: sam/SAM_PUBLIC_UTF-8_MONTHLY_V2_20261005.ZIP, downloaded without an account from SAM.gov Data Services >
Entity Registration > Public V2. Column names and order come from SAM's SAM_MASTER_EXTRACT_MAPPING_Feb2025.xlsx
(Data Element Mapping, the 'Public' columns in column order), checked against rows of the file.

Two things are not kept (dropped under the old no-people rule, lifted 2026-11-05; add them back when the people layer needs them):
- columns 40-142: mailing address, then the points of contact (people's names and addresses) and flex fields;
- rows other than US government entities (ENTITY STRUCTURE 2A) and tax-exempt corporations (8H): the rest are
  businesses, many of them sole proprietors registered under their own name and home address.
"""

from __future__ import annotations

import zipfile

import duckdb

from .. import catalogue, paths, tables
from . import PROVENANCE, sha256, unzip_member

ARCHIVE = "sam/SAM_PUBLIC_UTF-8_MONTHLY_V2_20261005.ZIP"
EDITION = "SAM.gov Entity Registration, public V2 monthly extract of 2026-10-05 (UTF-8)"
MAPPING = "sam/SAM_MASTER_EXTRACT_MAPPING_Feb2025.xlsx"

# The first 39 public columns, in file order (SAM mapping names). Positions 1-39 were checked against rows of the file.
COLUMNS = (
    ("UNIQUE ENTITY ID", "SAM's 12-character Unique Entity ID (UEI)"),
    ("BLANK (DEPRECATED)", "empty (deprecated by SAM)"),
    ("ENTITY EFT INDICATOR", "suffix distinguishing an entity's bank accounts"),
    ("CAGE CODE", "the entity's CAGE code"),
    ("DODAAC", "Department of Defense activity address code"),
    ("SAM EXTRACT CODE", "A = active registration, E = expired"),
    ("PURPOSE OF REGISTRATION", "Z1 federal assistance, Z2 all awards, Z3 IGT only, Z4 assistance and IGT, Z5 all awards and IGT"),
    ("INITIAL REGISTRATION DATE", "YYYYMMDD"), ("REGISTRATION EXPIRATION DATE", "YYYYMMDD"),
    ("LAST UPDATE DATE", "YYYYMMDD"), ("ACTIVATION DATE", "YYYYMMDD"),
    ("LEGAL BUSINESS NAME", "the entity's legal name"), ("DBA NAME", "doing-business-as name"),
    ("ENTITY DIVISION NAME", "the division or department registered (e.g. AIRPORT under COUNTY OF PALM BEACH)"),
    ("ENTITY DIVISION NUMBER", "the division's number"),
    ("PHYSICAL ADDRESS LINE 1", "the entity's physical address"), ("PHYSICAL ADDRESS LINE 2", "the entity's physical address"),
    ("PHYSICAL ADDRESS CITY", "physical address city"), ("PHYSICAL ADDRESS PROVINCE OR STATE", "physical address state (postal code in the US)"),
    ("PHYSICAL ADDRESS ZIP/POSTAL CODE", "physical address ZIP"), ("PHYSICAL ADDRESS ZIP CODE +4", "ZIP+4 suffix"),
    ("PHYSICAL ADDRESS COUNTRY CODE", "physical address country (USA)"),
    ("PHYSICAL ADDRESS CONGRESSIONAL DISTRICT", "congressional district"),
    ("D&B OPEN DATA FLAG", "Y when name and address fields carry Dun & Bradstreet data, which has bulk-use limits"),
    ("ENTITY START DATE", "YYYYMMDD"), ("FISCAL YEAR END CLOSE DATE", "MMDD"),
    ("ENTITY URL", "the entity's website, as registered"),
    ("ENTITY STRUCTURE", "2A U.S. government entity, 8H corporate entity (tax exempt), and others"),
    ("STATE OF INCORPORATION", "state of incorporation"), ("COUNTRY OF INCORPORATION", "country of incorporation"),
    ("BUSINESS TYPE COUNTER", "how many business types follow"),
    ("BUS TYPE STRING", "'~'-separated business type codes: 12 U.S. local government, C6 municipality, C7 county, C8 city, "
                        "FO township, H6 school district, TW transit authority, 2F U.S. state government, 3I tribal government, ..."),
    ("PRIMARY NAICS", "primary NAICS code"), ("NAICS CODE COUNTER", "how many NAICS codes follow"),
    ("NAICS CODE STRING", "'~'-separated NAICS codes, each followed by Y/N (small business)"),
    ("PSC CODE COUNTER", "how many PSC codes follow"), ("PSC CODE STRING", "'~'-separated product and service codes"),
    ("CREDIT CARD USAGE", "Y/N"), ("CORRESPONDENCE FLAG", "correspondence preference"),
)

SPEC = catalogue.register(catalogue.Table(
    dataset="raw", name="sam_entity_public_2026_10", grain="SAM registration of a US government entity or tax-exempt corporation",
    description=f"{EDITION}: columns 1-39 of 142 and rows with ENTITY STRUCTURE 2A or 8H in the USA, as published; "
                "points of contact and other businesses are not kept (people data; see ddwb/raw/sam.py)",
    columns=tuple(catalogue.Column(name, f"{meaning} (SAM mapping: {MAPPING})", "copy", f"{ARCHIVE}:column {i}")
                  for i, (name, meaning) in enumerate(COLUMNS, start=1)) + PROVENANCE,
    inputs=(ARCHIVE,),
))


def _data_file():
    """The .dat inside the zip inside the downloaded zip, unpacked once into the cache."""
    outer = paths.DOWNLOADS / ARCHIVE
    inner = unzip_member(outer, zipfile.ZipFile(outer).namelist()[0])
    member = next(n for n in zipfile.ZipFile(inner).namelist() if n.endswith(".dat"))
    return unzip_member(inner, member), member


def build(run_id: str) -> dict[str, int]:
    data, member = _data_file()
    archive_hash = sha256(paths.DOWNLOADS / ARCHIVE)
    con = duckdb.connect()
    con.execute("SET threads = 1; SET preserve_insertion_order = true")  # keeps rows in file order for _row
    names = ", ".join(f'column{i:03d} AS "{name}"' for i, (name, _) in enumerate(COLUMNS))
    select = f"""
        SELECT * EXCLUDE (_n) FROM (
            SELECT {names}, '{ARCHIVE}!{member}' AS _file, '' AS _sheet, row_number() OVER () AS _row,
                   '{archive_hash}' AS _file_sha256, '{EDITION}' AS _edition, column000 AS _n
            FROM read_csv('{data}', delim = '|', quote = '', header = false, skip = 1, all_varchar = true,
                          null_padding = true, strict_mode = false)
            WHERE column000 NOT LIKE 'EOF %')
        WHERE "ENTITY STRUCTURE" IN ('2A', '8H') AND "PHYSICAL ADDRESS COUNTRY CODE" = 'USA'"""
    return {SPEC.key: tables.write(con, SPEC, select, run_id, {ARCHIVE: archive_hash})}
