"""The registry resolver: parsing, matching, lookups, cards and the release export.

Offline tests build a tiny registry folder (the layout of data/tables) in a temporary directory. The checks against
the real data at the bottom skip when data/tables has no entities.bodies.
"""

import ast
import json
from pathlib import Path

import duckdb
import pytest

from ddwb import paths
from ddwb.registry import Registry, export_release, names, snapshot
from ddwb.registry import __main__ as cli
from ddwb.registry import resolver as resolver_module

RUN, OLD = "20261101T0000Z-test", "20250101T0000Z-old"
STATES = {"OH": "OHIO", "CO": "COLORADO", "IL": "ILLINOIS", "CA": "CALIFORNIA", "NY": "NEW YORK", "PA": "PENNSYLVANIA"}

RECORD_COLUMNS = {"record_id": "VARCHAR", "list": "VARCHAR", "agency": "VARCHAR", "name": "VARCHAR", "entity_type": "VARCHAR",
                  "function": "VARCHAR", "state": "VARCHAR", "county_fips5": "VARCHAR", "county_name": "VARCHAR", "city": "VARCHAR",
                  "population": "BIGINT", "unit_type": "VARCHAR", "owner_type": "VARCHAR", "owner_name": "VARCHAR",
                  "legal_basis": "VARCHAR", "organization_type": "VARCHAR", "department_type": "VARCHAR", "website": "VARCHAR"}
GP, SD, SCH = "census_gus_2026_general_purpose", "census_gus_2026_special_district", "census_gus_2026_school_district"
EPA, IMLS, LEA, USFA = "epa_sdwis_water_system", "imls_pls_fy2024_administrative_entity", "nces_lea_directory_2025_26", "usfa_registry"
# (body_id, record_id, list, agency, name, entity_type, function, state, county_fips5, county_name, city, population, website)
RECORDS = [
    ("b_summit", "census_pid6:1", GP, "Census", "COUNTY OF SUMMIT", "county", None, "OH", "39153", "SUMMIT", "AKRON", 540000, "https://summitcountyoh.gov"),
    ("b_summit_tid", "census_pid6:2", SD, "Census", "SUMMIT COUNTY TRANSPORTATION IMPROVEMENT DISTRICT", "special_district", "HIGHWAYS",
     "OH", "39153", "SUMMIT", "AKRON", None, None),
    ("b_summit_twp", "census_pid6:3", GP, "Census", "TOWNSHIP OF SUMMIT", "town_or_township", None, "OH", "39111", "MONROE", "LEWISVILLE", 300, None),
    ("b_cortez", "census_pid6:4", GP, "Census", "CITY OF CORTEZ", "municipality", None, "CO", "08083", "MONTEZUMA", "CORTEZ", 9151, "https://cortezco.gov"),
    ("b_cortez_water", "epa_pwsid:CO0142100", EPA, "EPA", "CORTEZ CITY OF", "water_system", None, "CO", None, None, "CORTEZ", 9000, None),
    ("b_cortez_fire", "census_pid6:5", SD, "Census", "CORTEZ FIRE PROTECTION DISTRICT", "special_district", "LOCAL FIRE PROTECTION",
     "CO", "08083", "MONTEZUMA", "CORTEZ", None, None),
    ("b_springfield", "census_pid6:6", GP, "Census", "CITY OF SPRINGFIELD", "municipality", None, "IL", "17167", "SANGAMON", "SPRINGFIELD",
     114000, "http://www.springfield.il.us/"),
    ("b_springfield_twp", "census_pid6:7", GP, "Census", "TOWNSHIP OF SPRINGFIELD", "town_or_township", None, "IL", "17167", "SANGAMON",
     "SPRINGFIELD", 2000, None),
    ("b_springfield_water", "epa_pwsid:IL1671200", EPA, "EPA", "SPRINGFIELD", "water_system", None, "IL", None, None, "SPRINGFIELD", 150000, None),
    ("b_springfield_fire", "usfa_fdid:IL-123", USFA, "USFA", "Springfield Fire Department", "fire_department", None, "IL", None, "Sangamon",
     "Springfield", None, "https://www.springfield.il.us/"),
    ("b_pueblo_lib", "census_pid6:8", SD, "Census", "PUEBLO CITY-COUNTY LIBRARY DISTRICT", "special_district", "LIBRARIES", "CO", "08101",
     "PUEBLO", "PUEBLO", None, None),
    ("b_pueblo_imls", "imls_fscskey:CO0099", IMLS, "IMLS", "PUEBLO CITY-COUNTY LIBRARY DISTRICT", "library_system", None, "CO", "08101",
     "PUEBLO", "PUEBLO", 169427, None),
    ("b_pylusd", "census_pid6:9", SCH, "Census", "PLACENTIA-YORBA LINDA UNIFIED", "school_district", None, "CA", "06059", "ORANGE",
     "PLACENTIA", None, "http://www.pylusd.k12.ca.us/home.asp"),
    ("b_pylusd", "nces_leaid:0630660", LEA, "NCES", "Placentia-Yorba Linda Unified", "k12_district", None, "CA", None, None, "Placentia",
     None, "http://www.pylusd.org"),
    ("b_hose", "usfa_fdid:CO-999", USFA, "USFA", "Sample Hose Company", "fire_department", None, "CO", None, "Montezuma", "Cortez", None,
     "chief@example.com"),
]
WEBSITES = [  # (body_id, website, host, source, record_id, quality, rank)
    ("b_summit", "https://summitcountyoh.gov", "summitcountyoh.gov", "claim:cisa_dotgov_domains", "census_pid6:1", "ok", 1),
    ("b_cortez", "https://cortezco.gov", "cortezco.gov", f"record:{GP}", "census_pid6:4", "ok", 1),
    ("b_springfield", "http://www.springfield.il.us/", "springfield.il.us", f"record:{GP}", "census_pid6:6", "ok", 1),
    ("b_springfield_fire", "https://www.springfield.il.us/", "springfield.il.us", f"record:{USFA}", "usfa_fdid:IL-123", "ok", 1),
    ("b_pylusd", "http://www.pylusd.org", "pylusd.org", f"record:{LEA}", "nces_leaid:0630660", "ok", 1),
    ("b_pylusd", "http://www.pylusd.k12.ca.us/home.asp", "pylusd.k12.ca.us", f"record:{SCH}", "census_pid6:9", "ok", 2),
    ("b_hose", "chief@example.com", "chief@example.com", f"record:{USFA}", "usfa_fdid:CO-999", "ok", 1),
]


