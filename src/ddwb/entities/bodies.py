"""Bodies, built from standard.records, the links the agencies publish (standard.links) and accepted labels.

- Same body: two records are one body only when an agency says so. The Census F-33 file links a Census school
  system to its NCES district; NTD lists one reporter once per program it reports through (the same NTD ID). These
  links are joined as a graph (connected components), and a component never holds two Census governments.
- body_id: 'b' + the first 12 hex characters of sha1 of the body's anchor record, the member listed first by
  ANCHOR_ORDER (then the lowest record_id). It stays the same while the anchor does.
- Parent: the body that runs this one (run_by, Census) or, for a charter school, its district (school_of_district,
  NCES). Only links an agency publishes set parents; root, depth and counts follow the tree.
- Website: from the records' own lists and accepted labels.website_matches, chosen by WEBSITE_ORDER until a live
  check can choose; every candidate is kept in entities.websites.
"""

from __future__ import annotations

import csv
import hashlib
import tempfile
from pathlib import Path

import duckdb

from .. import catalogue, tables
from ..catalogue import Column
from ..standard import records, site_claims

ANCHOR_ORDER = (records.GP, records.SD, records.SCH, records.DEP, records.PEN, records.FIN, records.LEA, records.SCHOOL,
                records.EPA, records.IMLS, records.USFA, records.NTD)
WEBSITE_ORDER = (f"record:{records.LEA}", f"record:{records.SCHOOL}", f"claim:{site_claims.DOTGOV}", "record:*",
                 f"claim:{site_claims.SAM}", f"claim:{site_claims.TEXAS}")
SAME_BODY = ("same_school_system",)  # standard.links types that make two records one body
PARENT = ("run_by", "school_of_district")  # in order of precedence

# Identifiers each record carries beyond its own agency ID: (value, scheme, issuer, the lists it applies to).
# IMLS's state library IDs, like USFA's FDIDs, are unique only within a state, so the state goes in front.
EXTRA_IDS = (("r.state_agency_id", "state_leaid", "the state (published by NCES)", (records.LEA,)),
             ("r.state_agency_id", "state_schid", "the state (published by NCES)", (records.SCHOOL,)),
             ("r.state || '-' || r.state_agency_id", "state_libid", "the state library agency (published by IMLS)", (records.IMLS,)),
             ("r.ueid", "sam_uei", "GSA (SAM.gov; published by NTD)", (records.NTD,)))
ISSUER = {"census_pid6": "Census Bureau", "census_government_id": "Census Bureau", "nces_leaid": "NCES", "nces_ncessch": "NCES",
          "epa_pwsid": "EPA", "imls_fscskey": "IMLS", "usfa_fdid": "USFA", "ntd_id": "FTA"}

BODY_RECORDS = catalogue.register(catalogue.Table(
    dataset="entities", name="body_records", grain="record, with the body it belongs to",
    description="Which records form each body, and the published link that joined them",
    columns=(
        Column("record_id", "the record", "copy", "standard.records.record_id"),
        Column("body_id", "the body it belongs to", "bookkeeping"),
        Column("is_anchor", "whether this record names the body (the first member by list order)", "bookkeeping"),
        Column("joined_by", "anchor, or the link that joined the record to the body: same_school_system (Census F-33), "
               "same_ntd_id (NTD lists one reporter per program)", "bookkeeping", "standard.links"),
        Column("joined_by_source", "the list and row of that link", "bookkeeping", "standard.links.list, source_row"),
    ),
    inputs=("standard.records", "standard.links"),
))

