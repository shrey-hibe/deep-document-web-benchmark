"""standard.records and the code tables it looks codes up in.

- standard.code_lists, code_states, code_entity_types: one CSV each in standard/codes/, every row citing the document
  it was copied from. code_entity_types is this repo's one name per agency code (Census UNIT_TYPE 1 -> county).
- standard.records: one row per record of every list in scope, made from the raw tables by copy, cast, split,
  join_codes and code-table lookups only. Which records are the same body, which body runs which, how big a body is:
  those are judgements, and live in the labels dataset.

What counts as a record:
- every row of the five Census 2026 tabs, and the Census 2022 finance units that are not in the 2026 list;
- every NCES LEA, and the NCES charter schools whose LEA is not itself a charter LEA (a charter school inside a
  charter LEA is covered by that LEA's record);
- every EPA community water system (all owner types), IMLS administrative entity, USFA registry row and NTD reporter
  row (one per program it reports through).
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from .. import catalogue, tables
from ..catalogue import Column
from ..raw import census, files
from ..raw import sha256 as file_sha256

CODES = Path(__file__).parent / "codes"

CODE_TABLES = {
    "code_lists": ("code in one of the agencies' code lists",
                   "The agencies' own code lists, copied from their documentation",
                   (("code_list", "which code list: census_active, census_government_type, epa_activity, epa_owner_type, "
                                  "epa_pws_type, imls_legal_basis, imls_structure_change"),
                    ("code", "the code as it appears in the agency's file"),
                    ("meaning", "the agency's wording for the code"),
                    ("source", "the document the code was copied from"))),
    "code_states": ("state or territory",
                    "State and territory FIPS codes and postal abbreviations, copied from the Census and IMLS documentation",
                    (("fips_state", "2-digit FIPS code"), ("postal", "2-letter postal abbreviation"),
                     ("state_name", "the name"), ("source", "the document the row was copied from"))),
    "code_entity_types": ("kind of record in one list (an agency code, or '*' for a whole list)",
                          "This repo's name for each kind of record an agency publishes: one name per agency code, a renaming, "
                          "not a judgement",
                          (("list", "the raw table"), ("unit_type_code", "the agency's code for the kind of unit, or '*' for every row of the list"),
                           ("entity_type", "this repo's name for that kind"), ("note", "the agency's wording or the rule"))),
}

CODE_SPECS = {
    name: catalogue.register(catalogue.Table(
        dataset="standard", name=name, grain=grain, description=description,
        columns=tuple(Column(c, m, "copy", f"ddwb/standard/codes/{name}.csv:{c}") for c, m in columns),
        inputs=(f"ddwb/standard/codes/{name}.csv",),
    ))
    for name, (grain, description, columns) in CODE_TABLES.items()
}

GUS = {tab: name for tab, (name, _) in census.TABS.items()}
GP, SD, SCH, DEP, PEN = (GUS[t] for t in census.TABS)
FIN = files.FIN_SPEC.name
LEA, SCHOOL, EPA, IMLS, USFA, NTD = ("nces_lea_directory_2025_26", "nces_school_directory_2025_26", "epa_sdwis_water_system",
                                     "imls_pls_fy2024_administrative_entity", "usfa_registry", "ntd_reporters_2025")
assert {LEA, SCHOOL, EPA, IMLS, USFA, NTD} <= {s.table for s in files.SOURCES}

# (column, type, meaning, how, source). `source` names the raw columns per list.
COLUMNS = (
    ("record_id", "VARCHAR", "this repo's ID for the record: '<id_scheme>:<agency_id>'. Where the agency ID repeats in the list (NTD) "
     "the reporter it reports through is added after '/'; USFA IDs are unique within a state, so the state comes first; "
     "a USFA row without an ID gets '<list>#<row>'", "join_codes", "id_scheme, agency_id; NTD reported_by_ntd_id; USFA HQ state, _row"),
    ("list", "VARCHAR", "the raw table the record comes from", "bookkeeping", ""),
    ("source_row", "BIGINT", "the record's row in that raw table", "copy", "raw.<list>._row"),
    ("edition", "VARCHAR", "the edition of the list, as the agency names it", "copy", "raw.<list>._edition"),
    ("agency", "VARCHAR", "the agency that publishes the list", "bookkeeping", ""),
    ("id_scheme", "VARCHAR", "the ID system agency_id belongs to: census_pid6, census_government_id, nces_leaid, nces_ncessch, "
     "epa_pwsid, imls_fscskey, usfa_fdid (unique within a state), ntd_id", "bookkeeping", ""),
    ("agency_id", "VARCHAR", "the agency's ID for the record", "copy",
     "Census CENSUS_ID_PID6; finance ID; NCES LEAID, NCESSCH; EPA pwsid; IMLS FSCSKEY; USFA FDID; NTD ntd_id"),
    ("name", "VARCHAR", "the record's name as published", "copy",
     "Census UNIT_NAME; finance ID_NAME; NCES LEA_NAME, SCH_NAME; EPA pws_name; IMLS LIBNAME; USFA Fire dept name; NTD agency_name"),
    ("entity_type", "VARCHAR", "this repo's name for the kind of record, from the agency's own code (standard.code_entity_types)", "code_table",
     "list + unit_type_code -> standard.code_entity_types"),
    ("unit_type_code", "VARCHAR", "the agency's code for the kind of unit", "split",
     "Census UNIT_TYPE before ' - ' (general purpose, special district and school district tabs); finance ID position 3; "
     "NCES LEA_TYPE, SCH_TYPE; EPA pws_type_code"),
    ("unit_type", "VARCHAR", "the agency's wording for that kind", "code_table",
     "Census UNIT_TYPE after ' - '; finance: code_lists census_government_type; NCES LEA_TYPE_TEXT, SCH_TYPE_TEXT; EPA: code_lists epa_pws_type"),
    ("function_code", "VARCHAR", "Census special districts: the function code; pension systems: the activity code", "split",
     "Census FUNCTION_NAME, ACTIVITY_NAME before ' - '; finance FUNCTION_CODE"),
    ("function", "VARCHAR", "the Census's wording for that function or activity", "code_table",
     "Census FUNCTION_NAME, ACTIVITY_NAME after ' - '; finance: the same code's wording in the 2026 Special District tab"),
    ("state", "VARCHAR", "the state or territory, postal abbreviation (as published, or looked up from the FIPS code)", "code_table",
     "Census FIPS_STATE -> code_states (not STATE, which is the mailing address); finance ID positions 1-2 -> code_states; "
     "NCES ST; EPA state_code; IMLS STABR; USFA HQ state; NTD state"),
    ("state_fips", "VARCHAR", "the state or territory, 2-digit FIPS code (as published, or looked up from the postal abbreviation)", "code_table",
     "Census FIPS_STATE; finance ID positions 1-2; NCES FIPST; others: state -> code_states"),
    ("county_fips5", "VARCHAR", "5-digit county FIPS code: the county the government is in (Census), or the county of the "
     "library's geocoded address (IMLS)", "join_codes",
     "Census FIPS_STATE + FIPS_COUNTY; finance ID positions 1-2 + 4-6 (none for states); IMLS CENTRACT positions 1-5"),
    ("county_name", "VARCHAR", "the county, by name, as published", "copy",
     "Census COUNTY_AREA_NAME; finance COUNTY_NAME; IMLS CNTY; USFA County"),
    ("place_fips", "VARCHAR", "7-digit place FIPS code (state + place)", "join_codes",
     "Census general purpose FIPS_STATE + FIPS_PLACE; finance ID positions 1-2 + FIPS_PLACE"),
    ("city", "VARCHAR", "the city of the record's address as published (Census: mailing address; NCES: location)", "copy",
     "Census CITY; NCES LCITY; EPA city_name; IMLS CITY; USFA HQ city; NTD city"),
    ("zip5", "VARCHAR", "the first five digits of that address's ZIP code", "split",
     "Census ZIP; NCES LZIP; EPA zip_code; IMLS ZIP; USFA HQ zip; NTD zip_code"),
    ("website", "VARCHAR", "the website as published, unchecked (EPA and IMLS publish none)", "copy",
     "Census WEB_ADDRESS; NCES WEBSITE; USFA Website; NTD url"),
    ("status_code", "VARCHAR", "the agency's status code", "copy",
     "Census ACTIVE; NCES UPDATED_STATUS; EPA pws_activity_code; IMLS STATSTRU (a structure-change code)"),
    ("status", "VARCHAR", "the agency's wording for that code, never translated (USFA, NTD and the finance list publish none)", "code_table",
     "Census: code_lists census_active; NCES UPDATED_STATUS_TEXT; EPA: code_lists epa_activity; IMLS: code_lists imls_structure_change"),
    ("parent_agency_id", "VARCHAR", "Census dependent units: the Census ID of the government that runs this one", "copy",
     "Census PARENT_CENSUS_ID_PID6 (dependent school and pension tabs)"),
    ("parent_agency_name", "VARCHAR", "Census dependent units: the name of the government that runs this one", "copy",
     "Census PARENT_UNIT_NAME (dependent school and pension tabs)"),
    ("parent_unit_type", "VARCHAR", "Census dependent units: the kind of government that runs this one", "split",
     "Census UNIT_TYPE after ' - ' (dependent school and pension tabs, where it describes the government running the unit)"),
    ("school_lea_id", "VARCHAR", "NCES schools: the NCES ID of the school's LEA", "copy", "NCES school LEAID"),
    ("owner_type_code", "VARCHAR", "EPA: the owner type code", "copy", "EPA owner_type_code"),
    ("owner_type", "VARCHAR", "EPA: the wording for that code", "code_table", "EPA: code_lists epa_owner_type"),
    ("owner_name", "VARCHAR", "EPA: the owning organisation's name, where EPA returned one", "copy", "EPA org_name"),
    ("legal_basis_code", "VARCHAR", "IMLS: the legal basis code", "copy", "IMLS C_LEGBAS"),
    ("legal_basis", "VARCHAR", "IMLS: the wording for that code", "code_table", "IMLS: code_lists imls_legal_basis"),
    ("organization_type", "VARCHAR", "the organisation type as published", "copy", "USFA Organization Type; NTD organization_type"),
    ("department_type", "VARCHAR", "USFA: career, mostly career, mostly volunteer or volunteer", "copy", "USFA Dept Type"),
    ("reporter_type", "VARCHAR", "NTD: the reporter type", "copy", "NTD reporter_type"),
    ("reported_by_id", "VARCHAR", "NTD: the NTD ID of the agency that reports for this one", "copy", "NTD reported_by_ntd_id"),
    ("state_agency_id", "VARCHAR", "the state's own ID for the record, as published", "copy", "NCES ST_LEAID, ST_SCHID; IMLS LIBID"),
    ("ueid", "VARCHAR", "NTD: the SAM.gov unique entity ID", "copy", "NTD ueid"),
    ("service_area_geoid", "VARCHAR", "IMLS: the Census GEOID of the library's legal service area", "copy", "IMLS LSAGEOID"),
    ("service_area_type", "VARCHAR", "IMLS: the kind of that area (PLACE, COUNTY, COUNTY SUBDIVISION, ...)", "copy", "IMLS LSAGEOTYPE"),
    ("population", "BIGINT", "people in the area served, as the agency counts it: residents (Census), people served (EPA), "
     "legal service area population (IMLS), service area population (NTD)", "cast",
     "Census general purpose POPULATION (thousands separators removed); finance POPULATION; EPA population_served_count; "
     "IMLS POPU_LSA (its missing-value codes -1, -3, -4, -9 become empty); NTD service_area_pop"),
    ("enrollment", "BIGINT", "school districts: enrollment", "cast",
     "Census SCHOOL_ENROLLMENT (thousands separators removed); finance ENROLLMENT"),
)

SPEC = catalogue.register(catalogue.Table(
    dataset="standard", name="records", grain="record in one of the agencies' lists",
    description="One row per record of every list in scope, by mechanical steps only (see ddwb/standard/records.py)",
    columns=tuple(Column(name, meaning, how, source) for name, _, meaning, how, source in COLUMNS),
    inputs=tuple(f"raw.{t}" for t in (*GUS.values(), FIN, LEA, SCHOOL, EPA, IMLS, USFA, NTD))
           + tuple(f"standard.{name}" for name in CODE_TABLES),
))

# Lookup columns used while building, dropped before writing: which code list each status and unit type is in.
LOOKUPS = ("status_list", "unit_type_list", "fin_function_code")


def _select(list_name: str, agency: str, id_scheme: str, source: str, **exprs: str) -> str:
    """One list's rows with every column, empty where the list publishes nothing."""
    exprs = {"list": f"'{list_name}'", "source_row": "_row", "edition": "_edition", "agency": f"'{agency}'",
             "id_scheme": f"'{id_scheme}'", **exprs}
    out = [f"CAST({exprs.pop(name, 'NULL')} AS {kind}) AS {name}" for name, kind, *_ in COLUMNS if name != "record_id"]
    out += [f"CAST({exprs.pop(name, 'NULL')} AS VARCHAR) AS {name}" for name in LOOKUPS]
    if exprs:
        raise ValueError(f"{list_name}: unknown columns {sorted(exprs)}")
    return f"SELECT {', '.join(out)} FROM {source}"