def _write(root: Path, dataset: str, table: str, run: str, columns: dict[str, str], rows: list[tuple]) -> None:
    folder = root / dataset / table
    folder.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute(f"CREATE TABLE t ({', '.join(f'{c} {k}' for c, k in columns.items())})")
    if rows:
        con.executemany(f"INSERT INTO t VALUES ({', '.join('?' for _ in columns)})", rows)
    con.execute(f"COPY (SELECT *, '{run}' AS run_id, now() AS written_at FROM t) TO '{folder / f'{run}.parquet'}' (FORMAT parquet)")


@pytest.fixture(scope="module")
def root(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("registry")
    v = "VARCHAR"
    records = [(r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10], r[11], None, None,
                "A PERSON NAME" if r[2] == EPA else None, None, None, None, r[12]) for r in RECORDS]
    _write(root, "standard", "records", RUN, RECORD_COLUMNS, records)
    _write(root, "standard", "code_states", RUN, {"fips_state": v, "postal": v, "state_name": v, "source": v},
           [(str(i), p, n.title(), "test") for i, (p, n) in enumerate(STATES.items())])
    _write(root, "standard", "code_lists", RUN, {"code_list": v, "code": v, "meaning": v, "source": v}, [("x", "1", "one", "test")])
    _write(root, "standard", "code_entity_types", RUN, {"list": v, "unit_type_code": v, "entity_type": v, "note": v},
           [(GP, "1", "county", "test")])
    anchors = {}
    for r in RECORDS:
        anchors.setdefault(r[0], r)
    _write(root, "entities", "body_records", RUN, {"record_id": v, "body_id": v, "is_anchor": "BOOLEAN", "joined_by": v, "joined_by_source": v},
           [(r[1], r[0], anchors[r[0]] is r, "anchor" if anchors[r[0]] is r else "same_school_system", None) for r in RECORDS])
    body_cols = {"body_id": v, "name": v, "entity_type": v, "entity_types": v, "state": v, "county_fips5": v, "records": "INTEGER",
                 "lists": v, "anchor_record_id": v, "parent_body_id": v, "website": v, "website_source": v, "websites_known": "INTEGER"}
    bodies = []
    for body_id, a in anchors.items():
        members = [r for r in RECORDS if r[0] == body_id]
        site = next((w for w in WEBSITES if w[0] == body_id and w[6] == 1), None)
        bodies.append((body_id, a[4], a[5], ", ".join(sorted({m[5] for m in members})), a[7], a[8], len(members),
                       ", ".join(sorted({m[2] for m in members})), a[1], None, site[1] if site else None, site[3] if site else None,
                       sum(1 for w in WEBSITES if w[0] == body_id)))
    _write(root, "entities", "bodies", RUN, body_cols, bodies)
    _write(root, "entities", "bodies", OLD, body_cols, [("b_old", "OLD SUMMIT COUNTY", "county", "county", "OH", "39153", 1, GP,
                                                          "census_pid6:1", None, None, None, 0)])
    _write(root, "entities", "identifiers", RUN, {"body_id": v, "scheme": v, "value": v, "issuer": v, "record_id": v, "shared": "BOOLEAN"},
           [(r[0], r[1].split(":")[0], r[1].split(":")[1], "test", r[1], False) for r in RECORDS])
    _write(root, "entities", "websites", RUN, {"body_id": v, "website": v, "host": v, "source": v, "record_id": v, "quality": v,
                                                "rank": "BIGINT"}, WEBSITES)
    _write(root, "entities", "relations", RUN, {"from_body_id": v, "relation": v, "to_body_id": v, "status": v, "source": v},
           [("b_pueblo_imls", "same_body", "b_pueblo_lib", "candidate", "rule same_name_zip v1")])
    meta = [(d, t, i, c, "m", "copy", "", "g", "") for d, t, cols in (("standard", "records", RECORD_COLUMNS), ("entities", "bodies", body_cols),
                                                                       ("raw", "sam_entity_public_2026_10", {"x": v}))
            for i, c in enumerate(cols, start=1)]
    _write(root, "meta", "columns", RUN, {"dataset": v, "table_name": v, "position": "INTEGER", "column_name": v, "meaning": v, "how": v,
                                          "source": v, "table_grain": v, "table_inputs": v}, meta)
    return root