BODIES = catalogue.register(catalogue.Table(
    dataset="entities", name="bodies", grain="public body",
    description="One row per body: its anchor record's name and kind, its parent and place in the tree, and a website",
    columns=(
        Column("body_id", "this repo's ID for the body", "bookkeeping"),
        Column("name", "the anchor record's name", "copy", "standard.records.name"),
        Column("entity_type", "the anchor record's kind", "copy", "standard.records.entity_type"),
        Column("entity_types", "every kind among the body's records, comma-separated", "copy", "standard.records.entity_type"),
        Column("state", "the anchor record's state", "copy", "standard.records.state"),
        Column("county_fips5", "the first county FIPS code among the records, anchor first", "copy", "standard.records.county_fips5"),
        Column("records", "how many records form the body", "bookkeeping"),
        Column("lists", "the lists the body appears in, comma-separated", "bookkeeping"),
        Column("anchor_record_id", "the record that names the body", "bookkeeping"),
        Column("parent_body_id", "the body that runs it (run_by) or, for a charter school, its district", "id_lookup", "entities.relations"),
        Column("parent_relation", "run_by or school_of_district", "bookkeeping"),
        Column("root_body_id", "the top of its tree (itself when it has no parent)", "id_lookup", "entities.relations"),
        Column("depth", "0 at the root, 1 for its children, ...", "bookkeeping"),
        Column("children_count", "how many bodies have it as parent", "bookkeeping"),
        Column("descendants_count", "how many bodies sit under it, at any depth", "bookkeeping"),
        Column("is_parent", "whether any body has it as parent", "bookkeeping"),
        Column("is_child", "whether it has a parent", "bookkeeping"),
        Column("website", "the chosen website (first by the order in ddwb/entities/bodies.py, until a live check can choose)",
               "label", "entities.websites"),
        Column("website_source", "where the chosen website comes from: record:<list> or claim:<list>", "bookkeeping"),
        Column("websites_known", "how many different hosts are known for the body", "bookkeeping"),
    ),
    inputs=("entities.body_records", "entities.relations", "entities.websites", "standard.records"),
))

IDENTIFIERS = catalogue.register(catalogue.Table(
    dataset="entities", name="identifiers", grain="ID an issuer gives a body",
    description="Every ID the body's records carry, with who issues it",
    columns=(
        Column("body_id", "the body", "bookkeeping"),
        Column("scheme", "the ID system: census_pid6, census_government_id, nces_leaid, nces_ncessch, epa_pwsid, imls_fscskey, "
               "usfa_fdid (state-FDID), ntd_id, state_leaid, state_schid, state_libid (state-LIBID), sam_uei", "bookkeeping"),
        Column("value", "the ID", "copy", "standard.records.agency_id, state_agency_id, ueid"),
        Column("issuer", "who issues the ID", "bookkeeping"),
        Column("record_id", "the record the ID comes from (the lowest, when several of the body's records carry it)", "copy",
               "standard.records.record_id"),
        Column("shared", "whether another body carries the same ID (a SAM UEI can be the operating government's)", "bookkeeping"),
    ),
    inputs=("entities.body_records", "standard.records"),
))

RELATIONS = catalogue.register(catalogue.Table(
    dataset="entities", name="relations", grain="link between two bodies",
    description="Links between bodies. Accepted links come from the agencies (standard.links); parents come only from these. "
                "Candidate links come from rules (labels.relation_candidates) and wait for their rule to be measured",
    columns=(
        Column("from_body_id", "the body the link starts at", "id_lookup", "standard.links.from_record_id"),
        Column("relation", "run_by, school_of_district, reports_through or same_body", "copy",
               "standard.links.link_type, labels.relation_candidates.relation"),
        Column("to_body_id", "the body it points to", "id_lookup", "standard.links.to_record_id"),
        Column("status", "accepted (an agency publishes it) or candidate (a rule proposes it)", "bookkeeping"),
        Column("source", "the list, row and columns that publish it", "bookkeeping", "standard.links.list, source_row, source_columns"),
    ),
    inputs=("entities.body_records", "standard.links", "labels.relation_candidates"),
))

WEBSITES = catalogue.register(catalogue.Table(
    dataset="entities", name="websites", grain="website known for a body (one host per body and source)",
    description="Every website known for a body: from its records' own lists and from accepted website labels",
    columns=(
        Column("body_id", "the body", "bookkeeping"),
        Column("website", "the website as published", "copy", "standard.records.website, labels.website_matches.website"),
        Column("host", "lower case, without 'www.'", "label", "labels.websites.host"),
        Column("source", "record:<list> (the agency's own list) or claim:<list> (an accepted label)", "bookkeeping"),
        Column("record_id", "the record it comes with", "copy", "standard.records.record_id"),
        Column("quality", "ok, or why the website is a poor pick: not_a_domain, social_page, webmail, dead_platform (e.g. "
               "geocities), shared_page (the same page serves more than 2 bodies: a law firm, tax collector, directory or a "
               "government listing its units; a page per body on a shared platform is fine)", "label"),
        Column("rank", "1 for the chosen website: quality ok first, then the source order in ddwb/entities/bodies.py", "bookkeeping"),
    ),
    inputs=("entities.body_records", "standard.records", "labels.website_matches"),
))


