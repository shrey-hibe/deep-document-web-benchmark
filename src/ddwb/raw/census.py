"""Census Bureau, Government Units List 2026 (Census of Governments): every US government, one tab per kind.

File: census/gov_units_2026.zip (https://www2.census.gov/programs-surveys/gus/datasets/2026/gov_units_2026.zip),
member Govt_Units_2026_Final.xlsx; documentation in the same zip (Government_Units_List_Documentation_2026.pdf).
Each tab becomes one raw table, its columns exactly as published.
"""

from __future__ import annotations

import duckdb

from .. import catalogue, paths, tables
from . import PROVENANCE, sha256, unzip_member

ARCHIVE = "census/gov_units_2026.zip"
MEMBER = "Govt_Units_2026_Final.xlsx"
EDITION = "Census of Governments: Government Units List 2026 (final)"

# What each published column holds, in the Census's own terms (documentation PDF in the zip).
MEANING = {
    "CENSUS_ID_PID6": "the Census Bureau's 6-digit ID for the government",
    "UNIT_NAME": "the government's name",
    "UNIT_TYPE": "the Census's code and name for the kind of government (e.g. '1 - COUNTY')",
    "FUNCTION_NAME": "special districts: the Census's code and name for the district's function (e.g. '24 - FIRE PROTECTION')",
    "ACTIVITY_NAME": "pension systems: the Census's code and name for the activity",
    "TITLE": "the title of the official the Census mails to (a role, not a person's name)",
    "ADDRESS1": "mailing address, line 1", "ADDRESS2": "mailing address, line 2", "CITY": "mailing city", "STATE": "mailing state (postal code)",
    "ZIP": "mailing ZIP code", "ZIP4": "mailing ZIP+4 suffix",
    "WEB_ADDRESS": "the government's website as reported to the Census (self-reported, not checked by the Census)",
    "POLITICAL_CODE_DESCRIPTION": "general purpose: the Census's description of the political unit (e.g. city, village, borough)",
    "POPULATION": "general purpose: population", "POPULATION_SOURCE_YEAR": "general purpose: the year of that population figure",
    "SCHOOL_ENROLLMENT": "school districts: enrollment", "ENROLLMENT_YEAR": "school districts: the year of that enrollment",
    "SCHOOL_LEVEL_DESCRIPTION": "school districts: the grades served (elementary, secondary, unified ...)",
    "FIPS_STATE": "2-digit state FIPS code", "FIPS_COUNTY": "3-digit county FIPS code within the state",
    "FIPS_PLACE": "general purpose: 5-digit place FIPS code within the state",
    "COUNTY_AREA_NAME": "the county the government is in, by name",
    "ACTIVE": "Y if the government is active, N if not",
    "PARENT_CENSUS_ID_PID6": "dependent units: the 6-digit ID of the government that runs this one",
    "PARENT_UNIT_NAME": "dependent units: the name of the government that runs this one",
}

# Tab -> (raw table name, the tab's columns in published order).
TABS = {
    "General Purpose": ("census_gus_2026_general_purpose", (
        "CENSUS_ID_PID6", "UNIT_NAME", "UNIT_TYPE", "TITLE", "ADDRESS1", "ADDRESS2", "CITY", "STATE", "ZIP", "ZIP4", "WEB_ADDRESS",
        "POLITICAL_CODE_DESCRIPTION", "POPULATION", "POPULATION_SOURCE_YEAR", "FIPS_STATE", "FIPS_COUNTY", "FIPS_PLACE",
        "COUNTY_AREA_NAME", "ACTIVE")),
    "Special District": ("census_gus_2026_special_district", (
        "CENSUS_ID_PID6", "UNIT_NAME", "UNIT_TYPE", "FUNCTION_NAME", "TITLE", "ADDRESS1", "ADDRESS2", "CITY", "STATE", "ZIP", "ZIP4",
        "WEB_ADDRESS", "FIPS_STATE", "FIPS_COUNTY", "COUNTY_AREA_NAME", "ACTIVE")),
    "School District": ("census_gus_2026_school_district", (
        "CENSUS_ID_PID6", "UNIT_NAME", "UNIT_TYPE", "TITLE", "ADDRESS1", "ADDRESS2", "CITY", "STATE", "ZIP", "ZIP4", "WEB_ADDRESS",
        "SCHOOL_ENROLLMENT", "ENROLLMENT_YEAR", "SCHOOL_LEVEL_DESCRIPTION", "FIPS_STATE", "FIPS_COUNTY", "COUNTY_AREA_NAME", "ACTIVE")),
    "DEP School Dist": ("census_gus_2026_dependent_school_district", (
        "CENSUS_ID_PID6", "UNIT_NAME", "UNIT_TYPE", "TITLE", "ADDRESS1", "ADDRESS2", "CITY", "STATE", "ZIP", "ZIP4", "WEB_ADDRESS",
        "SCHOOL_ENROLLMENT", "ENROLLMENT_YEAR", "SCHOOL_LEVEL_DESCRIPTION", "FIPS_STATE", "FIPS_COUNTY", "COUNTY_AREA_NAME", "ACTIVE",
        "PARENT_CENSUS_ID_PID6", "PARENT_UNIT_NAME")),
    "Public Pension Sys": ("census_gus_2026_public_pension_system", (
        "CENSUS_ID_PID6", "UNIT_NAME", "UNIT_TYPE", "ACTIVITY_NAME", "TITLE", "ADDRESS1", "ADDRESS2", "CITY", "STATE", "ZIP", "ZIP4",
        "WEB_ADDRESS", "FIPS_STATE", "FIPS_COUNTY", "COUNTY_AREA_NAME", "ACTIVE", "PARENT_CENSUS_ID_PID6", "PARENT_UNIT_NAME")),
}

GRAIN = {
    "General Purpose": "general-purpose government (county, municipality, town or township)",
    "Special District": "special district government",
    "School District": "independent school district government",
    "DEP School Dist": "dependent school system (run by another government)",
    "Public Pension Sys": "public pension system",
}

SPECS = {
    tab: catalogue.register(catalogue.Table(
        dataset="raw", name=name, grain=GRAIN[tab],
        description=f"Census Government Units List 2026, tab '{tab}', exactly as published",
        columns=tuple(catalogue.Column(c, MEANING[c], "copy", f"{ARCHIVE}!{MEMBER}#{tab}:{c}") for c in columns) + PROVENANCE,
        inputs=(f"{ARCHIVE}!{MEMBER}#{tab}",),
    ))
    for tab, (name, columns) in TABS.items()
}


def build(run_id: str) -> dict[str, int]:
    archive = paths.DOWNLOADS / ARCHIVE
    workbook = unzip_member(archive, MEMBER)
    file_hash = sha256(archive)
    con = duckdb.connect()
    con.execute("LOAD excel; SET threads = 1; SET preserve_insertion_order = true")  # keeps rows in file order for _row
    counts = {}
    for tab, spec in SPECS.items():
        published = [c.name for c in spec.columns if not c.name.startswith("_")]
        select = f"""
            SELECT {', '.join(f'"{c}"' for c in published)},
                   '{ARCHIVE}!{MEMBER}' AS _file, '{tab}' AS _sheet, row_number() OVER () AS _row,
                   '{file_hash}' AS _file_sha256, '{EDITION}' AS _edition
            FROM read_xlsx('{workbook}', sheet = '{tab}', all_varchar = true)"""
        counts[spec.key] = tables.write(con, spec, select, run_id, {f"{ARCHIVE}": file_hash})
    return counts