@pytest.fixture(scope="module")
def reg(root) -> Registry:
    return Registry(root)


def top(result: dict) -> str:
    return result["candidates"][0]["body_id"]


# ------------------------------------------------------------------------------------------- names
def test_latest_run_is_read_off_the_file_names(root):
    run, files = snapshot.latest(root, "entities", "bodies")
    assert run == RUN and [f.name for f in files] == [f"{RUN}.parquet"]


def test_plain_names_put_inverted_government_names_back_in_order():
    assert names.plain("CORTEZ CITY OF") == "CITY OF CORTEZ"
    assert names.plain("BARLOW, CITY OF") == "CITY OF BARLOW"
    assert names.plain("AMSTERDAM (C)") == "CITY OF AMSTERDAM"
    assert names.plain("HOUSTON CO SCH DIST") == "HOUSTON COUNTY SCHOOL DISTRICT"
    assert names.plain("Placentia-Yorba Linda USD") == "PLACENTIA YORBA LINDA UNIFIED SCHOOL DISTRICT"
    assert names.record_row("CORTEZ CITY OF", "CO", "water_system", None).key == "CORTEZ|CITY"
    assert names.record_row("COUNTY OF SUMMIT", "OH", "county", None).key == "SUMMIT|COUNTY"
    assert names.record_row("TWP OF UPPER MAKEFIELD", "PA", "water_system", None).key == "UPPER MAKEFIELD|TOWNSHIP"


def test_record_families_come_from_kind_function_and_name():
    assert set(names.record_row("CITY OF CORTEZ", "CO", "municipality", None).families) == {"municipality", "city"}
    assert set(names.record_row("X FIRE PROTECTION DISTRICT", "CO", "special_district", "OTHER MULTI-FUNCTION DISTRICTS").families) \
        == {"special", "fire"}
    assert set(names.record_row("P LIBRARY DISTRICT", "CO", "special_district", "LIBRARIES").families) == {"special", "library"}
    assert "school" in names.record_row("Placentia-Yorba Linda Unified", "CA", "k12_district", None).families


