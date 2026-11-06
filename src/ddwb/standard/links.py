"""standard.links: every link between records that an agency publishes, as published.

A link is one published column naming another unit by its ID: the government that runs a Census dependent unit, the
NCES district that the Census F-33 file gives for a school system, a school's district, a charter school's authorizer,
the NTD reporter that reports for another. Each side is matched to a record by exact ID only, using the Census's
documented ID layout where needed (positions 7-12 of a finance ID are the PID6). Nothing is matched by name: a target
that only an agency's own code names (a state's charter authorizer code) stays unmatched, for the labels layer.
"""

from __future__ import annotations

import duckdb

from .. import catalogue, tables
from ..catalogue import Column
from . import records

F33 = "census_f33_2024"
DEP, PEN, SCHOOL, NTD = records.DEP, records.PEN, records.SCHOOL, records.NTD

LINK_TYPES = {
    "run_by": "Census dependent school system or pension system -> the government that runs it (PARENT_CENSUS_ID_PID6)",
    "same_school_system": "Census F-33 school system (PID6) -> the NCES district for the same system (NCESID)",
    "school_of_district": "NCES school -> its district (LEAID)",
    "charter_authorizer": "NCES charter school -> its authorizer, by the state's code (CHARTAUTH1, CHARTAUTH2)",
    "reports_through": "NTD reporter -> the NTD reporter that reports for it (reported_by_ntd_id)",
}

SPEC = catalogue.register(catalogue.Table(
    dataset="standard", name="links", grain="link between two units that an agency publishes (one published column of one row)",
    description="Every link between records that an agency publishes; both sides matched to records by exact ID only "
                "(see ddwb/standard/links.py)",
    columns=(
        Column("link_type", "the kind of link, named for the column that publishes it: "
               + "; ".join(f"{k}: {v}" for k, v in LINK_TYPES.items()), "bookkeeping"),
        Column("from_scheme", "the ID system of the linking unit's ID", "bookkeeping"),
        Column("from_agency_id", "the linking unit's ID as published", "copy",
               "Census CENSUS_ID_PID6; F-33 PID6; NCES NCESSCH; NTD ntd_id"),
        Column("from_record_id", "the linking unit's record: the record whose row publishes the link or, for F-33 (not itself a "
               "list of records), the record holding exactly that ID when exactly one does", "id_lookup", "standard.records.record_id"),
        Column("from_records", "1 when the link is published in a record's own row; for F-33, how many records hold the ID",
               "id_lookup", "standard.records"),
        Column("to_scheme", "the ID system of the linked unit's ID: census_pid6, nces_leaid, ntd_id, or "
               "state_charter_authorizer_code (a state's own code, matched to no record)", "bookkeeping"),
        Column("to_agency_id", "the linked unit's ID as published", "copy",
               "Census PARENT_CENSUS_ID_PID6; F-33 NCESID; NCES LEAID, CHARTAUTH1, CHARTAUTH2; NTD reported_by_ntd_id"),
        Column("to_name", "the linked unit's name as published alongside the ID", "copy",
               "Census PARENT_UNIT_NAME; F-33 NAME (the school system's name); NCES LEA_NAME, CHARTAUTHN1, CHARTAUTHN2; "
               "NTD reported_by_name"),
        Column("to_record_id", "the record holding exactly that ID, when exactly one does", "id_lookup", "standard.records.record_id"),
        Column("to_records", "how many records hold that ID", "id_lookup", "standard.records"),
        Column("list", "the raw table the link is published in", "bookkeeping"),
        Column("source_row", "the row of that raw table", "copy", "raw.<list>._row"),
        Column("source_columns", "the published columns the link is read from", "bookkeeping"),
        Column("edition", "the edition of the list, as the agency names it", "copy", "raw.<list>._edition"),
    ),
    inputs=tuple(f"raw.{t}" for t in (DEP, PEN, F33, SCHOOL, NTD)) + ("standard.records",),
))


