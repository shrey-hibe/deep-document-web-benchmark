"""The light cost layer: a price table for estimates, a local ledger (one JSON line per call), a response cache, per-run
limits and the price guard.

The price guard compares what a provider REPORTS it charged with usage x this price table, so a price change is
caught. It never compares a charge with a pre-call estimate made from the request's size (that misfired on
2026-11-05: a table-heavy passage had more tokens per character than the estimate assumed, while the bill matched the
price table exactly). Pre-call estimates are only for the per-run dollar limit.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

# USD. Searches and scrapes per call; models per million tokens (input, output). Where each was read is noted.
PRICES: dict[str, Any] = {
    "parallel": {"search": 0.001, "included_results": 10, "extra_result": 0.001},  # Search, fast: docs.parallel.ai pricing (2026-10-28)
    "exa": {"search": 0.007, "included_results": 10, "extra_result": 0.001},  # /search, auto: exa.ai/pricing (2026-10-28)
    "firecrawl": {"scrape": 0.005},  # one page = one credit (2026-10-29)
    "models": {  # Vercel AI Gateway model pages (2026-10-29); Jev measured against the gateway's bill (2026-11-05)
        "typesafe-ai/jev-1.14.0": (0.042, 0.0),
        "anthropic/claude-haiku-5.5": (0.10, 0.50),
        "anthropic/claude-sonnet-5.5": (2.00, 10.00),  # Claude API list price (2026-10-06); the price guard checks the bill
        "openai/gpt-5-nano": (0.05, 0.40),
        "deepseek/deepseek-v4.1-flash": (0.044, 0.30),
        "zai/glm-5.3-flash": (0.0375, 0.0742),
        "alibaba/qwen3.8-flash": (0.15, 0.47),
        "google/gemini-3.1-flash-lite": (0.25, 1.50),
        "google/gemini-3-flash": (0.50, 3.00),
        "zai/glm-4.7-flash": (0.07, 0.40),
    },
}
JEV_OVERHEAD_TOKENS = 300  # fixed tokens per Jev call (measured)
JEV_CHARS_PER_TOKEN = 1.5  # estimates only (the dollar limit): conservative on table-heavy states
DEFAULT_MAX_TOKENS = 8000  # reason calls without max_tokens
DRIFT_FACTOR = 1.5  # a reported charge above usage x price x this is a price change
DRIFT_SLACK_USD = 1e-7  # rounding in a provider's report is not a drift


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def model_price(model: str, overrides: Mapping[str, Any] | None = None) -> tuple[float, float] | None:
    """(USD per input token, USD per output token), or None for a model with no price."""
    row = (overrides or {}).get(model)
    if isinstance(row, Mapping):
        return float(row.get("input_per_mtok", 0)) / 1e6, float(row.get("output_per_mtok", 0)) / 1e6
    if row is not None:
        return float(row[0]) / 1e6, float(row[1]) / 1e6
    row = PRICES["models"].get(model)
    return None if row is None else (row[0] / 1e6, row[1] / 1e6)


def search_price(vendor: str, n: int) -> float:
    p = PRICES[vendor]
    return p["search"] + max(0, n - p["included_results"]) * p["extra_result"]


# ------------------------------------------------------------------------------------------- what a call cost
@dataclass
class Charge:
    """The cost of one call. `reported`: what the provider said it charged; `expected`: usage x the price table."""

    cost: float
    source: str
    reported: float | None = None
    expected: float | None = None

    @property
    def drift(self) -> str | None:
        if self.reported is None or self.expected is None:
            return None
        if self.reported > self.expected * DRIFT_FACTOR + DRIFT_SLACK_USD:
            return (f"reported ${self.reported:.6f} against usage x price table ${self.expected:.6f} (over {DRIFT_FACTOR}x): "
                    "the provider's price changed; fix the price table before going on")
        return None


def _num(x: Any) -> float | None:
    if isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def charge_parallel(raw: Any, est: float) -> Charge:
    """Parallel reports usage SKUs, not dollars: usage x price. Unknown SKUs are priced like a search."""
    usage = raw.get("usage") if isinstance(raw, Mapping) else None
    if not isinstance(usage, list) or not usage:
        return Charge(est, "price_table")
    total = sum((_num(u.get("count")) or 0.0) * PRICES["parallel"]["search"] for u in usage if isinstance(u, Mapping))
    return Charge(total, "usage_x_price")


def charge_exa(raw: Any, n: int) -> Charge:
    expected = search_price("exa", n)
    cd = raw.get("costDollars") if isinstance(raw, Mapping) else None
    reported = _num(cd.get("total")) if isinstance(cd, Mapping) else None
    if reported is None:
        return Charge(expected, "price_table")
    return Charge(reported, "reported", reported, expected)


def charge_firecrawl(meta: Mapping[str, Any] | None, pages: int = 1) -> Charge:
    """Firecrawl reports credits used; one page is one credit at the price table's rate."""
    per_credit = PRICES["firecrawl"]["scrape"]
    expected = pages * per_credit
    credits = _num((meta or {}).get("creditsUsed"))
    if credits is None:
        return Charge(expected, "price_table")
    return Charge(credits * per_credit, "reported_credits", credits * per_credit, expected)