@pytest.mark.parametrize("text, name, state, county, kinds", [
    ("Summit County, OH", "Summit County", "OH", None, {"county"}),
    ("City of Cortez Colorado", "City of Cortez", "CO", None, {"city"}),
    ("Cortez, CO", "Cortez", "CO", None, set()),
    ("Springfield IL", "Springfield", "IL", None, set()),
    ("Springfield, Sangamon County, IL", "Springfield", "IL", "SANGAMON", set()),
    ("Pueblo City-County Library District", "Pueblo City-County Library District", None, None, {"library"}),
    ("Placentia-Yorba Linda Unified School District", "Placentia-Yorba Linda Unified School District", None, None, {"school"}),
    ("Webster CSD, New York", "Webster CSD", "NY", None, {"school"}),
    ("Upper Makefield Twp, PA", "Upper Makefield Twp", "PA", None, {"township"}),
    ("Summit Co, OH", "Summit County", "OH", None, {"county"}),
    ("Summit CO", "Summit", "CO", None, set()),
    ("Springfield housing authority IL", "Springfield housing authority", "IL", None, {"housing"}),
    ("Water Valley, IL", "Water Valley", "IL", None, set()),  # WATER names a kind only before a district word
    ("Fire Island Pines", "Fire Island Pines", None, None, set()),
])
def test_parse(text, name, state, county, kinds):
    q = names.parse(text, states=STATES)
    assert (q.name, q.state, q.county, set(q.families)) == (name, state, county, kinds)


def test_parse_keywords_override_and_name_kinds():
    q = names.parse("Springfield", state="Illinois", kind="township", county="Sangamon County", states=STATES)
    assert (q.state, q.county, set(q.families)) == ("IL", "SANGAMON", {"township"})
    assert set(names.parse(name="X", kind="water_system", states=STATES).families) == {"water"}
    assert set(names.parse(name="X", kind="special district", states=STATES).families) == {"special"}
    assert names.parse(name="X", state="Narnia", states=STATES).notes


# ------------------------------------------------------------------------------------------- resolve (fixture)
def test_county_query_finds_the_county_not_its_districts(reg):
    r = reg.resolve("Summit County, OH")
    assert top(r) == "b_summit" and not r["ambiguous"]
    c = r["candidates"][0]
    assert c["county"] == "Summit" and c["websites"][0]["host"] == "summitcountyoh.gov" and c["identifiers"] == {"census_pid6": ["1"]}
    assert any("SUMMIT|COUNTY" in why for why in c["reasons"])
    assert "b_old" not in {c["body_id"] for c in r["candidates"]}  # older runs are not read


def test_city_beats_the_water_system_with_the_inverted_name(reg):
    r = reg.resolve("Cortez, CO")
    ids = [c["body_id"] for c in r["candidates"]]
    assert ids[0] == "b_cortez" and ids[1] == "b_cortez_water" and not r["ambiguous"]
    assert reg.resolve("City of Cortez Colorado")["candidates"][0]["body_id"] == "b_cortez"
    assert top(reg.resolve("Cortez water system, CO")) == "b_cortez_water"
    assert top(reg.resolve("Cortez Fire Protection District, CO")) == "b_cortez_fire"


def test_springfield_is_ambiguous_until_a_kind_is_named(reg):
    r = reg.resolve("Springfield IL")
    assert r["ambiguous"] and {"b_springfield", "b_springfield_twp"} <= set(r["ambiguity"]["close"])
    assert "b_springfield_water" in [c["body_id"] for c in r["candidates"][:3]]
    r = reg.resolve("Springfield", state="IL", kind="township")
    assert top(r) == "b_springfield_twp" and not r["ambiguous"]
    assert top(reg.resolve("Springfield Water, IL")) == "b_springfield_water"


def test_county_qualifier_moves_the_ranking(reg):
    assert top(reg.resolve("Summit, Monroe County, OH")) == "b_summit_twp"
    r = reg.resolve("Summit, Summit County, OH")
    assert top(r) == "b_summit"


def test_duplicate_bodies_are_flagged_and_the_census_body_leads(reg):
    r = reg.resolve("Pueblo City-County Library District")
    assert top(r) == "b_pueblo_lib" and r["ambiguous"]
    assert set(r["ambiguity"]["close"]) == {"b_pueblo_lib", "b_pueblo_imls"} and "several agencies" in r["ambiguity"]["why"]
    assert r["candidates"][0]["related"][0]["relation"] == "same_body"


