"""The run log: one way for any pipeline to record what it did, as it does it.

A pipeline declares its log tables with `spec` (each column with its type, meaning and how it was made, checked by the
catalogue) and writes rows with `Log.add`. Rows are buffered and written as part files of the run
(data/tables/<dataset>/<table>/<run_id>__part-NNNNN.parquet, via tables.write_part), so they show in the warehouse while
the run is going, and every write is recorded in meta.runs. Log tables are append-only: each run adds its own rows, and
their warehouse views show every run (catalogue.Table.log), unlike built tables, whose views show the latest run.

Inputs are kept exactly (what a model saw, what a page returned), so a result can be re-scored or re-run later.
Used by the evals (ddwb.evals.log) and the deep chain (ddwb.deep.store).
"""

from __future__ import annotations

import json
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from . import catalogue, tables
from .catalogue import Column

B = "bookkeeping"


def spec(dataset: str, name: str, grain: str, description: str, columns: list[tuple[str, str, str, str]]):
    """Declare a log table. columns: (name, DuckDB type, meaning, how). Returns (catalogue table, {column: type})."""
    table = catalogue.register(catalogue.Table(dataset=dataset, name=name, grain=grain, description=description,
                                               columns=tuple(Column(c, m, h) for c, _, m, h in columns), log=True))
    return table, {c: t for c, t, _, _ in columns}


class Log:
    """Buffers rows per table and writes them as part files of the run. Thread-safe."""

    def __init__(self, run_id: str, specs: tuple, flush_every: int = 200) -> None:
        self.run_id, self.flush_every, self.con, self.specs = run_id, flush_every, duckdb.connect(), specs
        self.buffers: dict[str, list[dict]] = {t.key: [] for t, _ in specs}
        self.part = {t.key: len(tables.parts(t, run_id)) for t, _ in specs}
        self.lock = threading.RLock()

    def add(self, spec, row: dict) -> None:
        table, _ = spec
        with self.lock:
            self.buffers[table.key].append(row)
            if len(self.buffers[table.key]) >= self.flush_every:
                self._flush(spec)

    def flush(self, spec=None) -> None:
        with self.lock:
            self._flush(spec)

    def _flush(self, spec=None) -> None:
        for table, types in ([spec] if spec else self.specs):
            rows = self.buffers[table.key]
            if not rows:
                continue
            with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps({c: (json.dumps(row.get(c), default=str) if t == "JSON" and row.get(c) is not None
                                                 else row.get(c)) for c, t in types.items()}, default=str) + "\n")
            # names quoted, so a column may be called anything (at, check, rows, ...)
            columns = ", ".join(f"\"{c}\": '{'VARCHAR' if t == 'JSON' else t}'" for c, t in types.items())
            select = ", ".join(f'CAST("{c}" AS JSON) AS "{c}"' if t == "JSON" else f'"{c}"' for c, t in types.items())
            self.part[table.key] += 1
            tables.write_part(self.con, table, f"""SELECT {select} FROM read_json('{handle.name}', format = 'newline_delimited',
                                                    columns = {{{columns}}})""", self.run_id, self.part[table.key])
            tables.record_parts(self.con, table, self.run_id, {}, datetime.now(UTC))
            Path(handle.name).unlink()
            self.buffers[table.key] = []

    def close(self) -> None:
        self.flush()


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")
