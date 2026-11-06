"""The raw Census tables hold every row of the file, in order, with their provenance."""

import duckdb
import pytest

from ddwb import catalogue, paths
from ddwb.raw import census


@pytest.fixture(scope="module")
def con():
    if not paths.WAREHOUSE.exists():
        pytest.skip("build first: uv run python -m ddwb.build")
    return duckdb.connect(str(paths.WAREHOUSE), read_only=True)


@pytest.mark.parametrize("tab", list(census.TABS))
def test_every_row_kept_in_order_once(con, tab):
    name = census.SPECS[tab].name
    n, first, last, distinct_rows, distinct_ids = con.execute(
        f"select count(*), min(_row), max(_row), count(distinct _row), count(distinct CENSUS_ID_PID6) from raw.{name}").fetchone()
    assert (first, last, distinct_rows) == (1, n, n)
    assert distinct_ids == n  # one row per government


def test_every_catalogued_column_says_what_and_how():
    for table in catalogue.TABLES.values():
        for column in table.columns:
            assert column.meaning and column.how in catalogue.HOW, (table.key, column.name)
