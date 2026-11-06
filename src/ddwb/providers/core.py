"""The provider layer: four kinds of work, one interface each, and the light cost layer around every call.

    p = Providers(run_id="20261105-nav4")              # settings from env, ./providers.toml, ~/.config/...
    p.search("Cortez Colorado city council minutes")    # -> Result, response = [{url, title, excerpt, ...}]
    p.scrape("https://example.com")                    # -> Result, response = {markdown, links, title, ...}
    p.judge(state, questions)                          # -> Result, response = Jev's answer ({"answers": ...})
    p.reason(messages, model="...")                    # -> Result, response = the chat completion (benchmark only)
    p.browse(url)                                      # raises NotAvailable (work in progress)

A call can pin one provider (`provider="api:exa"`); otherwise the configured chain is tried in order, moving on only
when a provider gives no answer (never on an empty answer). Every call, cache hits included, is a line in the ledger.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from .. import paths
from . import cost
from . import mcp as mcp_mod
from . import settings as settings_mod
from .api import Answer, Api
from .errors import (BadRequest, BotWall, BudgetExceeded, ConfigError, LimitReached, NotAvailable, Off, PriceDrift,
                     ProviderError, RunStopped, Unavailable, UseAgent)
from .safety import default_guard, host_of, page_is_wall, redact
from .settings import Settings, Spec, expand, has_ref, parse_spec

PROVIDERS_DATA = paths.DATA / "providers"
LEDGER = PROVIDERS_DATA / "ledger.jsonl"
CACHE = PROVIDERS_DATA / "cache.sqlite"
DEFAULT = object()  # "use LEDGER / CACHE", read when a Providers is made (so tests can point them elsewhere)
OFF = {"search": "search is off in the settings", "scrape": "scrape is off: plain fetch only",
       "judge": "judge (Jev) is off: connect it with DDWB_JUDGE=api:vercel and AI_GATEWAY_API_KEY",
       "browse": "browse is off", "reason": "reason is off"}
QUESTION_TYPES = ("boolean", "choice", "score")


def new_run_id(label: str = "run") -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + re.sub(r"[^A-Za-z0-9_.-]", "_", label)


@dataclass
class Result:
    """One answer: the normalised response, what it cost, whether it came from the cache, and from which provider."""

    kind: str
    provider: str  # the spec, as written (never with a key in it)
    response: Any
    cost: float = 0.0  # charged by this call: $0 on a cache hit
    cached: bool = False
    est_cost: float = 0.0
    cost_source: str = ""
    reported_cost: float | None = None
    expected_cost: float | None = None  # usage x the price table
    raw: Any = None  # the vendor's own answer
    vendor: str | None = None
    tool: str | None = None  # the endpoint or MCP tool used
    model: str | None = None
    usage: Any = None
    latency_ms: int | None = None
    status: str = "ok"  # ok | empty (a search with no results: a result, not an error)
    run_id: str = ""

    @property
    def answers(self) -> dict:
        """judge: {question: {type, probability | choice | score, ...}}."""
        return (self.response or {}).get("answers") or {} if isinstance(self.response, Mapping) else {}

    @property
    def text(self) -> str | None:
        """reason: the first choice's message."""
        try:
            content = self.response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return None
        return content if isinstance(content, str) else None

    @property
    def cache_hit(self) -> bool:
        return self.cached

    @property
    def route(self) -> str:
        return f"{self.provider}/{self.model}" if self.model else self.provider


def _domain(domain: str | None) -> str | None:
    if not domain or not domain.strip():
        return None
    d = domain.strip()
    if "://" in d:
        d = host_of(d)
    return d.split("/")[0]


def check_questions(questions: Any) -> dict:
    """Jev's question shape: {name: {type: boolean | choice | score, instructions, criteria?}}."""
    if not isinstance(questions, Mapping) or not questions:
        raise BadRequest("judge needs questions as {name: {type, instructions}}")
    for name, q in questions.items():
        if not isinstance(q, Mapping) or q.get("type") not in QUESTION_TYPES or not str(q.get("instructions") or "").strip():
            raise BadRequest(f"judge: question {name!r} needs type boolean|choice|score and instructions")
    return dict(questions)


