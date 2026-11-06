"""standard.records holds every record in scope once, and every value is the agency's own or looked up in a code table."""

import duckdb
import pytest

from ddwb import paths
from ddwb.standard import records


@pytest.fixture(scope="module")
def con():
    if not paths.WAREHOUSE.exists():
        pytest.skip("build first: uv run python -m ddwb.build")
    return duckdb.connect(str(paths.WAREHOUSE), read_only=True)


def test_record_id_is_unique_and_present(con):
    n, distinct, present = con.execute("select count(*), count(distinct record_id), count(record_id) from standard.records").fetchone()
    assert n == distinct == present


def test_each_raw_row_appears_once(con):
    assert con.execute("select count(*) - count(distinct (list, source_row)) from standard.records").fetchone()[0] == 0


@pytest.mark.parametrize("table", [t for t in records.SPEC.inputs if t.startswith("raw.")
                                   and t not in (f"raw.{records.FIN}", f"raw.{records.SCHOOL}")])
def test_whole_lists_are_kept_whole(con, table):
    raw = con.execute(f"select count(*) from {table}").fetchone()[0]
    assert con.execute("select count(*) from standard.records where list = ?", [table.removeprefix("raw.")]).fetchone()[0] == raw


def test_finance_units_are_those_missing_from_the_2026_list(con):
    expected = con.execute(f"""select count(*) from raw.{records.FIN} where substr(ID, 7, 6) not in (
        {' union all '.join(f'select CENSUS_ID_PID6 from raw.{t}' for t in records.GUS.values())})""").fetchone()[0]
    assert con.execute("select count(*) from standard.records where list = ?", [records.FIN]).fetchone()[0] == expected


def test_charter_schools_are_those_outside_charter_leas(con):
    expected = con.execute(f"""select count(*) from raw.{records.SCHOOL} s join raw.{records.LEA} l using (LEAID)
                               where s.CHARTER_TEXT = 'Yes' and l.LEA_TYPE <> '7'""").fetchone()[0]
    assert con.execute("select count(*) from standard.records where list = ?", [records.SCHOOL]).fetchone()[0] == expected


def test_every_record_has_an_entity_type_and_edition(con):
    assert con.execute("select count(*) from standard.records where entity_type is null or edition is null").fetchone()[0] == 0


@pytest.mark.parametrize("code, wording", [("unit_type_code", "unit_type"), ("status_code", "status"),
                                           ("owner_type_code", "owner_type"), ("legal_basis_code", "legal_basis"),
                                           ("function_code", "function")])
def test_every_code_has_the_agencys_wording(con, code, wording):
    # DEP and pension tabs publish no unit type of their own: their UNIT_TYPE is the parent's kind (parent_unit_type).
    assert con.execute(f"select count(*) from standard.records where {code} is not null and {wording} is null").fetchone()[0] == 0


def test_status_is_never_made_up(con):
    # Only lists that publish a status have one, and it is the agency's wording for the code, untranslated.
    assert con.execute("""select count(*) from standard.records
                          where status is not null and agency in ('USFA', 'FTA')""").fetchone()[0] == 0
    assert con.execute(f"""select count(*) from standard.records r join raw.{records.LEA} l on r.list = '{records.LEA}'
                           and r.agency_id = l.LEAID where r.status is distinct from l.UPDATED_STATUS_TEXT""").fetchone()[0] == 0
    assert con.execute("""select count(*) from standard.records r join standard.code_lists c on c.code = r.status_code
                          and c.code_list = case r.agency when 'Census' then 'census_active' when 'EPA' then 'epa_activity'
                                                         when 'IMLS' then 'imls_structure_change' end
                          where r.status is distinct from c.meaning""").fetchone()[0] == 0


def test_state_fips_is_missing_only_where_the_agency_gave_no_us_state(con):
    # EPA lists a few systems with no state or a Canadian province.
    rows = con.execute("""select list, coalesce(state, '') from standard.records where state_fips is null
                          group by all""").fetchall()
    assert {list_name for list_name, _ in rows} <= {records.EPA}
    assert {state for _, state in rows} <= {"", "BC", "ON", "PQ", "QC"}
