"""Bodies: every record in exactly one body, joined only by published links; trees and counts agree."""

import hashlib

import duckdb
import pytest

from ddwb import paths


@pytest.fixture(scope="module")
def con():
    if not paths.WAREHOUSE.exists():
        pytest.skip("build first: uv run python -m ddwb.build")
    return duckdb.connect(str(paths.WAREHOUSE), read_only=True)


def test_every_record_is_in_exactly_one_body(con):
    records, members, distinct = con.execute("""select (select count(*) from standard.records), count(*), count(distinct record_id)
                                                from entities.body_records""").fetchone()
    assert records == members == distinct


def test_every_body_has_one_anchor_and_its_id_comes_from_it(con):
    assert con.execute("select count(*) from (select body_id from entities.body_records group by 1 having count(*) filter (where is_anchor) <> 1)").fetchone()[0] == 0
    for body_id, anchor in con.execute("select body_id, anchor_record_id from entities.bodies using sample 200 rows").fetchall():
        assert body_id == "b" + hashlib.sha1(anchor.encode()).hexdigest()[:12]


def test_records_join_only_through_published_links(con):
    assert {r[0] for r in con.execute("select distinct joined_by from entities.body_records").fetchall()} <= {"anchor", "same_school_system", "same_ntd_id"}


def test_no_body_holds_two_census_governments(con):
    assert con.execute("""select count(*) from (select m.body_id from entities.body_records m join standard.records r using (record_id)
                          where r.agency = 'Census' group by 1 having count(*) > 1)""").fetchone()[0] == 0


def test_parents_come_from_accepted_relations(con):
    assert con.execute("""select count(*) from entities.bodies b left join entities.relations r
                          on r.from_body_id = b.body_id and r.to_body_id = b.parent_body_id and r.relation = b.parent_relation
                          where b.parent_body_id is not null and (r.status is distinct from 'accepted')""").fetchone()[0] == 0


def test_tree_columns_agree(con):
    assert con.execute("""select count(*) from entities.bodies where is_child <> (parent_body_id is not null)
                          or (depth = 0) <> (root_body_id = body_id) or is_parent <> (children_count > 0)""").fetchone()[0] == 0
    assert con.execute("""select count(*) from entities.bodies b left join (select parent_body_id, count(*) n from entities.bodies
                          where parent_body_id is not null group by 1) c on c.parent_body_id = b.body_id
                          where b.children_count <> coalesce(c.n, 0)""").fetchone()[0] == 0


def test_identifiers_are_one_row_per_body_and_id(con):
    assert con.execute("select count(*) - count(distinct (body_id, scheme, value)) from entities.identifiers").fetchone()[0] == 0


def test_the_chosen_website_is_rank_one(con):
    assert con.execute("""select count(*) from entities.bodies b left join entities.websites w on w.body_id = b.body_id and w.rank = 1
                          where b.website is distinct from w.website""").fetchone()[0] == 0


def test_the_chosen_website_is_a_good_one_when_the_body_has_one(con):
    assert con.execute("""select count(*) from entities.websites w where w.rank = 1 and w.quality <> 'ok'
                          and exists (select 1 from entities.websites o where o.body_id = w.body_id and o.quality = 'ok')""").fetchone()[0] == 0


def test_relation_candidates_are_one_to_one_and_never_set_parents(con):
    assert con.execute("""select count(*) from (select from_record_id, rule from labels.relation_candidates
                          where rule in ('same_name_zip', 'same_name_state', 'named_after_government', 'imls_legal_basis_area')
                          group by all having count(*) > 1)""").fetchone()[0] == 0
    assert con.execute("""select count(*) from entities.bodies b join entities.relations r
                          on r.from_body_id = b.body_id and r.to_body_id = b.parent_body_id
                          where r.status = 'candidate' and not exists (select 1 from entities.relations a where a.status = 'accepted'
                          and a.from_body_id = b.body_id and a.to_body_id = b.parent_body_id)""").fetchone()[0] == 0
