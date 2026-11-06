"""labels.relation_candidates: rule-based candidates for 'same body' and 'run by' links that no agency publishes.

Every row is a candidate, not a fact: entities do not merge or parent on them until a rule's precision has been
measured (a Jev sample per rule) and the rule is accepted. Each row names its rule and version and the evidence.

Same body (a Census special district and the EPA, USFA, IMLS or NTD record for the same organisation):
- same_name_zip: the same normalised name (labels.websites.name_key), state and ZIP code; one-to-one.
- same_name_state: the same normalised name and state; one-to-one; the ZIPs differ or one is missing.
- same_published_page: both agencies publish the same web page for the two records, and no other body has it. This
  also catches a unit and its parent (a fire department on its district's page), so it says 'related', and its sample
  decides between same_body and run_by.
Run by (a record and the general-purpose government that runs it):
- named_after_government: the record's name starts 'CITY OF', 'TOWN OF', 'VILLAGE OF', 'BOROUGH OF', 'TOWNSHIP OF' or
  'COUNTY OF' and the rest, cut before words like WATER, FIRE, DEPT, names exactly one Census government in the state.
- imls_legal_basis_area: IMLS says the library's legal basis is a city (CI) or county (CO, CC) and gives its service
  area's Census code; the government with that code.
- ntd_uei_sam: the UEI NTD publishes for a reporter is the SAM registration accepted for a Census government
  (labels.website_matches, uei_exact) other than the reporter itself.
"""

from __future__ import annotations

import re

import duckdb

from .. import catalogue, tables
from ..catalogue import Column
from ..standard import records
from . import websites

VERSION = "v1"
CUT = re.compile(r"\b(WATER|WTR|WATERWORKS|UTILIT\w*|PUBLIC|DEPT|DEPARTMENT|FIRE|VOLUNTEER|VFD|FD|RESCUE|SEWER|SYSTEM|MUNICIPAL|"
                 r"BOARD|COMMISSION|LIBRARY|TRANSIT|TRANSPORTATION|BUS|PWS|W S|W&S|AND)\b.*$")
GOVERNMENT_PREFIX = re.compile(r"^(CITY|TOWN|VILLAGE|BOROUGH|TOWNSHIP|COUNTY) OF ")
OTHER_KINDS = ("water_system", "fire_department", "library_system", "transit_reporter")

SPEC = catalogue.register(catalogue.Table(
    dataset="labels", name="relation_candidates", grain="candidate link between two records (one rule)",
    description="Rule-based candidates for same-body and run-by links that no agency publishes; candidates only "
                "(see ddwb/labels/relations.py)",
    columns=(
        Column("from_record_id", "the record (for run_by: the unit that is run)", "label", "standard.records.record_id"),
        Column("relation", "same_body or run_by", "label"),
        Column("to_record_id", "the other record (for run_by: the government that runs it)", "label", "standard.records.record_id"),
        Column("rule", "the rule that proposes the link (see ddwb/labels/relations.py)", "bookkeeping"),
        Column("rule_version", "the version of the rules", "bookkeeping"),
        Column("evidence", "what the rule compared, as text", "label"),
        Column("status", "candidate: not used for merges or parents until the rule is measured and accepted", "bookkeeping"),
    ),
    inputs=("standard.records", "labels.website_matches", "standard.code_states"),
))


def government_core(name: str | None) -> str | None:
    """'CITY OF CANBY WATER DEPT' -> 'CANBY|CITY': the government a unit is named after, if it is."""
    if not name:
        return None
    text = " ".join(websites._tokens(name))
    match = GOVERNMENT_PREFIX.match(text)
    if not match:
        return None
    core = CUT.sub("", text[match.end():]).strip()
    kind = {"CITY": "CITY", "TOWN": "TOWN", "VILLAGE": "VILLAGE", "BOROUGH": "BOROUGH", "TOWNSHIP": "TOWNSHIP", "COUNTY": "COUNTY"}[match.group(1)]
    return f"{core}|{kind}" if core else None