def _split(column: str, part: int) -> str:
    """Part 1 (the code) or 2 (the wording) of a Census value such as '1 - COUNTY'."""
    return f"nullif(trim(split_part({column}, ' - ', {part})), '')"


def _number(column: str) -> str:
    return f"CAST(nullif(replace(trim({column}), ',', ''), '') AS BIGINT)"


def _census(table: str) -> str:
    dependent = table in (DEP, PEN)
    exprs = dict(
        agency_id="CENSUS_ID_PID6", name="UNIT_NAME", state_fips="FIPS_STATE", county_fips5="FIPS_STATE || FIPS_COUNTY",
        county_name="COUNTY_AREA_NAME", city="CITY", zip5="left(ZIP, 5)", website="WEB_ADDRESS", status_code="ACTIVE",
        status_list="'census_active'",
    )
    if dependent:
        exprs |= dict(parent_agency_id="PARENT_CENSUS_ID_PID6", parent_agency_name="PARENT_UNIT_NAME", parent_unit_type=_split("UNIT_TYPE", 2))
    else:
        exprs |= dict(unit_type_code=_split("UNIT_TYPE", 1), unit_type=_split("UNIT_TYPE", 2))
    if table == GP:
        exprs |= dict(place_fips="FIPS_STATE || FIPS_PLACE", population=_number("POPULATION"))
    if table == SD:
        exprs |= dict(function_code=_split("FUNCTION_NAME", 1), function=_split("FUNCTION_NAME", 2))
    if table == PEN:
        exprs |= dict(function_code=_split("ACTIVITY_NAME", 1), function=_split("ACTIVITY_NAME", 2))
    if table in (SCH, DEP):
        exprs |= dict(enrollment=_number("SCHOOL_ENROLLMENT"))
    return _select(table, "Census", "census_pid6", tables.latest("raw", table), **exprs)


