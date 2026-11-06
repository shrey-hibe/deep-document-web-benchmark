"""The navigation state: what an agent has found, opened, read, skipped and recorded for one body, kept as an
append-only event log. The agent's context is working memory; this store is the memory. An agent asks the `next` tool
what is pending, so a long run survives compaction and can resume.

Every tool call appends events. Each event carries its node's whole state (facts merged with what came before), so a
node's current state is simply its latest event. Nodes:

    B  the body              G  a page (homepage, search result, candidate list, accepted list)
    K  a link on a list      M  a meeting (date and board)
    P  a passage read        E  a buying event recorded by the agent           A  the answer

One SQLite file per run (data/nav/<run>.sqlite): the arms of a run write to it side by side, one session each
(run:arm:body), and the run's settings (its caps, who approved it) are fixed there by its first `start`. A side table
notes which passages the agent was shown, so a skip of many at once can tell reviewed from unreviewed; it is not an event,
so it changes neither the nodes nor the tool calls counted. Paid costs are
tagged with the cap that paid them and also go to data/nav/shared.sqlite with the run's ID: every arm of a run counts
against that run's caps, and a new run starts from zero. Host clocks (polite pacing, bot walls) live in shared.sqlite
too, across runs.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from .. import paths

NAV = paths.DATA / "nav"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (session TEXT PRIMARY KEY, run_id TEXT, arm TEXT, body TEXT, body_id TEXT, query TEXT,
    since TEXT, started_at TEXT, card TEXT, homepage TEXT, status TEXT, stopped TEXT);
CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, at TEXT, node TEXT, kind TEXT,
    parent TEXT, url TEXT, status TEXT, data TEXT, jev TEXT, reason TEXT, tool TEXT, cost_usd REAL DEFAULT 0,
    budget TEXT, ms INTEGER);
CREATE INDEX IF NOT EXISTS events_node ON events (session, node, seq);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS shown (session TEXT, node TEXT, at TEXT, PRIMARY KEY (session, node));
CREATE VIEW IF NOT EXISTS nodes AS SELECT e.* FROM events e
    JOIN (SELECT session, node, max(seq) AS last FROM events GROUP BY 1, 2) l ON e.session = l.session AND e.node = l.node AND e.seq = l.last;
"""


BLOCK_DAYS = 7  # a host that showed a bot wall is left alone this long, then tried again


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def nid(prefix: str, key: str) -> str:
    """A short, stable node ID: the same URL gets the same ID in every arm, so their maps line up."""
    return prefix + hashlib.sha1(key.encode()).hexdigest()[:7]


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in ("data", "jev", "card"):
        if d.get(k):
            d[k] = json.loads(d[k])
    return d


def opened(path, script: str, timeout: float) -> sqlite3.Connection:
    """A connection with the schema in place. Several tool processes start at once: switching to WAL and creating
    tables can meet a lock that the busy timeout does not cover, so the start is retried."""
    for attempt in range(8):
        try:
            db = sqlite3.connect(path, timeout=timeout, isolation_level=None)
            db.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
            if db.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal":
                db.execute("PRAGMA journal_mode=WAL")
            db.executescript(script)
            return db
        except sqlite3.OperationalError as error:
            if "locked" not in str(error) or attempt == 7:
                raise
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError("unreachable")