def tokens(usage: Any) -> tuple[float | None, float | None]:
    if not isinstance(usage, Mapping):
        return None, None
    t_in = next((_num(usage[k]) for k in ("inputTokens", "prompt_tokens", "input_tokens") if k in usage), None)
    t_out = next((_num(usage[k]) for k in ("outputTokens", "completion_tokens", "output_tokens") if k in usage), None)
    return t_in, t_out


def gateway_reported(raw: Any) -> float | None:
    if not isinstance(raw, Mapping):
        return None
    for key in ("providerMetadata", "provider_metadata"):
        gw = (raw.get(key) or {}).get("gateway") if isinstance(raw.get(key), Mapping) else None
        if isinstance(gw, Mapping) and (c := _num(gw.get("cost"))) is not None:
            return c
    return None


def charge_gateway(raw: Any, model: str, est: float, overrides: Mapping[str, Any] | None = None) -> Charge:
    """Vercel AI Gateway: the reported cost against usage tokens x the model's price."""
    price = model_price(model, overrides)
    t_in, t_out = tokens(raw.get("usage") if isinstance(raw, Mapping) else None)
    expected = None if price is None or t_in is None else t_in * price[0] + (t_out or 0.0) * price[1]
    reported = gateway_reported(raw)
    if reported is not None:
        return Charge(reported, "reported", reported, expected)
    if expected is not None:
        return Charge(expected, "usage_x_price")
    return Charge(est, "estimate")


def estimate_jev(body: Mapping[str, Any], model: str, overrides: Mapping[str, Any] | None = None) -> float:
    price = model_price(model, overrides) or (0.0, 0.0)
    t_in = math.ceil(len(canonical(body)) / JEV_CHARS_PER_TOKEN) + JEV_OVERHEAD_TOKENS
    return t_in * price[0]


def estimate_chat(messages: Any, max_tokens: int, model: str, overrides: Mapping[str, Any] | None = None) -> float:
    price = model_price(model, overrides) or (0.0, 0.0)
    return (math.ceil(len(canonical(messages)) / 3) + 100) * price[0] + max_tokens * price[1]


# ------------------------------------------------------------------------------------------- ledger
@dataclass
class RunTotals:
    usd: float = 0.0
    calls: dict[str, int] = field(default_factory=dict)  # provider spec -> paid calls (cache hits are not counted)
    stopped: str | None = None


