"""standard.links holds every link the agencies publish, once, matched to records by exact ID only."""

import duckdb
import pytest

from ddwb import paths
from ddwb.standard import links


@pytest.fixture(scope="module")
def con():
    if not paths.WAREHOUSE.exists():
        pytest.skip("build first: uv run python -m ddwb.build")
    return duckdb.connect(str(paths.WAREHOUSE), read_only=True)


def test_each_published_column_of_each_row_is_one_link(con):
    assert con.execute("select count(*) - count(distinct (list, source_row, source_columns)) from standard.links").fetchone()[0] == 0


@pytest.mark.parametrize("table, column", [(links.DEP, "PARENT_CENSUS_ID_PID6"), (links.PEN, "PARENT_CENSUS_ID_PID6"),
                                           (links.F33, "NCESID"), (links.NTD, "reported_by_ntd_id")])
def test_every_published_link_is_kept(con, table, column):
    published = con.execute(f'select count("{column}") from raw.{table}').fetchone()[0]
    assert con.execute("select count(*) from standard.links where list = ?", [table]).fetchone()[0] == published


def test_a_link_is_matched_only_when_exactly_one_record_holds_the_id(con):
    assert con.execute("""select count(*) from standard.links
                          where (to_record_id is not null) <> (to_records = 1)""").fetchone()[0] == 0


def test_matched_records_hold_the_published_id(con):
    # Exact ID only: the matched record's own ID, or (Census finance units) the PID6 in positions 7-12 of its ID.
    assert con.execute("""select count(*) from standard.links l join standard.records r on r.record_id = l.to_record_id
                          where l.to_agency_id <> case when r.id_scheme = 'census_government_id' and l.to_scheme = 'census_pid6'
                                                       then substr(r.agency_id, 7, 6) else r.agency_id end""").fetchone()[0] == 0


def test_links_read_from_a_record_start_at_that_record(con):
    assert con.execute(f"""select count(*) from standard.links l left join standard.records r
                           on r.list = l.list and r.source_row = l.source_row
                           where l.list <> '{links.F33}' and r.record_id is distinct from l.from_record_id""").fetchone()[0] == 0


def test_state_charter_codes_are_never_matched(con):
    assert con.execute("""select count(*) from standard.links
                          where to_scheme = 'state_charter_authorizer_code' and to_record_id is not null""").fetchone()[0] == 0
