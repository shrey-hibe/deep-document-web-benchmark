"""The $0 replay: a stored session's map copied into a new pipeline session, documents from the HTTP cache only, Jev from
the provider cache only, model answers from the response cache only. Anything not cached is a miss: counted, reported,
never fetched or paid for.

- `copy_map`: the session row, the body, the accepted and mapped lists, their views, the links as the map left them
  (rule kind, date, governing; Jev's link answers when the map asked them) and the meetings. Nothing the agent did after
  the map (reads, skips, passages, events) is copied. The source store is opened read-only.
- `CacheOnlyHttp`: the navigator's HTTP cache (data/cache/deep/), never the network.
- `ReplayJev`: the provider cache, read-only. First the exact request a live run would send (`tools.label_passages`'
  state and questions); else, with `match="text"`, a cached BUYING_EVENT answer on the same passage text for the same
  body, preferring the calibrated questions (run jev-calibration-B) over nav3's older ones. Each label says where it
  came from (`via`), and the counts are printed, so a replay on older questions is never mistaken for an exact one.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

from ..deep import state as nav_state
from ..deep import web
from ..deep.forks import jev_questions
from ..deep.tools import compact
from ..providers import JEV
from ..providers import core as providers_core
from ..providers import cost as cost_mod
from ..providers import settings as settings_mod

TEXT_PREFERENCE = ("jev-calibration-B", "router:20261105", "router:20261104", "router:20261103-nav2", "router:20261103-nav1")


def store_path(run: str) -> Path:
    return nav_state.NAV / f"{run}.sqlite"


def read_only(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"no stored run at {path}")
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def _row(r) -> dict:
    d = dict(r)
    for k in ("data", "jev", "card"):
        if d.get(k):
            d[k] = json.loads(d[k])
    return d


def copy_map(src: Path, src_sid: str, st, sid: str, run: str, arm: str) -> dict:
    """Copy the map of session `src_sid` (store `src`, read-only) into a new session `sid` of State `st`."""
    db = read_only(src)
    sess = db.execute("SELECT * FROM sessions WHERE session = ?", [src_sid]).fetchone()
    if not sess:
        raise SystemExit(f"no session {src_sid} in {src}")
    sess = _row(sess)
    if st.session(sid):
        raise SystemExit(f"session {sid} exists already: start a new run (--run) to replay again")
    st.start(sid, run_id=run, arm=arm, body=sess["body"], body_id=sess["body_id"], query=sess.get("query") or "q1",
             since=sess.get("since"), started_at=nav_state.now(), card=sess.get("card") or {}, homepage=sess.get("homepage"),
             status="started")
    first_tool: dict[str, str] = {}
    for r in db.execute("SELECT node, tool FROM events WHERE session = ? AND kind = 'link' ORDER BY seq", [src_sid]):
        first_tool.setdefault(r["node"], r["tool"])
    links: dict[str, dict] = {}
    for r in db.execute("SELECT * FROM events WHERE session = ? AND kind = 'link' AND tool IN ('map', 'map:jev') ORDER BY seq", [src_sid]):
        if first_tool.get(r["node"]) == "map":
            links[r["node"]] = _row(r)
    rest = [_row(r) for r in db.execute("SELECT * FROM nodes WHERE session = ? AND kind IN ('page', 'view', 'meeting') ORDER BY seq", [src_sid])]
    lists = [n for n in rest if n["kind"] == "page" and n["status"] in ("accepted", "mapped")]
    others = [n for n in rest if n["kind"] in ("view", "meeting")]
    db.close()
    with st.batch():
        st.put(sid, "B", "body", "started", url=sess.get("homepage"), tool="copy-map",
               data={"card": sess.get("card"), "map_from": f"{src.stem}:{src_sid}"})
        for n in lists + others + list(links.values()):
            st.put(sid, n["node"], n["kind"], n["status"], url=n.get("url"), parent=n.get("parent"), data=n.get("data") or {},
                   jev=n.get("jev"), reason=n.get("reason"), tool="copy-map")
    return {"lists": len(lists), "views": sum(1 for n in others if n["kind"] == "view"),
            "meetings": sum(1 for n in others if n["kind"] == "meeting"), "links": len(links)}


class CacheOnlyHttp:
    """The navigator's HTTP cache, never the network. A URL not cached is a miss: an error response, counted."""

    def __init__(self, cache: Path | None = None) -> None:
        self.cache = cache or web.CACHE
        self.hits, self.misses = 0, []

    def get(self, url: str, use_cache: bool = True, fresh_since: str | None = None) -> web.Response:
        key = self.cache / hashlib.sha1(url.encode()).hexdigest()
        if key.with_suffix(".json").exists() and key.with_suffix(".bin").exists():
            body = key.with_suffix(".bin").read_bytes()
            if not (web.OLD_CAP <= len(body) < web.OLD_CAP + 100_000):
                self.hits += 1
                return web.Response(**json.loads(key.with_suffix(".json").read_text()), body=body, from_cache=True)
        self.misses.append(url)
        return web.Response(url, error="replay: not in the HTTP cache (not fetched)")

    def post(self, *a, **k):
        raise RuntimeError("a replay makes no requests")