class Ledger:
    """data/providers/ledger.jsonl: one line per call (cache hits too, at $0). Lines are appended under a file lock,
    so several processes can share it; totals per run are read incrementally."""

    def __init__(self, path: Path | str | None) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._offset = 0
        self._runs: dict[str, RunTotals] = {}

    def write(self, entry: Mapping[str, Any]) -> None:
        if self.path is None:
            return
        line = (json.dumps(entry, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "ab") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                try:
                    fh.write(line)
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)

    def totals(self, run_id: str) -> RunTotals:
        """This run's spend, paid calls per provider, and whether the price guard stopped it."""
        if self.path is None or not self.path.exists():
            return self._runs.get(run_id, RunTotals())
        with self._lock:
            with open(self.path, "rb") as fh:
                fh.seek(self._offset)
                chunk = fh.read()
            end = chunk.rfind(b"\n") + 1  # a line still being written is read next time
            self._offset += end
            for raw in chunk[:end].splitlines():
                if not raw.strip():
                    continue
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue  # a damaged line is skipped, never counted as zero spend silently: it stays in the file
                t = self._runs.setdefault(str(e.get("run_id")), RunTotals())
                if e.get("cached"):
                    continue
                t.usd += float(e.get("cost") or 0.0)
                if e.get("outcome") not in ("refused",):
                    t.calls[e.get("provider")] = t.calls.get(e.get("provider"), 0) + 1
                if e.get("outcome") == "price_drift" and not t.stopped:
                    t.stopped = e.get("error") or "price guard"
            t = self._runs.get(run_id, RunTotals())
            return RunTotals(t.usd, dict(t.calls), t.stopped)


# ------------------------------------------------------------------------------------------- cache
class Cache:
    """data/providers/cache.sqlite: answers keyed by provider + request. A repeat costs $0."""

    def __init__(self, path: Path | str | None) -> None:
        self.path = Path(path) if path else None
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def _conn(self) -> sqlite3.Connection | None:
        if self.path is None:
            return None
        if self._db is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = sqlite3.connect(str(self.path), timeout=60, check_same_thread=False, isolation_level=None)
            db.execute("PRAGMA busy_timeout = 60000")
            db.execute("CREATE TABLE IF NOT EXISTS provider_cache (key TEXT PRIMARY KEY, provider TEXT, kind TEXT, "
                       "request TEXT, response TEXT, raw TEXT, meta TEXT, cost REAL, created_at TEXT, run_id TEXT)")
            self._db = db
        return self._db

    @staticmethod
    def key(provider: str, kind: str, request: Mapping[str, Any]) -> str:
        return sha256(canonical({"v": 1, "provider": provider, "kind": kind, "request": request}))

    def get(self, key: str) -> dict | None:
        with self._lock:
            db = self._conn()
            if db is None:
                return None
            row = db.execute("SELECT response, raw, meta, cost FROM provider_cache WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        return {"response": json.loads(row[0]), "raw": json.loads(row[1]) if row[1] else None,
                "meta": json.loads(row[2]) if row[2] else {}, "cost": row[3]}

    def put(self, key: str, provider: str, kind: str, request: Mapping[str, Any], response: Any, raw: Any,
            meta: Mapping[str, Any], cost: float, run_id: str) -> None:
        with self._lock:
            db = self._conn()
            if db is None:
                return
            db.execute("INSERT OR REPLACE INTO provider_cache VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (key, provider, kind, canonical(request), canonical(response), canonical(raw), canonical(meta),
                        cost, now(), run_id))

    def add_new(self, entries: list[tuple]) -> int:
        """Many answers in one transaction, each (key, provider, kind, request, response, raw, meta, cost, run_id); a key
        already cached is left as it is. Returns how many were added."""
        with self._lock:
            db = self._conn()
            if db is None or not entries:
                return 0
            before = db.total_changes
            db.execute("BEGIN")
            try:
                db.executemany("INSERT OR IGNORE INTO provider_cache VALUES (?,?,?,?,?,?,?,?,?,?)",
                               [(k, p, kd, canonical(rq), canonical(rs), canonical(rw), canonical(m), c, now(), r)
                                for k, p, kd, rq, rs, rw, m, c, r in entries])
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
            return db.total_changes - before

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None
