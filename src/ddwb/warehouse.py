"""data/warehouse.duckdb: schemas and views only, no data, like a BigQuery project.

- One view per catalogued table, showing its latest run (files read by column name, so older runs still read);
  a log table's view (ddwb.runlog) shows every run.
- meta.runs: every table run, with the runs of the tables it was built from.
- meta.stale: tables whose latest run was built from an input that has a newer run since.

Built by ddwb.build. Close the DuckDB UI first: it holds a lock on the file.
"""

from __future__ import annotations

import duckdb

from . import catalogue, paths, tables

STALE = """
CREATE OR REPLACE VIEW meta.stale AS
WITH latest AS (SELECT table_key, max(run_id) AS run_id FROM meta.runs GROUP BY 1),
built_from AS (
  SELECT r.table_key, r.run_id, k AS input_key, json_extract_string(r.inputs, '$."' || k || '"') AS input_run_id
  FROM meta.runs r JOIN latest l USING (table_key, run_id), unnest(json_keys(r.inputs)) AS t(k)
)
SELECT b.table_key, b.run_id, b.input_key, b.input_run_id AS built_from_run, l.run_id AS input_latest_run
FROM built_from b JOIN latest l ON l.table_key = b.input_key
WHERE b.input_run_id IS DISTINCT FROM l.run_id
"""


def build() -> None:
    paths.DATA.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(paths.WAREHOUSE))
    for table in catalogue.TABLES.values():
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {table.dataset}")
        files = tables.glob(table.dataset, table.name)
        if not any((paths.TABLES / table.dataset / table.name).glob("*.parquet")):
            continue  # nothing written yet; the view comes with the first run
        if table.log:  # a log table: every run's rows count (ddwb.runlog)
            con.execute(f"CREATE OR REPLACE VIEW {table.dataset}.{table.name} AS "
                        f"SELECT * FROM read_parquet('{files}', union_by_name = true)")
            continue
        con.execute(f"""CREATE OR REPLACE VIEW {table.dataset}.{table.name} AS
                        SELECT * FROM read_parquet('{files}', union_by_name = true)
                        WHERE run_id = (SELECT max(run_id) FROM read_parquet('{files}', union_by_name = true))""")
    con.execute("CREATE SCHEMA IF NOT EXISTS meta")
    con.execute(f"CREATE OR REPLACE VIEW meta.runs AS SELECT * FROM read_parquet('{tables.glob('meta', 'runs')}')")
    con.execute(STALE)
    con.close()
