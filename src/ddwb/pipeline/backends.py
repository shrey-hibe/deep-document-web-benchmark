"""Who fills the form: model backends behind one interface, a response cache, and the two deciders that need no model.

Backends (one call: a prompt in, a reply out), picked by setting (`--backend`, else DDWB_PIPELINE_BACKEND):
    gateway       the provider layer's `reason` kind (Vercel AI Gateway: anthropic/claude-haiku-5.5, anthropic/claude-sonnet-5.5);
                  paid, counted against the run's model cap
    claude-code   Claude Code headless (`claude -p --model haiku|sonnet`) on a Claude Max plan: no cash cost, the API
                  equivalent is recorded
    replay        answers from the response cache only; a prompt not cached is a miss, counted, never sent

Every live answer is kept in the response cache (data/pipeline/responses.sqlite), keyed by (backend family and model,
prompt), so a run can be replayed at $0 and only a changed prompt misses.

Deciders (a batch of item cards in, the form's objects out):
    ModelDecider     a model through a backend, with the fixed form (form.prompt / form.parse)
    JevDecider       Jev's own labels as the answer (event if p >= 0.5 with a buying type): a $0 floor
    RecordedDecider  what a stored navigator run recorded on the same passages (by text): the same judgements with the
                     pipeline's order and coverage, at $0
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import paths
from ..providers import cost as cost_mod
from . import form

RESPONSES = paths.DATA / "pipeline" / "responses.sqlite"
GATEWAY_MODELS = {"haiku": "anthropic/claude-haiku-5.5", "sonnet": "anthropic/claude-sonnet-5.5"}
CLAUDE_CODE_MODELS = {"haiku": "haiku", "sonnet": "sonnet"}
MAX_TOKENS = 4000
SYSTEM = "You fill a fixed JSON form about public meeting records. Reply with the JSON array only."


# ------------------------------------------------------------------------------------------- the response cache
class ResponseCache:
    """Model replies keyed by (model, prompt). Read-only for a replay: a replay never writes."""

    def __init__(self, path: Path | str | None = None, read_only: bool = False) -> None:
        self.path = Path(path or RESPONSES)
        self.read_only = read_only
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    @staticmethod
    def key(model: str, prompt: str) -> str:
        return hashlib.sha256(f"{model}\n\x00\n{prompt}".encode()).hexdigest()

    def _conn(self) -> sqlite3.Connection | None:
        if self._db is None:
            if self.read_only:
                if not self.path.exists():
                    return None
                self._db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
            else:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._db = sqlite3.connect(str(self.path), timeout=60, check_same_thread=False, isolation_level=None)
                self._db.execute("CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, model TEXT, prompt TEXT, "
                                 "reply TEXT, meta TEXT, created_at TEXT)")
        return self._db

    def get(self, model: str, prompt: str) -> dict | None:
        with self._lock:
            db = self._conn()
            if db is None:
                return None
            try:
                row = db.execute("SELECT reply, meta FROM responses WHERE key = ?", [self.key(model, prompt)]).fetchone()
            except sqlite3.OperationalError:  # an empty file with no table yet
                return None
        return {"reply": row[0], "meta": json.loads(row[1] or "{}")} if row else None

    def put(self, model: str, prompt: str, reply: str, meta: dict) -> None:
        if self.read_only:
            raise RuntimeError("a replay never writes to the response cache")
        with self._lock:
            self._conn().execute("INSERT OR REPLACE INTO responses VALUES (?, ?, ?, ?, ?, ?)",
                                 [self.key(model, prompt), model, prompt, reply, json.dumps(meta, default=str),
                                  cost_mod.now()])


@dataclass
class Reply:
    text: str | None = None
    cost: float = 0.0  # cash
    api_usd: float | None = None  # what the tokens would cost on the API (Claude Code on the Max plan)
    cached: bool = False
    miss: bool = False  # replay: not in the cache, not sent
    error: str | None = None
    model: str = ""
    ms: int = 0
    tokens_in: float | None = None
    tokens_out: float | None = None


# ------------------------------------------------------------------------------------------- backends
class Backend:
    family = ""
    live = True
    models: dict[str, str] = {}

    def __init__(self, cache: ResponseCache | None = None, models: dict | None = None) -> None:
        self.cache = cache or ResponseCache()
        self.models = dict(models or self.models)

    def model_id(self, tier: str) -> str:
        return self.models.get(tier, tier)

    def key_model(self, tier: str) -> str:
        return f"{self.family}:{self.model_id(tier)}"

    def ask(self, prompt: str, tier: str) -> Reply:
        model = self.key_model(tier)
        hit = self.cache.get(model, prompt)
        if hit is not None:
            m = hit["meta"]
            return Reply(hit["reply"], 0.0, m.get("api_usd"), True, model=model, tokens_in=m.get("tokens_in"),
                         tokens_out=m.get("tokens_out"))
        started = time.monotonic()
        reply = self.send(prompt, tier)
        reply.model, reply.ms = model, int((time.monotonic() - started) * 1000)
        if reply.text and not reply.error:
            self.cache.put(model, prompt, reply.text, {"cost": reply.cost, "api_usd": reply.api_usd, "tokens_in": reply.tokens_in,
                                                       "tokens_out": reply.tokens_out, "ms": reply.ms})
        return reply

    def send(self, prompt: str, tier: str) -> Reply:
        raise NotImplementedError

    def estimate(self, prompt: str, tier: str) -> float | None:
        """The cash cost of one call at the price table, when it has the model (for a replay's miss report)."""
        price = cost_mod.model_price(GATEWAY_MODELS.get(tier, tier))
        if price is None:
            return None
        return (len(prompt) / 3 + 100) * price[0] + 60 * form.BATCH_ITEMS * price[1]


class GatewayBackend(Backend):
    """The provider layer's `reason` kind (Vercel AI Gateway). `providers` is made by the caller with the run's caps."""

    family = "gateway"
    models = GATEWAY_MODELS

    def __init__(self, providers, cache: ResponseCache | None = None, models: dict | None = None) -> None:
        super().__init__(cache, models)
        self.providers = providers

    def send(self, prompt: str, tier: str) -> Reply:
        try:
            res = self.providers.reason([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                                        self.model_id(tier), max_tokens=MAX_TOKENS, temperature=0)
        except Exception as error:  # a failure is a failure, never an empty answer
            return Reply(error=f"{type(error).__name__}: {str(error)[:300]}")
        t_in, t_out = cost_mod.tokens(res.usage)
        return Reply(res.text, res.cost or 0.0, res.cost or 0.0, tokens_in=t_in, tokens_out=t_out,
                     error=None if res.text else "no text in the reply")


class ClaudeCodeBackend(Backend):
    """Claude Code headless on the Max plan: `claude -p --model <haiku|sonnet>`, no tools, no session kept, run from
    an empty folder so no project instructions are loaded. `runner` is subprocess.run (a test passes a fake)."""

    family = "claude-code"
    models = CLAUDE_CODE_MODELS

    def __init__(self, cache: ResponseCache | None = None, models: dict | None = None, runner=None, binary: str | None = None,
                 timeout: float = 600) -> None:
        super().__init__(cache, models)
        self.runner = runner or subprocess.run
        self.binary = binary or os.environ.get("DDWB_CLAUDE_BIN") or "claude"
        self.timeout = timeout

    def command(self, tier: str) -> list[str]:
        return [self.binary, "-p", "--model", self.model_id(tier), "--output-format", "json", "--tools", "",
                "--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands", "--system-prompt", SYSTEM]

    def send(self, prompt: str, tier: str) -> Reply:
        if self.runner is subprocess.run and not shutil.which(self.binary):
            return Reply(error=f"{self.binary} is not on the PATH")
        with tempfile.TemporaryDirectory(prefix="ddwb-pipeline-") as empty:
            try:
                res = self.runner(self.command(tier), input=prompt, capture_output=True, text=True, timeout=self.timeout, cwd=empty)
            except Exception as error:
                return Reply(error=f"{type(error).__name__}: {str(error)[:300]}")
        if res.returncode != 0:
            return Reply(error=f"claude exited {res.returncode}: {(res.stderr or res.stdout or '')[:300]}")
        try:
            out = json.loads(res.stdout)
        except json.JSONDecodeError:
            return Reply(error=f"claude printed no JSON: {res.stdout[:200]}")
        if out.get("is_error"):
            return Reply(error=f"claude: {str(out.get('result'))[:300]}")
        usage = out.get("usage") or {}
        return Reply(out.get("result"), 0.0, out.get("total_cost_usd"), tokens_in=usage.get("input_tokens"),
                     tokens_out=usage.get("output_tokens"), error=None if out.get("result") else "no result")


class CacheOnlyBackend(Backend):
    """A replay: answers from the response cache only, as recorded by a live backend of `family`."""

    live = False

    def __init__(self, cache: ResponseCache, family: str = "gateway") -> None:
        super().__init__(cache, GATEWAY_MODELS if family == "gateway" else CLAUDE_CODE_MODELS)
        self.family = family

    def send(self, prompt: str, tier: str) -> Reply:
        return Reply(miss=True, error="replay: this prompt's answer is not in the response cache (not sent)")


# ------------------------------------------------------------------------------------------- deciders
@dataclass
class Asked:
    """One decider call on a batch of cards."""

    objects: list | None
    error: str | None = None
    cost: float = 0.0
    api_usd: float | None = None
    cached: bool = False
    miss: bool = False
    by: str = ""
    prompt_chars: int = 0
    estimate: float | None = None  # a missed call's cash cost at the price table
    model_call: bool = True
    extra: dict = field(default_factory=dict)


class ModelDecider:
    deterministic = False

    def __init__(self, backend: Backend, tier: str) -> None:
        self.backend, self.tier = backend, tier
        self.by = backend.key_model(tier)

    @property
    def live(self) -> bool:
        return self.backend.live

    def ask(self, meeting: dict, cards: list[tuple[int, dict]], notes: dict[int, str] | None = None) -> Asked:
        p = form.prompt(meeting, cards, notes)
        r = self.backend.ask(p, self.tier)
        if r.text is None:
            return Asked(None, r.error or "no reply", r.cost, r.api_usd, r.cached, r.miss, self.by, len(p),
                         self.backend.estimate(p, self.tier) if r.miss else None)
        objects, why = form.parse(r.text)
        return Asked(objects, why, r.cost, r.api_usd, r.cached, False, self.by, len(p))


def first_words(t: str, n: int = 160) -> str:
    """The opening words of a text, as a quote that occurs in it."""
    lines = [x for x in (t or "").split("\n") if len(form.norm(x)) >= form.QUOTE_MIN]
    line = lines[0] if lines else (t or "")
    return line if len(line) <= n else line[:n].rsplit(" ", 1)[0]


class JevDecider:
    """Jev's labels as the answer: no model. An item Jev has no label for gets no answer (left undecided)."""

    deterministic, live = True, False
    by = "jev-labels"

    def ask(self, meeting: dict, cards: list[tuple[int, dict]], notes: dict[int, str] | None = None) -> Asked:
        out = []
        for n, it in cards:
            p = it.get("jev_p")
            if p is None:
                continue
            if p >= 0.5 and it.get("jev_type") in form.BUYING_TYPES:
                out.append({"item": n, "event": True, "type": it["jev_type"],
                            "action": it.get("jev_action") if it.get("jev_action") in form.ACTIONS else "unclear",
                            "vendor": (it.get("orgs") or [None])[0], "amount": max(it["amounts"]) if it.get("amounts") else None,
                            "what": (it.get("title") or "")[:120] or "item", "quote": first_words(it.get("text")), "unsure": False})
            else:
                out.append({"item": n, "event": False})
        return Asked(out, by=self.by, model_call=False)


class RecordedDecider:
    """What a stored navigator session recorded on the same passages (matched by text): each event recorded from one of
    the item's passages is an answer; an item whose passages that session read but recorded nothing from is "no event";
    an item it never read gets no answer (left undecided, counted)."""

    deterministic, live = True, False

    def __init__(self, src_db: str | Path, src_sid: str) -> None:
        self.by = f"recorded:{src_sid}"
        db = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        events = {}
        for r in db.execute("SELECT node, status, data FROM nodes WHERE session = ? AND kind = 'event'", [src_sid]):
            if r["status"] == "recorded":
                events[r["node"]] = json.loads(r["data"])
        self.index: dict[str, list[dict]] = {}
        for r in db.execute("SELECT status, url, data FROM nodes WHERE session = ? AND kind = 'passage'", [src_sid]):
            d = json.loads(r["data"])
            evs = [events[e] for e in d.get("events") or [] if e in events]
            self.index.setdefault(form.norm(d.get("text")), []).append({"status": r["status"], "url": r["url"], "events": evs})
        db.close()

    def ask(self, meeting: dict, cards: list[tuple[int, dict]], notes: dict[int, str] | None = None) -> Asked:
        out = []
        for n, it in cards:
            found = []
            for ps in it.get("passages") or [{"text": it.get("text")}]:
                hits = self.index.get(form.norm(ps["text"])) or []
                same_doc = [h for h in hits if h["url"] == meeting.get("url")]
                found += [(ps, h) for h in (same_doc or hits)[:1]]
            if not found:
                continue
            events = [(ps, e) for ps, h in found for e in h["events"] if e.get("type") in form.BUYING_TYPES]
            if not events:
                out.append({"item": n, "event": False})
            for ps, e in events:
                out.append({"item": n, "event": True, "type": e["type"], "action": e.get("action"), "vendor": e.get("vendor"),
                            "amount": e.get("amount"), "what": e.get("what") or (it.get("title") or "item")[:120],
                            "quote": first_words(ps["text"]), "unsure": False})
        return Asked(out, by=self.by, model_call=False)