def _finance(census_pid6: str) -> str:
    return _select(
        FIN, "Census", "census_government_id",
        f"{tables.latest('raw', FIN)} WHERE substr(ID, 7, 6) NOT IN (SELECT CENSUS_ID_PID6 FROM ({census_pid6}))",
        agency_id="ID", name="ID_NAME", unit_type_code="substr(ID, 3, 1)", unit_type_list="'census_government_type'",
        fin_function_code="nullif(FUNCTION_CODE, '')", function_code="nullif(FUNCTION_CODE, '')", state_fips="substr(ID, 1, 2)",
        county_fips5="CASE WHEN substr(ID, 4, 3) <> '000' THEN substr(ID, 1, 2) || substr(ID, 4, 3) END",
        county_name="nullif(COUNTY_NAME, '')",
        place_fips="CASE WHEN nullif(FIPS_PLACE, '') IS NOT NULL AND FIPS_PLACE <> '00000' THEN substr(ID, 1, 2) || FIPS_PLACE END",
        population=_number("POPULATION"), enrollment=_number("ENROLLMENT"),
    )


def build(run_id: str) -> dict[str, int]:
    con = duckdb.connect()
    counts = {}
    for name, spec in CODE_SPECS.items():
        path = CODES / f"{name}.csv"
        read = f"read_csv('{path}', all_varchar = true, header = true, delim = ',', quote = '\"', escape = '\"')"
        counts[spec.key] = tables.write(con, spec, f"SELECT * FROM {read}", run_id, {f"ddwb/standard/codes/{name}.csv": file_sha256(path)})
    code = {name: tables.latest("standard", name) for name in CODE_TABLES}

    census_pid6 = " UNION ALL ".join(f"SELECT CENSUS_ID_PID6 FROM {tables.latest('raw', t)}" for t in GUS.values())
    lea_types = f"(SELECT LEAID, LEA_TYPE FROM {tables.latest('raw', LEA)})"
    parts = [_census(t) for t in GUS.values()] + [
        _finance(census_pid6),
        _select(LEA, "NCES", "nces_leaid", tables.latest("raw", LEA),
                agency_id="LEAID", name="LEA_NAME", unit_type_code="LEA_TYPE", unit_type="LEA_TYPE_TEXT", state="ST", state_fips="FIPST",
                city="LCITY", zip5="left(LZIP, 5)", website="WEBSITE", status_code="UPDATED_STATUS", status="UPDATED_STATUS_TEXT",
                state_agency_id="ST_LEAID"),
        _select(SCHOOL, "NCES", "nces_ncessch",
                f"{tables.latest('raw', SCHOOL)} s JOIN {lea_types} l USING (LEAID) WHERE s.CHARTER_TEXT = 'Yes' AND l.LEA_TYPE <> '7'",
                agency_id="NCESSCH", name="SCH_NAME", unit_type_code="SCH_TYPE", unit_type="SCH_TYPE_TEXT", state="ST", state_fips="FIPST",
                city="LCITY", zip5="left(LZIP, 5)", website="WEBSITE", status_code="UPDATED_STATUS", status="UPDATED_STATUS_TEXT",
                school_lea_id="LEAID", state_agency_id="ST_SCHID"),
        _select(EPA, "EPA", "epa_pwsid", tables.latest("raw", EPA),
                agency_id="pwsid", name="pws_name", unit_type_code="pws_type_code", unit_type_list="'epa_pws_type'", state="state_code",
                city="city_name", zip5="left(zip_code, 5)", status_code="pws_activity_code", status_list="'epa_activity'",
                owner_type_code="owner_type_code", owner_name="org_name", population="population_served_count"),
        _select(IMLS, "IMLS", "imls_fscskey", tables.latest("raw", IMLS),
                agency_id="FSCSKEY", name="LIBNAME", state="STABR", county_fips5="left(CENTRACT, 5)", county_name="CNTY", city="CITY",
                zip5="left(ZIP, 5)", status_code="STATSTRU", status_list="'imls_structure_change'", legal_basis_code="C_LEGBAS",
                state_agency_id="LIBID", population="CASE WHEN CAST(POPU_LSA AS BIGINT) >= 0 THEN POPU_LSA END",
                service_area_geoid="LSAGEOID", service_area_type="LSAGEOTYPE"),
        _select(USFA, "USFA", "usfa_fdid", tables.latest("raw", USFA),
                agency_id='"FDID"', name='"Fire dept name"', state='"HQ state"', county_name='"County"', city='"HQ city"',
                zip5='left("HQ zip", 5)', website='"Website"', organization_type='"Organization Type"', department_type='"Dept Type"'),
        _select(NTD, "FTA", "ntd_id", tables.latest("raw", NTD),
                agency_id="ntd_id", name="agency_name", state="state", city="city", zip5="left(zip_code, 5)", website="url",
                organization_type="organization_type", reporter_type="reporter_type", reported_by_id="reported_by_ntd_id",
                ueid="ueid", population="service_area_pop"),
    ]
    functions = f"(SELECT DISTINCT {_split('FUNCTION_NAME', 1)} AS code, {_split('FUNCTION_NAME', 2)} AS wording FROM {tables.latest('raw', SD)})"
    select = f"""
        WITH stage AS ({' UNION ALL '.join(parts)}),
        looked_up AS (
            SELECT s.* REPLACE (
                       coalesce(s.unit_type, ut.meaning) AS unit_type,
                       coalesce(s.function, f.wording) AS function,
                       coalesce(s.state, bf.postal) AS state,
                       coalesce(s.state_fips, bp.fips_state) AS state_fips,
                       coalesce(s.status, st.meaning) AS status,
                       ot.meaning AS owner_type,
                       lb.meaning AS legal_basis,
                       e.entity_type AS entity_type),
                   count(*) OVER (PARTITION BY s.list, s.agency_id) AS id_rows
            FROM stage s
            LEFT JOIN {code['code_lists']} ut ON ut.code_list = s.unit_type_list AND ut.code = s.unit_type_code
            LEFT JOIN {functions} f ON f.code = s.fin_function_code
            LEFT JOIN {code['code_states']} bf ON bf.fips_state = s.state_fips AND s.state IS NULL
            LEFT JOIN {code['code_states']} bp ON bp.postal = s.state AND s.state_fips IS NULL
            LEFT JOIN {code['code_lists']} st ON st.code_list = s.status_list AND st.code = s.status_code
            LEFT JOIN {code['code_lists']} ot ON ot.code_list = 'epa_owner_type' AND ot.code = s.owner_type_code
            LEFT JOIN {code['code_lists']} lb ON lb.code_list = 'imls_legal_basis' AND lb.code = s.legal_basis_code
            LEFT JOIN {code['code_entity_types']} e ON e.list = s.list AND e.unit_type_code IN (s.unit_type_code, '*')
        )
        SELECT CASE
                   WHEN agency_id IS NULL THEN list || '#' || source_row
                   WHEN list = '{USFA}' THEN id_scheme || ':' || state || '-' || agency_id
                   WHEN id_rows > 1 AND list = '{NTD}' THEN id_scheme || ':' || agency_id || '/' || coalesce(reported_by_id, '-')
                   ELSE id_scheme || ':' || agency_id
               END AS record_id,
               {', '.join(name for name, *_ in COLUMNS if name != 'record_id')}
        FROM looked_up
        ORDER BY list, source_row"""
    inputs = {f"raw.{t}": tables.latest_run(con, "raw", t) for t in (*GUS.values(), FIN, LEA, SCHOOL, EPA, IMLS, USFA, NTD)}
    inputs |= {f"standard.{name}": run_id for name in CODE_TABLES}
    counts[SPEC.key] = tables.write(con, SPEC, select, run_id, inputs)
    return counts