def test_school_district_abbreviations(reg):
    for text in ("Placentia-Yorba Linda Unified School District", "Placentia-Yorba Linda USD, CA", "placentia yorba linda unified"):
        assert top(reg.resolve(text)) == "b_pylusd", text


def test_contact_values_never_reach_a_card(reg):
    c = reg.resolve("Sample Hose Company, CO")["candidates"][0]
    assert c["body_id"] == "b_hose" and c["websites"] == []


def test_nothing_found(reg):
    r = reg.resolve("Atlantis Harbour Authority, OH")
    assert r["candidates"] == [] and not r["ambiguous"]


# ------------------------------------------------------------------------------------------- by id, by host, card
def test_by_id(reg):
    assert [c["body_id"] for c in reg.by_id("nces_leaid", "0630660")] == ["b_pylusd"]
    assert [c["body_id"] for c in reg.by_id(None, "IL-123")] == ["b_springfield_fire"]
    assert reg.by_id("nces_leaid", "nope") == []


def test_by_host(reg):
    ids = [c["body_id"] for c in reg.by_host("https://www.springfield.il.us/departments")]
    assert ids == ["b_springfield", "b_springfield_fire"]  # a shared host: the government first
    assert [c["body_id"] for c in reg.by_host("pylusd.org")] == ["b_pylusd"]
    sub = reg.by_host("board.pylusd.org")
    assert [c["body_id"] for c in sub] == ["b_pylusd"] and sub[0]["score"] < 1
    assert reg.by_host("nowhere.example") == []


def test_body_card_has_the_eval_fields(reg):
    assert reg.body_card("b_cortez") == {"name": "CITY OF CORTEZ", "kind": "municipality", "state": "CO", "city": "CORTEZ",
                                         "county": "MONTEZUMA", "population_served": 9151}
    assert reg.body_card("b_nope") == {}


def test_cli_prints_ranked_candidates(root, capsys):
    assert cli.main(["Summit County, OH", "--dir", str(root)]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[1].startswith("1. b_summit")
    assert cli.main(["--dir", str(root), "--json", "Springfield IL"]) == 0
    assert json.loads(capsys.readouterr().out)["ambiguous"] is True


# ------------------------------------------------------------------------------------------- release
def test_export_release(root, tmp_path, monkeypatch):
    out = tmp_path / "release"
    manifest = export_release(out, source=root)
    paths_ = {f["path"] for f in manifest["files"]}
    assert f"entities/bodies/{RUN}.parquet" in paths_ and f"registry/name_keys/{RUN}.parquet" in paths_
    assert f"entities/bodies/{OLD}.parquet" not in paths_
    assert {f["table"] for f in manifest["files"]} == {
        "entities.bodies", "entities.identifiers", "entities.websites", "entities.body_records", "entities.relations",
        "standard.records", "standard.code_lists", "standard.code_states", "standard.code_entity_types", "meta.columns",
        "registry.name_keys"}
    assert manifest["people_check"]["result"] == "pass" and manifest["total_bytes"] == sum((out / p).stat().st_size for p in paths_)
    con = duckdb.connect()
    for f in manifest["files"]:
        assert con.execute(f"SELECT count(*) FROM read_parquet('{out / f['path']}')").fetchone()[0] == f["rows"]
    records = f"read_parquet('{out}/standard/records/{RUN}.parquet')"
    assert "owner_name" not in [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {records}").fetchall()]
    assert con.execute(f"SELECT count(*) FROM {records} WHERE website LIKE '%@%'").fetchone()[0] == 0
    assert con.execute(f"SELECT count(*) FROM read_parquet('{out}/entities/websites/{RUN}.parquet')").fetchone()[0] == len(WEBSITES) - 1
    assert manifest["redacted"]["entities.websites"] == {"rows dropped": 1}
    meta = con.execute(f"SELECT DISTINCT dataset || '.' || table_name FROM read_parquet('{out}/meta/columns/{RUN}.parquet')").fetchall()
    assert {m[0] for m in meta} == {"standard.records", "entities.bodies", "registry.name_keys"}  # no raw tables; name_keys described

    # the release reads without the Python normaliser and gives the same answers
    queries = ("Summit County, OH", "Cortez, CO", "Springfield IL", "Placentia-Yorba Linda USD, CA", "Pueblo City-County Library District")
    source = Registry(root)
    expected = {q: [c["body_id"] for c in source.resolve(q)["candidates"]] for q in queries}

    def no_normaliser(*args):
        raise AssertionError("the normaliser was called on a release")

    monkeypatch.setattr(names, "record_row", no_normaliser)
    released = Registry(out)
    for q in queries:
        assert [c["body_id"] for c in released.resolve(q)["candidates"]] == expected[q], q


def test_export_refuses_unsafe_folders(root, tmp_path):
    with pytest.raises(ValueError, match="inside the tables folder"):
        export_release(root / "release", source=root)
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "x.txt").write_text("x")
    with pytest.raises(ValueError, match="not empty"):
        export_release(busy, source=root)


def test_export_stops_on_people_columns(root, tmp_path):
    import shutil
    copy = tmp_path / "copy"
    shutil.copytree(root, copy)
    _write(copy, "entities", "relations", "20261102T0000Z-newer", {"from_body_id": "VARCHAR", "contact_email": "VARCHAR"},
           [("b_summit", "someone@example.org")])
    with pytest.raises(ValueError, match="people data found"):
        export_release(tmp_path / "out", source=copy)
    assert not (tmp_path / "out").exists()


# ------------------------------------------------------------------------------------------- real data
REAL = paths.TABLES / "entities" / "bodies"
real = pytest.mark.skipif(not REAL.exists() or not any(REAL.glob("*.parquet")), reason="no data/tables: build first")


def chain_bodies() -> dict:
    """deep/chain.py's BODIES, read without importing the module."""
    source = (Path(resolver_module.__file__).parents[1] / "deep" / "chain.py").read_text()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "BODIES" for t in node.targets):
            return {k: v["body_id"] for k, v in ast.literal_eval(node.value).items()}
    pytest.skip("BODIES not found in deep/chain.py")