def _links(table: str, link_type: str, from_scheme: str, from_id: str, to_scheme: str, to_id: str, to_name: str,
           where: str = "") -> str:
    columns = ", ".join(c.strip('"') for c in (from_id, to_id, to_name) if not c.startswith("'"))
    return f"""SELECT '{link_type}' AS link_type, '{from_scheme}' AS from_scheme, {from_id} AS from_agency_id,
                      '{to_scheme}' AS to_scheme, {to_id} AS to_agency_id, {to_name} AS to_name,
                      '{table}' AS list, _row AS source_row, '{columns}' AS source_columns, _edition AS edition
               FROM {tables.latest('raw', table)} WHERE {to_id} IS NOT NULL {where}"""


def build(run_id: str) -> dict[str, int]:
    con = duckdb.connect()
    in_scope = f"AND _row IN (SELECT source_row FROM {tables.latest('standard', 'records')} WHERE list = '{SCHOOL}')"
    parts = [
        _links(DEP, "run_by", "census_pid6", "CENSUS_ID_PID6", "census_pid6", "PARENT_CENSUS_ID_PID6", "PARENT_UNIT_NAME"),
        _links(PEN, "run_by", "census_pid6", "CENSUS_ID_PID6", "census_pid6", "PARENT_CENSUS_ID_PID6", "PARENT_UNIT_NAME"),
        _links(F33, "same_school_system", "census_pid6", "PID6", "nces_leaid", "NCESID", "NAME"),
        _links(SCHOOL, "school_of_district", "nces_ncessch", "NCESSCH", "nces_leaid", "LEAID", "LEA_NAME", in_scope),
        _links(SCHOOL, "charter_authorizer", "nces_ncessch", "NCESSCH", "state_charter_authorizer_code", "CHARTAUTH1", "CHARTAUTHN1", in_scope),
        _links(SCHOOL, "charter_authorizer", "nces_ncessch", "NCESSCH", "state_charter_authorizer_code", "CHARTAUTH2", "CHARTAUTHN2", in_scope),
        _links(NTD, "reports_through", "ntd_id", "ntd_id", "ntd_id", "reported_by_ntd_id", "reported_by_name"),
    ]
    select = f"""
        WITH recs AS {tables.latest('standard', 'records')},
        ids AS (
            SELECT id_scheme AS scheme, agency_id AS id, record_id FROM recs WHERE agency_id IS NOT NULL
            UNION ALL  -- the Census's ID layout: a finance unit's ID holds its PID6 in positions 7-12
            SELECT 'census_pid6', substr(agency_id, 7, 6), record_id FROM recs WHERE id_scheme = 'census_government_id'
        ),
        holders AS (SELECT scheme, id, count(*) AS n, min(record_id) AS record_id FROM ids GROUP BY ALL),
        published AS ({' UNION ALL '.join(parts)})
        SELECT p.link_type, p.from_scheme, p.from_agency_id,
               coalesce(r.record_id, CASE WHEN f.n = 1 THEN f.record_id END) AS from_record_id,
               CASE WHEN r.record_id IS NOT NULL THEN 1 ELSE coalesce(f.n, 0) END AS from_records,
               p.to_scheme, p.to_agency_id, p.to_name,
               CASE WHEN t.n = 1 THEN t.record_id END AS to_record_id, coalesce(t.n, 0) AS to_records,
               p.list, p.source_row, p.source_columns, p.edition
        FROM published p
        LEFT JOIN recs r ON r.list = p.list AND r.source_row = p.source_row
        LEFT JOIN holders f ON f.scheme = p.from_scheme AND f.id = p.from_agency_id
        LEFT JOIN holders t ON t.scheme = p.to_scheme AND t.id = p.to_agency_id
        ORDER BY p.link_type, p.list, p.source_row, p.source_columns"""
    inputs = {f"raw.{t}": tables.latest_run(con, "raw", t) for t in (DEP, PEN, F33, SCHOOL, NTD)}
    inputs["standard.records"] = tables.latest_run(con, "standard", "records")
    return {SPEC.key: tables.write(con, SPEC, select, run_id, inputs)}

