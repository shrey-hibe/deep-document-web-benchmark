"""Tables are folders of Parquet files: data/tables/<dataset>/<table>/<run_id>.parquet, one file per run.

A file is written under a temporary name and renamed when complete, so a reader never sees half a file. Each write
also records the run in meta.runs: its row count and the runs of the tables it was built from.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from . import catalogue, paths


def new_run_id() -> str:
    """When, and from which commit: 20261031T0700Z-fed52fa ('+dirty' when there are uncommitted changes)."""
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=paths.REPO, capture_output=True, text=True).stdout.strip()
    commit = git("rev-parse", "--short", "HEAD") or "nogit"
    return f"{datetime.now(UTC):%Y%m%dT%H%MZ}-{commit}{'+dirty' if git('status', '--porcelain', '--', 'src', 'tests') else ''}"


def glob(dataset: str, table: str) -> str:
    return str(paths.TABLES / dataset / table / "*.parquet")


def latest_run(con: duckdb.DuckDBPyConnection, dataset: str, table: str) -> str | None:
    folder = paths.TABLES / dataset / table
    if not any(folder.glob("*.parquet")):
        return None
    return con.execute(f"SELECT max(run_id) FROM read_parquet('{glob(dataset, table)}', union_by_name = true)").fetchone()[0]


def latest(dataset: str, table: str) -> str:
    """SQL for the latest run of a table, for use as an input."""
    files = glob(dataset, table)
    return (f"(SELECT * EXCLUDE (run_id, written_at) FROM read_parquet('{files}', union_by_name = true) "
            f"WHERE run_id = (SELECT max(run_id) FROM read_parquet('{files}', union_by_name = true)))")


def write(con: duckdb.DuckDBPyConnection, table: catalogue.Table, select_sql: str, run_id: str, inputs: dict[str, str | None]) -> int:
    """Write the rows of `select_sql` as this run's file of `table`, check them against the catalogue, record the run."""
    folder = paths.TABLES / table.dataset / table.name
    folder.mkdir(parents=True, exist_ok=True)
    final, temporary = folder / f"{run_id}.parquet", folder / f".{run_id}.parquet.tmp"
    started = datetime.now(UTC)
    con.execute(f"COPY (SELECT *, '{run_id}' AS run_id, now() AS written_at FROM ({select_sql})) TO '{temporary}' (FORMAT parquet)")
    built = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{temporary}')").fetchall()]
    try:
        catalogue.check(table, built)
    except ValueError:
        temporary.unlink()
        raise
    temporary.rename(final)
    rows = con.execute(f"SELECT count(*) FROM read_parquet('{final}')").fetchone()[0]
    _record_run(con, run_id, table.key, rows, inputs, started)
    return rows


def write_part(con: duckdb.DuckDBPyConnection, table: catalogue.Table, select_sql: str, run_id: str, part: int) -> int:
    """Write one part of a run that arrives in batches (data/tables/<dataset>/<table>/<run_id>__part-NNNNN.parquet).

    Views read every part of the latest run, so rows show in the warehouse as they arrive. Call record_parts when
    the run ends (or after each part) to record it in meta.runs.
    """
    folder = paths.TABLES / table.dataset / table.name
    folder.mkdir(parents=True, exist_ok=True)
    final, temporary = folder / f"{run_id}__part-{part:05d}.parquet", folder / f".{run_id}__part-{part:05d}.parquet.tmp"
    con.execute(f"COPY (SELECT *, '{run_id}' AS run_id, now() AS written_at FROM ({select_sql})) TO '{temporary}' (FORMAT parquet)")
    built = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{temporary}')").fetchall()]
    try:
        catalogue.check(table, built)
    except ValueError:
        temporary.unlink()
        raise
    temporary.rename(final)
    return con.execute(f"SELECT count(*) FROM read_parquet('{final}')").fetchone()[0]


def parts(table: catalogue.Table, run_id: str) -> list[Path]:
    return sorted((paths.TABLES / table.dataset / table.name).glob(f"{run_id}__part-*.parquet"))


def record_parts(con: duckdb.DuckDBPyConnection, table: catalogue.Table, run_id: str, inputs: dict[str, str | None],
                 started: datetime) -> int:
    files = parts(table, run_id)
    rows = con.execute(f"SELECT count(*) FROM read_parquet({[str(f) for f in files]!r})").fetchone()[0] if files else 0
    _record_run(con, run_id, table.key, rows, inputs, started)
    return rows


def _record_run(con, run_id: str, key: str, rows: int, inputs: dict[str, str | None], started: datetime) -> None:
    folder = paths.TABLES / "meta" / "runs"
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{run_id}__{key}"
    temporary, final = folder / f".{name}.parquet.tmp", folder / f"{name}.parquet"
    con.execute("""COPY (SELECT ? AS run_id, ? AS table_key, ? AS rows, ? AS inputs, ?::TIMESTAMPTZ AS started_at, now() AS finished_at)
                   TO '""" + str(temporary) + "' (FORMAT parquet)",
                [run_id, key, rows, json.dumps(inputs, sort_keys=True), started.isoformat()])
    temporary.rename(final)