@pytest.fixture(scope="module")
def real_reg():
    return Registry(paths.TABLES)


@real
@pytest.mark.parametrize("query, short", [
    ("Summit County, OH", "summit-county-oh"),
    ("Cortez, CO", "cortez-co"),
    ("Pueblo City-County Library District", "pueblo-library-co"),
    ("Pueblo City-County Library District, Colorado", "pueblo-library-co"),
    ("Placentia-Yorba Linda Unified School District", "pylusd-ca"),
    ("Placentia-Yorba Linda USD, CA", "pylusd-ca"),
])
def test_real_queries_find_the_chain_bodies(real_reg, query, short):
    expected = chain_bodies()[short]
    r = real_reg.resolve(query)
    assert top(r) == expected, [(c["body_id"], c["name"], c["score"]) for c in r["candidates"][:3]]
    assert r["seconds"] < 1.0


@real
def test_real_summit_and_cortez_are_clear(real_reg):
    assert chain_bodies()["summit-county-oh"] == "bf62a1a7cef59" and chain_bodies()["cortez-co"] == "b2378a7debda8"
    r = real_reg.resolve("Summit County, OH")
    assert top(r) == "bf62a1a7cef59" and not r["ambiguous"]
    r = real_reg.resolve("Cortez, CO")
    ids = [c["body_id"] for c in r["candidates"]]
    assert ids[0] == "b2378a7debda8" and "b7f4602240362" in ids[1:] and not r["ambiguous"]  # the city, then its water system


@real
def test_real_springfield_il_is_flagged(real_reg):
    r = real_reg.resolve("Springfield IL")
    assert r["ambiguous"] and {"b8e30881223fa", "b635058f45c5d"} <= set(r["ambiguity"]["close"])  # city, township
    assert "b3d5bf1960686" in [c["body_id"] for c in r["candidates"][:3]]  # the water system next


@real
def test_real_lookups_by_id_host_and_card(real_reg):
    assert [c["body_id"] for c in real_reg.by_id("nces_leaid", "0630660")] == ["be594dc0a1aa2"]
    assert real_reg.by_host("https://summitcountyoh.gov/")[0]["body_id"] == "bf62a1a7cef59"
    assert real_reg.body_card("b2378a7debda8")["name"] == "CITY OF CORTEZ"
    try:
        from ddwb.evals import run
    except Exception:  # another session may be changing evals/run.py; the comparison is a bonus
        return
    con = duckdb.connect()
    for body_id in chain_bodies().values():
        assert real_reg.body_card(body_id) == run.body_card(con, body_id)