class State:
    def __init__(self, run_id: str) -> None:
        NAV.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.db = opened(NAV / f"{run_id}.sqlite", SCHEMA, 60)
        self.db.row_factory = sqlite3.Row
        self.shared = shared_db()

    # ------------------------------------------------------------------------------- sessions
    def session(self, sid: str) -> dict | None:
        return _row(self.db.execute("SELECT * FROM sessions WHERE session = ?", [sid]).fetchone())

    def start(self, sid: str, **fields) -> None:
        cols = ["session", *fields]
        self.db.execute(f"INSERT OR IGNORE INTO sessions ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                        [sid, *(json.dumps(v) if isinstance(v, (dict, list)) else v for v in fields.values())])

    def set_session(self, sid: str, **fields) -> None:
        self.db.execute(f"UPDATE sessions SET {', '.join(f'{k} = ?' for k in fields)} WHERE session = ?", [*fields.values(), sid])

    # ------------------------------------------------------------------------------- nodes
    def get(self, sid: str, node: str) -> dict | None:
        return _row(self.db.execute("SELECT * FROM events WHERE session = ? AND node = ? ORDER BY seq DESC LIMIT 1",
                                    [sid, node]).fetchone())

    def put(self, sid: str, node: str, kind: str, status: str, *, url=None, parent=None, data=None, jev=None, reason=None,
            tool=None, cost=0.0, budget=None, ms=None) -> dict:
        """Append the node's new state: its facts merged with the old, Jev's answers and the reason replaced if given."""
        old = self.get(sid, node) or {}
        merged = {**(old.get("data") or {}), **(data or {})}
        row = {"session": sid, "at": now(), "node": node, "kind": kind, "parent": parent or old.get("parent"),
               "url": url or old.get("url"), "status": status, "data": json.dumps(merged, default=str),
               "jev": json.dumps(jev if jev is not None else old.get("jev"), default=str),
               "reason": reason if reason is not None else old.get("reason"), "tool": tool, "cost_usd": cost or 0.0,
               "budget": budget, "ms": ms}
        self.db.execute(f"INSERT INTO events ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
        if cost and budget:
            self.shared.execute("INSERT INTO spend (run_id, budget, cost_usd, at) VALUES (?, ?, ?, ?)", [self.run_id, budget, cost, row["at"]])
        return row | {"data": merged, "jev": jev if jev is not None else old.get("jev")}

    def nodes(self, sid: str, kind: str | None = None, status: str | tuple | None = None) -> list[dict]:
        sql, args = "SELECT * FROM nodes WHERE session = ?", [sid]
        if kind:
            sql, args = sql + " AND kind = ?", args + [kind]
        if status:
            statuses = (status,) if isinstance(status, str) else tuple(status)
            sql, args = sql + f" AND status IN ({', '.join('?' * len(statuses))})", args + list(statuses)
        return [_row(r) for r in self.db.execute(sql + " ORDER BY seq", args).fetchall()]

    def mark_shown(self, sid: str, nodes: list[str]) -> None:
        """Nodes whose text a tool printed for the agent (the first time is kept)."""
        if nodes:
            at = now()
            with self.batch():
                self.db.executemany("INSERT OR IGNORE INTO shown (session, node, at) VALUES (?, ?, ?)", [(sid, n, at) for n in nodes])

    def shown(self, sid: str) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT node FROM shown WHERE session = ?", [sid])}

    def events(self, sid: str) -> list[dict]:
        return [_row(r) for r in self.db.execute("SELECT * FROM events WHERE session = ? ORDER BY seq", [sid]).fetchall()]

    @contextmanager
    def batch(self):
        """Many puts in one transaction (a map of 2,000 links)."""
        self.db.execute("BEGIN")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------------------- the run's settings and money
    def settings(self) -> dict:
        return {k: json.loads(v) for k, v in self.db.execute("SELECT key, value FROM settings").fetchall()}

    def fix_settings(self, **values) -> dict:
        """The run's settings, set once by its first `start` (later values for the same key are ignored). Returns the
        settings in force."""
        for k, v in values.items():
            self.db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", [k, json.dumps(v)])
        return self.settings()

    def spent(self, budget: str) -> float:
        """Paid cost against one of this run's caps, across the run's sessions (every arm of the run)."""
        return self.shared.execute("SELECT coalesce(sum(cost_usd), 0) FROM spend WHERE budget = ? AND run_id = ?",
                                   [budget, self.run_id]).fetchone()[0]


def shared_db() -> sqlite3.Connection:
    """data/nav/shared.sqlite: what every process shares: paid spend against the approved caps, and host clocks."""
    NAV.mkdir(parents=True, exist_ok=True)
    existed = (NAV / "shared.sqlite").exists()
    db = opened(NAV / "shared.sqlite", "CREATE TABLE IF NOT EXISTS hosts (host TEXT PRIMARY KEY, next_at REAL, blocked TEXT, blocked_at TEXT);", 120)
    new = not existed or not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'spend'").fetchone()
    db.execute("CREATE TABLE IF NOT EXISTS spend (run_id TEXT, budget TEXT, cost_usd REAL, at TEXT)")
    db.execute("CREATE TABLE IF NOT EXISTS browser_hosts (host TEXT PRIMARY KEY, why TEXT, at TEXT)")
    if new:  # spend recorded before this table existed (2026-11-03 smoke runs), carried over once
        for f in NAV.glob("*.sqlite"):
            if f.name in ("shared.sqlite", "hosts.sqlite"):
                continue
            run = sqlite3.connect(f)
            try:
                for budget, cost in run.execute("SELECT budget, sum(cost_usd) FROM events WHERE budget IS NOT NULL GROUP BY 1"):
                    db.execute("INSERT INTO spend VALUES (?, ?, ?, ?)", [f.stem, budget, cost, now()])
            except sqlite3.OperationalError:  # not a run store
                pass
            run.close()
    return db


class HostClock:
    """Polite pacing across processes: each tool call is its own process, so the gap between two requests to a host,
    and a host that showed a bot wall, live here rather than in memory."""

    def __init__(self) -> None:
        self._local = threading.local()

    @property
    def db(self) -> sqlite3.Connection:
        """One connection per thread: the browser paces from its own worker thread, and a SQLite connection belongs
        to the thread that made it."""
        db = getattr(self._local, "db", None)
        if db is None:
            db = self._local.db = shared_db()
        return db

    def blocked(self, host: str) -> str | None:
        """Why the host is blocked, if it showed a bot wall in the last BLOCK_DAYS; an older block has lapsed."""
        r = self.db.execute("SELECT blocked, blocked_at FROM hosts WHERE host = ?", [host]).fetchone()
        if not r or not r[0]:
            return None
        try:
            if r[1] and datetime.fromisoformat(r[1]) < datetime.now(UTC) - timedelta(days=BLOCK_DAYS):
                return None
        except ValueError:
            pass
        return r[0]

    def browser_only(self, host: str) -> str | None:
        """Why the host is read through the browser (plain HTTP walled or never answered there in the last BLOCK_DAYS)."""
        r = self.db.execute("SELECT why, at FROM browser_hosts WHERE host = ?", [host]).fetchone()
        try:
            return r[0] if r and datetime.fromisoformat(r[1]) >= datetime.now(UTC) - timedelta(days=BLOCK_DAYS) else None
        except (TypeError, ValueError):
            return r[0] if r else None

    def needs_browser(self, host: str, why: str) -> None:
        self.db.execute("INSERT INTO browser_hosts VALUES (?, ?, ?) ON CONFLICT(host) DO UPDATE SET why = excluded.why, "
                        "at = excluded.at", [host, why, now()])

    def block(self, host: str, why: str) -> None:
        self.db.execute("INSERT INTO hosts (host, next_at, blocked, blocked_at) VALUES (?, 0, ?, ?) "
                        "ON CONFLICT(host) DO UPDATE SET blocked = excluded.blocked, blocked_at = excluded.blocked_at", [host, why, now()])

    def wait(self, host: str, gap: float) -> None:
        """Reserve the host's next slot, then sleep until it (other hosts are not held up meanwhile)."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            r = self.db.execute("SELECT next_at FROM hosts WHERE host = ?", [host]).fetchone()
            slot = max(time.time(), (r[0] or 0) if r else 0)
            self.db.execute("INSERT INTO hosts (host, next_at) VALUES (?, ?) ON CONFLICT(host) DO UPDATE SET next_at = excluded.next_at",
                            [host, slot + gap])
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        time.sleep(max(0.0, slot - time.time()))