class _Components:
    """Union-find over record IDs."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        self.parent[self.find(a)] = self.find(b)


def _body_records(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    recs = con.execute(f"SELECT record_id, list, agency, id_scheme, agency_id FROM {tables.latest('standard', 'records')}").fetchall()
    info = {r[0]: r for r in recs}
    edges = con.execute(f"""SELECT from_record_id, to_record_id, link_type, list || '#' || source_row FROM {tables.latest('standard', 'links')}
                            WHERE link_type IN ({', '.join(f"'{t}'" for t in SAME_BODY)})
                              AND from_record_id IS NOT NULL AND to_record_id IS NOT NULL""").fetchall()
    # NTD lists a reporter once per program: rows with the same NTD ID are one reporter.
    by_ntd: dict[str, list[str]] = {}
    for record_id, list_name, _, _, agency_id in recs:
        if list_name == records.NTD:
            by_ntd.setdefault(agency_id, []).append(record_id)
    edges += [(ids[0], other, "same_ntd_id", f"{records.NTD}:ntd_id {ntd}") for ntd, ids in by_ntd.items() for other in ids[1:]]

    components, census, joined = _Components(), {}, {}
    for record_id in info:
        components.find(record_id)
        census[record_id] = {record_id} if info[record_id][2] == "Census" else set()
    for a, b, how, source in sorted(edges):
        ra, rb = components.find(a), components.find(b)
        if ra == rb or (census[ra] and census[rb]):
            continue  # already one body, or the link would put two Census governments in one body
        components.union(a, b)
        census[components.find(b)] = census[ra] | census[rb]
        joined.setdefault(a, (how, source))
        joined.setdefault(b, (how, source))

    members: dict[str, list[str]] = {}
    for record_id in info:
        members.setdefault(components.find(record_id), []).append(record_id)
    order = {name: i for i, name in enumerate(ANCHOR_ORDER)}
    rows = []
    for ids in members.values():
        anchor = min(ids, key=lambda r: (order.get(info[r][1], len(order)), r))
        body_id = "b" + hashlib.sha1(anchor.encode()).hexdigest()[:12]
        for record_id in ids:
            how, source = ("anchor", None) if record_id == anchor else joined.get(record_id, ("anchor", None))
            rows.append((record_id, body_id, record_id == anchor, how, source))
    return rows


def build(run_id: str) -> dict[str, int]:
    con = duckdb.connect()
    with tempfile.NamedTemporaryFile("w", suffix=".csv", newline="", delete=False) as handle:
        csv.writer(handle).writerows(_body_records(con))
    con.execute(f"""CREATE TEMP TABLE members AS SELECT * FROM read_csv('{handle.name}', header = false,
                    columns = {{'record_id': 'VARCHAR', 'body_id': 'VARCHAR', 'is_anchor': 'BOOLEAN', 'joined_by': 'VARCHAR',
                                'joined_by_source': 'VARCHAR'}})""")
    Path(handle.name).unlink()
    counts = {BODY_RECORDS.key: tables.write(con, BODY_RECORDS, "SELECT * FROM members ORDER BY body_id, NOT is_anchor, record_id",
                                              run_id, {"standard.records": tables.latest_run(con, "standard", "records"),
                                                       "standard.links": tables.latest_run(con, "standard", "links")})}
    recs, links, matches = (tables.latest("standard", "records"), tables.latest("standard", "links"),
                            tables.latest("labels", "website_matches"))
    members = tables.latest("entities", "body_records")

    relations = f"""
        SELECT DISTINCT f.body_id AS from_body_id, l.link_type AS relation, t.body_id AS to_body_id, 'accepted' AS status,
               l.list || '#' || l.source_row || ' (' || l.source_columns || ')' AS source
        FROM {links} l JOIN {members} f ON f.record_id = l.from_record_id JOIN {members} t ON t.record_id = l.to_record_id
        WHERE l.link_type IN ('run_by', 'school_of_district', 'reports_through') AND f.body_id <> t.body_id
        UNION
        SELECT DISTINCT f.body_id, c.relation, t.body_id, 'candidate', 'rule ' || c.rule || ' ' || c.rule_version || ': ' || c.evidence
        FROM {tables.latest('labels', 'relation_candidates')} c JOIN {members} f ON f.record_id = c.from_record_id
        JOIN {members} t ON t.record_id = c.to_record_id
        WHERE f.body_id <> t.body_id
        ORDER BY 1, 2, 3"""
    upstream = {"entities.body_records": run_id, "standard.links": tables.latest_run(con, "standard", "links"),
                "labels.relation_candidates": tables.latest_run(con, "labels", "relation_candidates")}
    counts[RELATIONS.key] = tables.write(con, RELATIONS, relations, run_id, upstream)

    rank = " ".join(f"WHEN source {'LIKE' if o.endswith('*') else '='} '{o.replace('*', '%')}' THEN {i}"
                    for i, o in enumerate(WEBSITE_ORDER, start=1))
    websites = f"""
        WITH known AS (
            SELECT m.body_id, r.website, regexp_replace(regexp_replace(lower(trim(r.website)), '^[a-z]+://', ''), '^www\\.', '') AS raw_host,
                   'record:' || r.list AS source, r.record_id
            FROM {recs} r JOIN {members} m USING (record_id) WHERE r.website IS NOT NULL
            UNION ALL
            SELECT m.body_id, w.website, w.host, 'claim:' || split_part(w.claim_id, '#', 1), w.record_id
            FROM {matches} w JOIN {members} m USING (record_id) WHERE w.status = 'accepted'),
        hosted AS (SELECT * EXCLUDE (raw_host), regexp_extract(raw_host, '^[^/?#\\s]+') AS host,
                          rtrim(regexp_replace(raw_host, '[?#].*$', ''), '/') AS page FROM known),
        one AS (SELECT * FROM hosted QUALIFY row_number() OVER (PARTITION BY body_id, host, source ORDER BY record_id) = 1),
        rated AS (
            SELECT *, CASE
                WHEN host IS NULL OR host NOT LIKE '%.%' OR length(host) < 4 THEN 'not_a_domain'
                WHEN regexp_matches(host, '(^|\.)(facebook|fb|twitter|x|instagram|linkedin|youtube|nextdoor|tiktok)\.com$') THEN 'social_page'
                WHEN regexp_matches(host, '(^|\.)(gmail|yahoo|hotmail|outlook|aol|comcast|att|sbcglobal|msn|live)\.(com|net)$') THEN 'webmail'
                WHEN regexp_matches(host, '(^|\.)(geocities|angelfire|tripod|homestead|freewebs)\.com$') THEN 'dead_platform'
                WHEN count(DISTINCT body_id) OVER (PARTITION BY page) > 2 THEN 'shared_page'
                ELSE 'ok' END AS quality
            FROM one)
        SELECT body_id, website, host, source, record_id, quality,
               row_number() OVER (PARTITION BY body_id ORDER BY quality <> 'ok', CASE {rank} END, record_id, host) AS rank
        FROM rated ORDER BY body_id, rank"""
    upstream = {"entities.body_records": run_id, "standard.records": tables.latest_run(con, "standard", "records"),
                "labels.website_matches": tables.latest_run(con, "labels", "website_matches")}
    counts[WEBSITES.key] = tables.write(con, WEBSITES, websites, run_id, upstream)

    identifiers = f"""
        WITH ids AS (
            SELECT m.body_id, r.id_scheme AS scheme,
                   CASE WHEN r.id_scheme = 'usfa_fdid' THEN r.state || '-' || r.agency_id ELSE r.agency_id END AS value, r.record_id
            FROM {recs} r JOIN {members} m USING (record_id) WHERE r.agency_id IS NOT NULL
            {' '.join(f"UNION ALL SELECT m.body_id, '{scheme}', {value}, r.record_id FROM {recs} r JOIN {members} m USING (record_id) "
                      f"WHERE {value} IS NOT NULL AND r.list IN ({', '.join(repr(x) for x in lists)})"
                      for value, scheme, _, lists in EXTRA_IDS)}),
        one AS (SELECT body_id, scheme, value, min(record_id) AS record_id FROM ids GROUP BY ALL)
        SELECT body_id, scheme, value,
               CASE scheme {' '.join(f"WHEN '{s}' THEN '{i}'" for s, i in ISSUER.items())}
                           {' '.join(f"WHEN '{s}' THEN '{i}'" for _, s, i, _ in EXTRA_IDS)} END AS issuer,
               record_id, count(*) OVER (PARTITION BY scheme, value) > 1 AS shared
        FROM one ORDER BY body_id, scheme, value"""
    upstream = {"entities.body_records": run_id, "standard.records": tables.latest_run(con, "standard", "records")}
    counts[IDENTIFIERS.key] = tables.write(con, IDENTIFIERS, identifiers, run_id, upstream)

    rels = tables.latest("entities", "relations")
    parents = dict(con.execute(f"""
        SELECT from_body_id, arg_min(to_body_id || '|' || relation, CASE relation {' '.join(f"WHEN '{p}' THEN {i}" for i, p in enumerate(PARENT))} END)
        FROM {rels} WHERE status = 'accepted' AND relation IN ({', '.join(f"'{p}'" for p in PARENT)}) GROUP BY 1""").fetchall())
    tree = []
    children: dict[str, int] = {}
    for child, value in parents.items():
        children[value.split("|")[0]] = children.get(value.split("|")[0], 0) + 1
    descendants: dict[str, int] = {}
    for child in parents:
        node, depth, seen = child, 0, {child}
        while node in parents:
            node = parents[node].split("|")[0]
            if node in seen:
                break  # a cycle: stop climbing
            seen.add(node)
            depth += 1
            descendants[node] = descendants.get(node, 0) + 1
        tree.append((child, parents[child].split("|")[0], parents[child].split("|")[1], node, depth))
    con.execute("CREATE TEMP TABLE tree (body_id VARCHAR, parent_body_id VARCHAR, parent_relation VARCHAR, root_body_id VARCHAR, depth INTEGER)")
    con.executemany("INSERT INTO tree VALUES (?, ?, ?, ?, ?)", tree)
    con.execute("CREATE TEMP TABLE counts (body_id VARCHAR, children_count INTEGER, descendants_count INTEGER)")
    con.executemany("INSERT INTO counts VALUES (?, ?, ?)", [(b, children.get(b, 0), descendants.get(b, 0))
                                                             for b in set(children) | set(descendants)])
    order = " ".join(f"WHEN '{name}' THEN {i}" for i, name in enumerate(ANCHOR_ORDER))
    bodies = f"""
        WITH m AS (SELECT m.*, r.* EXCLUDE (record_id), CASE r.list {order} END AS list_order
                   FROM {members} m JOIN {recs} r USING (record_id)),
        agg AS (
            SELECT body_id, any_value(name) FILTER (WHERE is_anchor) AS name, any_value(entity_type) FILTER (WHERE is_anchor) AS entity_type,
                   string_agg(DISTINCT entity_type, ', ' ORDER BY entity_type) AS entity_types,
                   any_value(state) FILTER (WHERE is_anchor) AS state,
                   arg_min(county_fips5, list_order) FILTER (WHERE county_fips5 IS NOT NULL) AS county_fips5,
                   count(*) AS records, string_agg(DISTINCT list, ', ' ORDER BY list) AS lists,
                   any_value(record_id) FILTER (WHERE is_anchor) AS anchor_record_id
            FROM m GROUP BY body_id),
        site AS (SELECT body_id, any_value(website) FILTER (WHERE rank = 1) AS website, any_value(source) FILTER (WHERE rank = 1) AS website_source,
                        count(DISTINCT host) AS websites_known
                 FROM {tables.latest('entities', 'websites')} GROUP BY body_id)
        SELECT a.body_id, a.name, a.entity_type, a.entity_types, a.state, a.county_fips5, a.records, a.lists, a.anchor_record_id,
               t.parent_body_id, t.parent_relation, coalesce(t.root_body_id, a.body_id) AS root_body_id, coalesce(t.depth, 0) AS depth,
               coalesce(c.children_count, 0) AS children_count, coalesce(c.descendants_count, 0) AS descendants_count,
               coalesce(c.children_count, 0) > 0 AS is_parent, t.parent_body_id IS NOT NULL AS is_child,
               s.website, s.website_source, coalesce(s.websites_known, 0) AS websites_known
        FROM agg a LEFT JOIN tree t USING (body_id) LEFT JOIN counts c USING (body_id) LEFT JOIN site s USING (body_id)
        ORDER BY a.body_id"""
    upstream = {"entities.body_records": run_id, "entities.relations": run_id, "entities.websites": run_id,
                "standard.records": tables.latest_run(con, "standard", "records")}
    counts[BODIES.key] = tables.write(con, BODIES, bodies, run_id, upstream)
    return counts
