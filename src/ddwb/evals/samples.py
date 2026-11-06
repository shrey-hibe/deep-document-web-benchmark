"""Frozen samples for the wide-tier Jev tests, drawn from the warehouse and saved as ID lists in evals/samples/.

A draw is deterministic (bodies ordered by sha1 of their ID and the set's seed) and is saved once; later builds read
the saved list, so a sample does not move when the data changes. Every set leaves out the sealed test sets' frames
(work/registry/draw/DRAW_RECORD.md, without opening sealed/): all California bodies (set B) and cities and towns in
TX, IL, NY and OR (set C).

Sets:
- N (never had a website): bodies with no website from any source, by kind.
- R (relation rules): candidate links from labels.relation_candidates, by rule.
- W (website found): bodies whose chosen website answered in the live check, 30 per source group (agency list, .gov, SAM,
  Wikidata, mail domain, state or other list).
- S (had a website, all dead): bodies whose every known website failed the live check.
- N2 (fresh): 50 more bodies from N's frame under a new seed, leaving out every body in N, S and W, to confirm on unseen
  bodies the rules learned on N and S.

Run: uv run python -m ddwb.evals.samples N R   (W and S once the live check has finished)
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

from .. import paths, tables

SAMPLES = paths.REPO / "evals" / "samples"
SEED = "wide-tier-2026-10-31"
SEALED_FRAMES = """NOT (b.state = 'CA' OR (b.state IN ('TX', 'IL', 'NY', 'OR') AND b.entity_type IN ('municipality', 'town_or_township')))"""

N_STRATA = {"water_system": 25, "fire_department": 25, "special_district": 20, "town_or_township": 15, "library_system": 15}
R_PER_RULE = 50
W_PER_SOURCE = 30
S_TOTAL = 50
# N2: fresh bodies from N's frame, to confirm rules found on N and S (the name check of 2026-10-31) on bodies they never saw
N2_SEED = "wide-tier-fresh-2026-10-31"
N2_STRATA = {"water_system": 13, "fire_department": 12, "special_district": 10, "town_or_township": 8, "library_system": 7}


def _ordered(key: str, seed: str = SEED) -> str:
    return f"sha1({key} || ':{seed}')"


def draw_n(con: duckdb.DuckDBPyConnection, name: str = "N", strata: dict = N_STRATA, seed: str = SEED, exclude: str = "") -> str:
    cases = " UNION ALL ".join(
        f"""(SELECT '{name}' AS sample, '{kind}' AS stratum, b.body_id, b.name, b.entity_type, b.state, b.anchor_record_id
             FROM {tables.latest('entities', 'bodies')} b
             JOIN {tables.latest('standard', 'records')} r ON r.record_id = b.anchor_record_id
             WHERE b.website IS NULL AND b.entity_type = '{kind}' AND {SEALED_FRAMES}
               AND (b.entity_type <> 'water_system' OR r.owner_type_code = 'L') {exclude}
             ORDER BY {_ordered('b.body_id', seed)} LIMIT {n})"""
        for kind, n in strata.items())
    return f"SELECT *, row_number() OVER (PARTITION BY stratum ORDER BY {_ordered('body_id', seed)}) AS draw_rank FROM ({cases})"


def draw_n2(con: duckdb.DuckDBPyConnection) -> str:
    used = " UNION ".join(f"SELECT body_id FROM read_csv('{SAMPLES / f'{s}.csv'}', all_varchar = true)" for s in ("N", "S", "W"))
    return draw_n(con, "N2", N2_STRATA, N2_SEED, f"AND b.body_id NOT IN ({used})")


def draw_r(con: duckdb.DuckDBPyConnection) -> str:
    rules = [r[0] for r in con.execute(f"SELECT DISTINCT rule FROM {tables.latest('labels', 'relation_candidates')} ORDER BY 1").fetchall()]
    cases = " UNION ALL ".join(
        f"""(SELECT 'R' AS sample, c.rule AS stratum, c.from_record_id, c.relation, c.to_record_id, f.name AS from_name, t.name AS to_name,
                    f.state, f.entity_type AS from_type, t.entity_type AS to_type
             FROM {tables.latest('labels', 'relation_candidates')} c
             JOIN {tables.latest('standard', 'records')} f ON f.record_id = c.from_record_id
             JOIN {tables.latest('standard', 'records')} t ON t.record_id = c.to_record_id
             WHERE c.rule = '{rule}' AND f.state <> 'CA'
               AND NOT (f.state IN ('TX', 'IL', 'NY', 'OR') AND (f.entity_type IN ('municipality', 'town_or_township')
                                                                 OR t.entity_type IN ('municipality', 'town_or_township')))
             ORDER BY {_ordered("c.from_record_id || c.to_record_id")} LIMIT {R_PER_RULE})"""
        for rule in rules)
    return (f"SELECT *, row_number() OVER (PARTITION BY stratum ORDER BY {_ordered('from_record_id || to_record_id')}) AS draw_rank "
            f"FROM ({cases})")


def draw_w(con: duckdb.DuckDBPyConnection) -> str:
    fetches = tables.latest("web", "fetches")
    return f"""
        WITH chosen AS (
            SELECT b.body_id, b.name, b.entity_type, b.state, w.website, w.host, w.source
            FROM {tables.latest('entities', 'bodies')} b
            JOIN {tables.latest('entities', 'websites')} w ON w.body_id = b.body_id AND w.rank = 1
            JOIN {fetches} f ON f.host = w.host AND f.status < 400 AND NOT f.bot_wall AND NOT f.parked
            WHERE {SEALED_FRAMES} AND w.quality = 'ok'),
        grouped AS (SELECT *, CASE WHEN source LIKE 'record:%' THEN 'agency_list'
                                   WHEN source = 'claim:cisa_dotgov_domains' THEN 'dotgov'
                                   WHEN source = 'claim:sam_entity_public_2026_10' THEN 'sam'
                                   WHEN source LIKE 'claim:wd_%' THEN 'wikidata'
                                   WHEN source IN ('claim:il_comptroller_units', 'claim:epa_sdwis_email_domains') THEN 'email_domain'
                                   ELSE 'state_or_other_list' END AS source_group FROM chosen),
        ranked AS (SELECT *, row_number() OVER (PARTITION BY source_group ORDER BY {_ordered('body_id')}) AS draw_rank FROM grouped)
        SELECT 'W' AS sample, source_group AS stratum, body_id, name, entity_type, state, website, host, source, draw_rank
        FROM ranked WHERE draw_rank <= {W_PER_SOURCE}"""


def draw_s(con: duckdb.DuckDBPyConnection) -> str:
    fetches = tables.latest("web", "fetches")
    return f"""
        WITH known AS (
            SELECT w.body_id, bool_or(f.status < 400 AND NOT f.bot_wall AND NOT f.parked) AS any_ok, count(f.host) AS checked
            FROM {tables.latest('entities', 'websites')} w LEFT JOIN {fetches} f ON f.host = w.host GROUP BY 1),
        dead AS (
            SELECT b.body_id, b.name, b.entity_type, b.state, b.website
            FROM {tables.latest('entities', 'bodies')} b JOIN known k USING (body_id)
            WHERE NOT k.any_ok AND k.checked > 0 AND {SEALED_FRAMES})
        SELECT 'S' AS sample, entity_type AS stratum, body_id, name, entity_type, state, website,
               row_number() OVER (ORDER BY {_ordered('body_id')}) AS draw_rank
        FROM dead ORDER BY draw_rank LIMIT {S_TOTAL}"""


DRAWS = {"N": draw_n, "R": draw_r, "W": draw_w, "S": draw_s, "N2": draw_n2}


def draw(name: str) -> Path:
    """Draw a set and save it, unless it was saved before (a frozen sample is never redrawn silently)."""
    out = SAMPLES / f"{name}.csv"
    if out.exists():
        raise FileExistsError(f"{out} exists: samples are frozen; delete it on purpose to redraw")
    SAMPLES.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"COPY (SELECT * FROM ({DRAWS[name](con)}) ORDER BY stratum, draw_rank) TO '{out}' (HEADER)")
    return out


if __name__ == "__main__":
    for name in sys.argv[1:]:
        path = draw(name)
        print(path, sum(1 for _ in path.open()) - 1, "rows")