def norm_ws(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


class ReplayJev:
    """Jev's answers from the provider cache only (read-only). `many` has the shape of `tools.Cx.jev_many`."""

    def __init__(self, cache_path: Path | None = None, match: str = "text", bodies: set[str] | None = None) -> None:
        self.path = Path(cache_path or providers_core.CACHE)
        self.match = match
        self.model = settings_mod.load().judge_model
        self.counts: Counter = Counter()
        self.miss_usd = 0.0
        self.live_usd = 0.0  # what every request would cost live with today's questions (none of them cached yet)
        self.db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False) if self.path.exists() else None
        self._index: dict | None = None
        self.bodies = bodies  # body names (cards) to index; None: all

    def exact(self, state, questions: dict) -> dict | None:
        if self.db is None:
            return None
        req = {"model": self.model, "state": state, "questions": providers_core.check_questions(questions)}
        row = self.db.execute("SELECT response FROM provider_cache WHERE key = ?",
                              [cost_mod.Cache.key(JEV, "judge", req)]).fetchone()
        return json.loads(row[0]) if row else None

    def index(self) -> dict:
        """(body name, passage text) -> (preference rank, run, answers) of every cached buying-event label."""
        if self._index is None:
            self._index = {}
            if self.db is not None:
                for run, req, resp in self.db.execute("SELECT run_id, request, response FROM provider_cache WHERE kind = 'judge' "
                                                      "AND request LIKE '%\"is_event\"%'"):
                    state = (json.loads(req) or {}).get("state") or {}
                    passage, name = state.get("passage"), (state.get("body") or {}).get("name")
                    if not isinstance(passage, str) or (self.bodies is not None and name not in self.bodies):
                        continue
                    rank = next((i for i, p in enumerate(TEXT_PREFERENCE) if (run or "").startswith(p)), len(TEXT_PREFERENCE))
                    key = (name, norm_ws(passage))
                    if key not in self._index or rank < self._index[key][0]:
                        self._index[key] = (rank, run, (json.loads(resp) or {}).get("answers"))
        return self._index

    def many(self, cx, items: list[tuple], qs: dict, budget: str) -> dict[str, tuple]:
        out = {}
        for item in items:
            key, state, *own = item
            questions = jev_questions(qs | (own[0] if own else {}))
            self.live_usd += cost_mod.estimate_jev({"model": self.model, "state": state, "questions": questions}, self.model)
            hit = self.exact(state, questions)
            via = "exact"
            answers = (hit or {}).get("answers") if hit else None
            if answers is None and self.match == "text" and "is_event" in qs and isinstance(state, dict):
                found = self.index().get(((state.get("body") or {}).get("name"), norm_ws(state.get("passage"))))
                if found:
                    answers, via = found[2], f"text:{found[1]}"
            if answers is None:
                self.counts["miss"] += 1
                self.miss_usd += cost_mod.estimate_jev({"model": self.model, "state": state, "questions": questions}, self.model)
                out[key] = (None, 0.0, "replay: Jev's answer is not in the provider cache (not sent)")
                continue
            self.counts[via] += 1
            out[key] = ((compact(answers) or {}) | {"via": via}, 0.0, None)
        return out
