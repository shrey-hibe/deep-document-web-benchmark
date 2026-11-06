"""The other raw tables hold every data row of their file, in order, once."""

import duckdb
import pytest

from ddwb import paths
from ddwb.raw import files

TABLES = [s.table for s in files.SOURCES] + [files.FIN_SPEC.name]


@pytest.fixture(scope="module")
def con():
    if not paths.WAREHOUSE.exists():
        pytest.skip("build first: uv run python -m ddwb.build")
    return duckdb.connect(str(paths.WAREHOUSE), read_only=True)


@pytest.mark.parametrize("table", TABLES)
def test_rows_in_file_order_once(con, table):
    n, first, last, distinct = con.execute(f"select count(*), min(_row), max(_row), count(distinct _row) from raw.{table}").fetchone()
    assert n > 0 and (first, last, distinct) == (1, n, n)


@pytest.mark.parametrize("table, key", [("nces_lea_directory_2025_26", "LEAID"), ("nces_school_directory_2025_26", "NCESSCH"),
                                        ("epa_sdwis_water_system", "pwsid"), ("imls_pls_fy2024_administrative_entity", "FSCSKEY"),
                                        ("census_finance_2022_units", "ID"), ("census_f33_2024", "PID6"), ("census_f33_2024", "NCESID")])
def test_the_agency_id_is_unique(con, table, key):
    assert con.execute(f'select count(*) - count(distinct "{key}") from raw.{table}').fetchone()[0] == 0


def test_ntd_rows_are_reporter_per_program(con):
    # NTD lists a reporter once per program it reports through (Greyhound: 21 state programs), so ntd_id alone repeats.
    assert con.execute("""select count(*) - count(distinct (ntd_id, coalesce(reported_by_ntd_id, ''), reporter_type))
                          from raw.ntd_reporters_2025""").fetchone()[0] == 0