class Providers:
    def __init__(self, settings: Settings | None = None, *, run_id: str | None = None,
                 max_calls: int | None | object = DEFAULT, max_usd: float | None | object = DEFAULT, approved_by: str = "", ledger_path: Path | str | None | object = DEFAULT,
                 cache_path: Path | str | None | object = DEFAULT, api: Api | None = None, connector: Any = None,
                 host_guard: Any = None, env: Mapping[str, str] | None = None) -> None:
        """max_calls / max_usd: this run's limits (default: the settings'; None: no limit). approved_by: free text,
        recorded with each call when given. ledger_path / cache_path: default data/providers/ (None: no ledger, no cache)."""
        ledger_path = LEDGER if ledger_path is DEFAULT else ledger_path
        cache_path = CACHE if cache_path is DEFAULT else cache_path
        self.env = os.environ if env is None else env
        self.settings = settings if settings is not None else settings_mod.load(self.env)
        self.run_id = run_id or new_run_id()
        self.max_calls = self.settings.max_calls if max_calls is DEFAULT else max_calls
        self.max_usd = self.settings.max_usd if max_usd is DEFAULT else max_usd
        self.approved_by = (approved_by or "").strip()
        self.ledger = cost.Ledger(ledger_path)
        self.cache = cost.Cache(cache_path)
        self.api = api or Api()
        self.connector = connector or mcp_mod.SdkConnector()
        self._guard = host_guard
        self._lock = threading.Lock()
        self._reserved = 0.0
        self._inflight: dict[str, int] = {}
        self.session = hashlib.sha1(f"ddwb:{self.run_id}".encode()).hexdigest()  # Parallel's free tier groups calls by it

    def __repr__(self) -> str:  # never holds a key
        return f"Providers(run_id={self.run_id!r}, max_calls={self.max_calls}, max_usd={self.max_usd})"

    @property
    def guard(self):
        if self._guard is None:
            self._guard = default_guard()
        return self._guard

    # ------------------------------------------------------------------------------- the four kinds
    def search(self, query: str, *, objective: str | None = None, domain: str | None = None, n: int = 10,
               provider: str | None = None) -> Result:
        if not isinstance(query, str) or not query.strip():
            raise BadRequest("search needs a query")
        n = int(n)
        if not 1 <= n <= 100:
            raise BadRequest("search: n must be 1-100")
        req = {"query": query.strip(), "objective": objective, "domain": _domain(domain), "n": n}
        return self._run("search", {k: v for k, v in req.items() if v is not None}, provider)

    def scrape(self, url: str, *, objective: str | None = None, provider: str | None = None) -> Result:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise BadRequest("scrape needs an http(s) URL")
        req = {"url": url} | ({"objective": objective} if objective else {})
        return self._run("scrape", req, provider)

    def judge(self, state: Any, questions: Mapping[str, Any], *, model: str | None = None,
              provider: str | None = None) -> Result:
        if not isinstance(state, (str, Mapping, list)):
            raise BadRequest("judge needs a state (string, object or list)")
        req = {"model": model or self.settings.judge_model, "state": state, "questions": check_questions(questions)}
        return self._run("judge", req, provider)

    def reason(self, messages: Any, model: str, *, max_tokens: int | None = None, temperature: float | None = None,
               provider: str | None = None) -> Result:
        """For the benchmark harness: a chat model answers. `messages`: a list of {role, content}, or a prompt."""
        msgs = [{"role": "user", "content": messages}] if isinstance(messages, str) else messages
        if not isinstance(msgs, list) or not msgs or not all(isinstance(m, Mapping) and "role" in m and "content" in m for m in msgs):
            raise BadRequest("reason needs messages: a non-empty list of {role, content}, or a prompt")
        if cost.model_price(model, self.settings.prices.get("models")) is None:
            raise ConfigError(f"no price for model {model!r}: add it under [prices.models] in providers.toml "
                              "(input_per_mtok, output_per_mtok) so its cost can be checked")
        req: dict = {"model": model, "messages": msgs, "max_tokens": int(max_tokens or cost.DEFAULT_MAX_TOKENS)}
        if temperature is not None:
            req["temperature"] = temperature
        return self._run("reason", req, provider)

    def browse(self, url: str, task: str | None = None, *, provider: str | None = None) -> Result:
        spec = provider or ", ".join(s.text for s in self.settings.chain("browse"))
        raise NotAvailable(f"browse is work in progress: not available yet (configured: {spec})", provider=spec)

    # ------------------------------------------------------------------------------- one call, through the chain
    def _run(self, kind: str, req: dict, provider: str | None) -> Result:
        if provider is not None:
            chain = settings_mod.check_chain(kind, (parse_spec(provider),))
        else:
            chain = self.settings.chain(kind)
        failures: list[str] = []
        for spec in chain:
            try:
                return self._one(kind, spec, req)
            except (UseAgent, Off) as end:
                if failures:
                    raise type(end)(f"{end} (before it: {'; '.join(failures)})", provider=spec.text) from None
                raise
            except ProviderError as error:
                error.provider = error.provider or spec.text
                if provider is not None or not error.fallback:
                    raise
                failures.append(f"{spec.text}: {error}")
        raise Unavailable(f"{kind}: no provider answered: {'; '.join(failures)}")

    def _one(self, kind: str, spec: Spec, req: dict) -> Result:
        if spec.family == "off":
            raise Off(OFF[kind], provider=spec.text)
        if spec.family == "agent":
            raise UseAgent(f"use your own web search tool for: {req.get('query')}", provider=spec.text)
        if kind == "browse":
            raise NotAvailable("browse is work in progress: not available yet", provider=spec.text)
        headers = dict(self.settings.headers.get(kind) or {}) if spec.family == "mcp" else {}
        keyed = spec.family == "api" or has_ref(spec.url or "") or any(has_ref(v) for v in headers.values())
        host = host_of(req["url"]) if kind == "scrape" else None
        if host:
            why = self.guard.blocked(host)
            if why:
                self._log(kind, spec, req, outcome="refused", error=f"bot wall: {why}")
                raise BotWall(f"{host} is stopped for a bot wall ({why}); never worked around", provider=spec.text)

        ckey = cost.Cache.key(spec.text, kind, req)
        hit = self.cache.get(ckey)
        if hit is not None:
            meta = hit.get("meta") or {}
            res = Result(kind, spec.text, hit["response"], 0.0, True, cost_source=f"cache ({meta.get('cost_source')})",
                         raw=hit.get("raw"), vendor=spec.vendor, tool=meta.get("tool"), model=meta.get("model"),
                         usage=meta.get("usage"), status=meta.get("status", "ok"), run_id=self.run_id)
            self._log(kind, spec, req, outcome="cached", cached=True, tool=res.tool, model=res.model,
                      cost_source="cache", original_cost=hit.get("cost"))
            return res

        est = self._estimate(kind, spec, req, keyed)
        self._reserve(kind, spec, req, est)
        started = time.monotonic()
        try:
            try:
                answer = self._send(kind, spec, req, headers)
            except ProviderError as error:
                self._log(kind, spec, req, outcome="failed", est=est, error=f"{type(error).__name__}: {error}",
                          latency_ms=int((time.monotonic() - started) * 1000))
                raise
            charge = answer.charge
            if spec.family == "mcp":
                charge = mcp_mod.adapter_for(spec.vendor).charge(kind, req, answer) if keyed else cost.Charge(0.0, "keyless")
            ms = int((time.monotonic() - started) * 1000)
            status = "empty" if kind == "search" and not answer.response else "ok"
            res = Result(kind, spec.text, answer.response, charge.cost, False, est, charge.source, charge.reported,
                         charge.expected, answer.raw, spec.vendor, answer.tool, answer.model, answer.usage, ms, status,
                         self.run_id)
            if kind == "scrape" and (why := page_is_wall(answer.response)):
                self.guard.block(host, why)
                self._log(kind, spec, req, outcome="bot_wall", est=est, res=res, error=why)
                raise BotWall(f"{req['url']}: {why}: the page is dropped and {host} is stopped", provider=spec.text, result=res)
            drift = charge.drift if keyed else None
            self.cache.put(ckey, spec.text, kind, req, res.response, res.raw,
                           {"tool": res.tool, "model": res.model, "usage": res.usage, "status": status,
                            "cost_source": res.cost_source}, res.cost, self.run_id)
            self._log(kind, spec, req, outcome="price_drift" if drift else status, est=est, res=res,
                      error=f"price guard: {drift}" if drift else None)
            if drift:
                raise PriceDrift(f"{spec.text}: {drift}. The answer is kept; this run's paid calls are stopped",
                                 provider=spec.text, result=res)
            return res
        finally:
            self._release(spec.text, est)

    def _send(self, kind: str, spec: Spec, req: dict, headers: dict) -> Answer:
        secrets: list[str] = []
        try:
            if spec.family == "api":
                key, secrets = expand(self.settings.key_ref(spec.vendor), self.env)
                prices = self.settings.prices.get("models")
                if kind == "search":
                    return self.api.search(spec.vendor, key, req)
                if kind == "scrape":
                    return self.api.scrape(key, req, spec.vendor)
                if kind == "judge":
                    return self.api.judge(key, req["state"], req["questions"], req["model"], prices)
                if kind == "reason":
                    return self.api.reason(key, req, prices)
                raise ConfigError(f"{kind} cannot use {spec.text}")
            url, used = expand(spec.url or "", self.env)
            secrets += used
            real_headers = {}
            for name, value in headers.items():
                real_headers[name], used = expand(value, self.env)
                secrets += used
            return mcp_mod.call(self.connector, url, real_headers, spec.vendor, kind, req, self.session)
        except ProviderError as error:
            clean = type(error)(redact(str(error), secrets), provider=spec.text, result=error.result)
            raise clean from None
        except Exception as error:  # an adapter or transport bug is an error too, never an empty answer
            raise Unavailable(redact(f"{type(error).__name__}: {str(error)[:300]}", secrets), provider=spec.text) from None

    # ------------------------------------------------------------------------------- limits and the ledger
    def _estimate(self, kind: str, spec: Spec, req: Mapping[str, Any], keyed: bool) -> float:
        if not keyed:
            return 0.0
        prices = self.settings.prices.get("models")
        if kind == "judge":
            return cost.estimate_jev(req, req["model"], prices)
        if kind == "reason":
            return cost.estimate_chat(req["messages"], req["max_tokens"], req["model"], prices)
        if kind == "search" and spec.vendor in ("parallel", "exa"):
            return cost.search_price(spec.vendor, int(req.get("n") or 10))
        if kind == "scrape" and spec.vendor in ("firecrawl", "treg"):
            return cost.PRICES["firecrawl"]["scrape"]
        return 0.0

    def totals(self) -> cost.RunTotals:
        return self.ledger.totals(self.run_id)

    def _reserve(self, kind: str, spec: Spec, req: Mapping[str, Any], est: float) -> None:
        with self._lock:
            t = self.ledger.totals(self.run_id)
            reason = None
            if t.stopped:
                error: ProviderError = RunStopped(f"this run's paid calls were stopped earlier: {t.stopped}", provider=spec.text)
            elif self.max_calls is not None and t.calls.get(spec.text, 0) + self._inflight.get(spec.text, 0) + 1 > self.max_calls:
                error = LimitReached(f"{spec.text}: this run has used its {self.max_calls} calls (max_calls)", provider=spec.text)
            elif self.max_usd is not None and t.usd + self._reserved + est > self.max_usd + 1e-9:
                error = BudgetExceeded(f"{spec.text}: ${t.usd + self._reserved:.4f} spent or in flight + estimate ${est:.4f} "
                                       f"would pass this run's ${self.max_usd:.2f} (max_usd)", provider=spec.text)
            else:
                error = None
                self._inflight[spec.text] = self._inflight.get(spec.text, 0) + 1
                self._reserved += est
            if error is not None:
                reason = str(error)
        if error is not None:
            self._log(kind, spec, req, outcome="refused", est=est, error=reason)
            raise error

    def _release(self, provider: str, est: float) -> None:
        with self._lock:
            self._inflight[provider] = max(0, self._inflight.get(provider, 0) - 1)
            self._reserved = max(0.0, self._reserved - est)

    def _log(self, kind: str, spec: Spec, req: Mapping[str, Any], *, outcome: str, est: float = 0.0,
             res: Result | None = None, cached: bool = False, error: str | None = None, tool: str | None = None,
             model: str | None = None, cost_source: str | None = None, latency_ms: int | None = None,
             original_cost: float | None = None) -> None:
        e = {"ts": cost.now(), "run_id": self.run_id, "kind": kind, "provider": spec.text, "vendor": spec.vendor,
             "tool": res.tool if res else tool, "model": (res.model if res else model) or req.get("model"),
             "request": cost.sha256(cost.canonical(req))[:16], "cached": cached, "outcome": outcome,
             "est_cost": round(est, 9), "cost": round(res.cost, 9) if res else 0.0,
             "cost_source": res.cost_source if res else (cost_source or "not_billed"),
             "reported_cost": res.reported_cost if res else None, "expected_cost": res.expected_cost if res else None,
             "latency_ms": res.latency_ms if res else latency_ms}
        if original_cost is not None:
            e["original_cost"] = original_cost
        if error:
            e["error"] = error[:500]
        if self.approved_by:
            e["approved_by"] = self.approved_by
        self.ledger.write(e)

    def describe(self) -> dict:
        return {"run_id": self.run_id, "max_calls": self.max_calls, "max_usd": self.max_usd,
                "approved_by": self.approved_by or None, **self.settings.describe()}
