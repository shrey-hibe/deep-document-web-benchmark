"""The column catalogue: every column of every table, what it means, where it comes from and how.

A build fails if a table has a column the catalogue doesn't describe, or the catalogue names a column the table
doesn't have. `how` is one of:
- copy:        the source value, unchanged
- cast:        the source value, converted to a type (text to date, say)
- split:       part of a source value: the code or the wording in '1 - COUNTY', positions of an ID
- join_codes:  source codes put together (state FIPS + county FIPS)
- code_table:  looked up in a code table stored as a table: an agency's own, copied from its documentation, or this
               repo's one name per agency code (standard.code_entity_types)
- id_lookup:   the record holding exactly a published ID (exact equality, never a name match)
- label:       a judgement, read from a named label set (see the labels dataset)
- bookkeeping: ids, provenance and run columns this repo adds
"""

from __future__ import annotations

from dataclasses import dataclass, field

HOW = {"copy", "cast", "split", "join_codes", "code_table", "id_lookup", "label", "bookkeeping"}


@dataclass(frozen=True)
class Column:
    name: str
    meaning: str
    how: str
    source: str = ""  # upstream table.column(s), or the source file's column

    def __post_init__(self) -> None:
        if self.how not in HOW:
            raise ValueError(f"{self.name}: how must be one of {sorted(HOW)}, not {self.how!r}")


@dataclass(frozen=True)
class Table:
    dataset: str
    name: str
    grain: str  # one row per what
    description: str
    columns: tuple[Column, ...] = field(default_factory=tuple)
    inputs: tuple[str, ...] = ()  # upstream dataset.table names, or source files for raw tables
    log: bool = False  # a log table (ddwb.runlog): every run adds rows, and its view shows every run, not just the latest

    @property
    def key(self) -> str:
        return f"{self.dataset}.{self.name}"


# Columns every table gets when it is written (tables.write).
RUN_COLUMNS = (
    Column("run_id", "the build that wrote this row: time, then the commit of this repo", "bookkeeping"),
    Column("written_at", "when the row was written (UTC)", "bookkeeping"),
)

TABLES: dict[str, Table] = {}


def register(table: Table) -> Table:
    TABLES[table.key] = table
    return table


def check(table: Table, built_columns: list[str]) -> None:
    """Fail when the built table and its catalogue entry disagree."""
    described = [c.name for c in table.columns] + [c.name for c in RUN_COLUMNS]
    missing = [c for c in built_columns if c not in described]
    extra = [c for c in described if c not in built_columns]
    if missing or extra:
        raise ValueError(f"{table.key}: columns not in the catalogue {missing}; catalogue columns not built {extra}")


def rows() -> list[dict]:
    """The catalogue as rows, for the meta.columns table."""
    out = []
    for table in TABLES.values():
        for position, column in enumerate(table.columns + RUN_COLUMNS, start=1):
            out.append({"dataset": table.dataset, "table_name": table.name, "position": position, "column_name": column.name,
                        "meaning": column.meaning, "how": column.how, "source": column.source,
                        "table_grain": table.grain, "table_inputs": ", ".join(table.inputs)})
    return out