def build(run_id: str) -> dict[str, int]:
    con = duckdb.connect()
    websites.STATE_NAMES.update(con.execute(f"SELECT postal, upper(state_name) FROM {tables.latest('standard', 'code_states')}").fetchall())
    con.create_function("name_key", websites.name_key, ["VARCHAR", "VARCHAR", "BOOLEAN"], "VARCHAR", null_handling="special")
    con.create_function("government_core", government_core, ["VARCHAR"], "VARCHAR", null_handling="special")
    con.create_function("host", websites.host, ["VARCHAR"], "VARCHAR", null_handling="special")
    recs, matches = tables.latest("standard", "records"), tables.latest("labels", "website_matches")
    kinds = ", ".join(f"'{k}'" for k in OTHER_KINDS)
    select = f"""
        WITH r AS (SELECT *, name_key(name, state, entity_type IN ('municipality', 'town_or_township', 'county')) AS nkey,
                          rtrim(regexp_replace(regexp_replace(lower(trim(website)), '^[a-z]+://', ''), '^www\\.', ''), '/') AS page
                   FROM {recs}),
        districts AS (SELECT * FROM r WHERE entity_type = 'special_district' AND nkey IS NOT NULL),
        others AS (SELECT * FROM r WHERE entity_type IN ({kinds}) AND nkey IS NOT NULL),
        by_name AS (
            SELECT o.record_id AS a, d.record_id AS b, o.zip5 = d.zip5 AS same_zip, o.state, split_part(o.nkey, '|', 1) AS name,
                   count(*) OVER (PARTITION BY o.record_id) AS n_for_other, count(*) OVER (PARTITION BY d.record_id, o.entity_type) AS n_for_district
            FROM others o JOIN districts d ON d.state = o.state AND split_part(d.nkey, '|', 1) = split_part(o.nkey, '|', 1)),
        same_name AS (
            SELECT a AS from_record_id, 'same_body' AS relation, b AS to_record_id,
                   CASE WHEN same_zip THEN 'same_name_zip' ELSE 'same_name_state' END AS rule,
                   'state ' || state || '; name ' || name || CASE WHEN same_zip THEN '; same ZIP' ELSE '; ZIPs differ or missing' END AS evidence
            FROM by_name WHERE n_for_other = 1 AND n_for_district = 1),
        pages AS (SELECT page, count(DISTINCT record_id) AS n FROM r WHERE page IS NOT NULL AND page <> '' GROUP BY 1),
        same_page AS (
            SELECT o.record_id, 'same_body', d.record_id, 'same_published_page', 'both agencies publish ' || o.page
            FROM others o JOIN districts d ON d.page = o.page JOIN pages p ON p.page = o.page AND p.n = 2),
        governments AS (SELECT record_id, state, county_name, nkey FROM r
                        WHERE entity_type IN ('municipality', 'town_or_township', 'county') AND nkey IS NOT NULL),
        named AS (
            SELECT u.record_id AS a, g.record_id AS b, u.state, government_core(u.name) AS core,
                   count(*) OVER (PARTITION BY u.record_id) AS n
            FROM r u JOIN governments g ON g.state = u.state
             AND split_part(g.nkey, '|', 1) = split_part(government_core(u.name), '|', 1)
             AND (split_part(g.nkey, '|', 2) = '' OR split_part(g.nkey, '|', 2) = split_part(government_core(u.name), '|', 2)
                  OR (split_part(g.nkey, '|', 2) IN ('CITY', 'TOWN', 'VILLAGE', 'BOROUGH') AND split_part(government_core(u.name), '|', 2) IN ('CITY', 'TOWN', 'VILLAGE', 'BOROUGH')))
            WHERE u.entity_type IN ({kinds}) AND government_core(u.name) IS NOT NULL),
        run_by_name AS (SELECT a, 'run_by', b, 'named_after_government', 'state ' || state || '; named after ' || core FROM named WHERE n = 1),
        libraries AS (
            SELECT l.record_id, g.record_id AS gov, l.legal_basis_code, l.service_area_type, l.service_area_geoid
            FROM r l JOIN r g ON l.list = '{records.IMLS}' AND (
                 (l.legal_basis_code = 'CI' AND l.service_area_type = 'PLACE' AND g.place_fips = l.service_area_geoid
                  AND g.entity_type IN ('municipality', 'town_or_township'))
              OR (l.legal_basis_code = 'CI' AND l.service_area_type = 'COUNTY SUBDIVISION' AND length(l.service_area_geoid) = 10
                  AND g.place_fips = left(l.service_area_geoid, 2) || right(l.service_area_geoid, 5)
                  AND g.entity_type IN ('municipality', 'town_or_township'))
              OR (l.legal_basis_code IN ('CO', 'CC') AND l.service_area_type = 'COUNTY' AND g.county_fips5 = l.service_area_geoid
                  AND g.entity_type = 'county'))
            QUALIFY count(*) OVER (PARTITION BY l.record_id) = 1),
        run_by_area AS (SELECT record_id, 'run_by', gov, 'imls_legal_basis_area',
                               'IMLS legal basis ' || legal_basis_code || '; service area ' || service_area_type || ' ' || service_area_geoid
                        FROM libraries),
        run_by_uei AS (
            SELECT DISTINCT t.record_id, 'run_by', g.record_id, 'ntd_uei_sam',
                   'NTD UEI ' || t.ueid || ' is SAM claim ' || m1.claim_id || ', accepted by name for this government'
            FROM {matches} m1 JOIN {matches} m2 ON m2.claim_id = m1.claim_id AND m2.method = 'name_place' AND m2.status = 'accepted'
            JOIN r t ON t.record_id = m1.record_id AND t.list = '{records.NTD}'
            JOIN r g ON g.record_id = m2.record_id AND g.entity_type IN ('municipality', 'town_or_township', 'county')
            WHERE m1.method = 'uei_exact')
        SELECT *, '{VERSION}' AS rule_version, 'candidate' AS status FROM (
            SELECT * FROM same_name UNION ALL SELECT * FROM same_page UNION ALL SELECT * FROM run_by_name
            UNION ALL SELECT * FROM run_by_area UNION ALL SELECT * FROM run_by_uei)
        WHERE from_record_id <> to_record_id
        ORDER BY rule, from_record_id, to_record_id"""
    inputs = {"standard.records": tables.latest_run(con, "standard", "records"),
              "labels.website_matches": tables.latest_run(con, "labels", "website_matches"),
              "standard.code_states": tables.latest_run(con, "standard", "code_states")}
    columns = "from_record_id, relation, to_record_id, rule, rule_version, evidence, status"
    return {SPEC.key: tables.write(con, SPEC, f"SELECT {columns} FROM ({select})", run_id, inputs)}
