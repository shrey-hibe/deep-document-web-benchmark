"""Direct vendor APIs, on the user's own keys. The request shapes are the ones the benchmark used: Parallel Search (mode
fast), Exa /search (type auto, never page contents), Firecrawl scrape (markdown and links, no main-content filter, never a
stealth or enhanced proxy; direct, or relayed by treg), and the Vercel AI Gateway (Jev on /v1/evaluate, chat models on
/v1/chat/completions)."""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from . import cost
from .errors import BadRequest, BotWall, Unavailable
from .safety import USER_AGENT, looks_like_wall, redact

URLS = {
    "parallel": "https://api.parallel.ai/v1/search",
    "exa": "https://api.exa.ai/search",
    "firecrawl": "https://api.firecrawl.dev/v2/scrape",
    "treg": "https://treg.to/call/firecrawl.web.scrape",  # the same Firecrawl call, relayed and billed by treg
    "vercel": "https://ai-gateway.vercel.sh/v1",
}
NO_CREDIT = re.compile(r"insufficient[_ ]?(credit|credits|balance|funds|quota)|out of credits?|payment required|"
                       r"no (remaining )?credits?|credit (limit|balance) (reached|exceeded)|exceeded your (current )?quota", re.I)

# (method, url, headers, body, timeout) -> (status, headers, text)
Transport = Callable[[str, str, Mapping[str, str], bytes | None, float], tuple[int, Mapping[str, str], str]]


class TransportFailed(Exception):
    pass


class HttpxTransport:
    """The default transport. Redirects are not followed (an API that redirects is an error to look at)."""

    def __init__(self) -> None:
        import httpx

        self._httpx = httpx
        self._client = httpx.Client(follow_redirects=False)

    def __call__(self, method, url, headers, body, timeout):
        try:
            r = self._client.request(method, url, headers=dict(headers), content=body,
                                     timeout=self._httpx.Timeout(timeout, connect=min(15.0, timeout)))
        except self._httpx.HTTPError as error:
            raise TransportFailed(type(error).__name__) from None
        return r.status_code, dict(r.headers), r.text


@dataclass
class Answer:
    """One provider answer: the normalised response, the vendor's raw response, and what it cost."""

    response: Any
    raw: Any
    charge: cost.Charge
    tool: str | None = None  # the endpoint or MCP tool used
    model: str | None = None
    usage: Any = None
    meta: dict = field(default_factory=dict)


def body_json(body: Any) -> bytes:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def post_json(transport: Transport, url: str, headers: Mapping[str, str], body: Any, *, secrets: list[str],
              timeout: float = 60.0, retries: int = 3, sleep: Callable[[float], None] = time.sleep) -> tuple[Any, int]:
    """POST a JSON body; retry 408, 429, 5xx and transport failures with backoff. Returns (JSON, latency ms).
    Raises Unavailable (no answer: may fall back) or BadRequest (the request was wrong)."""
    data = body_json(body)
    attempt = 0
    while True:
        attempt += 1
        started = time.monotonic()
        try:
            status, resp_headers, text = transport("POST", url, headers, data, timeout)
        except TransportFailed as error:
            status, resp_headers, text = None, {}, f"transport failed ({error})"
        latency = int((time.monotonic() - started) * 1000)
        if status == 200:
            try:
                return json.loads(text), latency
            except ValueError:
                if looks_like_wall(text):
                    raise BotWall(f"{url}: a bot-check page instead of JSON") from None
                raise Unavailable(f"{url}: the answer was not JSON") from None
        detail = redact((text or "").replace("\n", " ")[:300], secrets)
        retry = status is None or status in (408, 429) or 500 <= status < 600
        if retry and not (status == 429 and NO_CREDIT.search(text or "")) and attempt <= retries:
            wait = min(30.0, 2.0 ** (attempt - 1)) + random.uniform(0, 0.25)
            ra = next((v for k, v in resp_headers.items() if k.lower() == "retry-after"), None)
            try:
                wait = max(wait, min(float(ra), 60.0)) if ra else wait
            except ValueError:
                pass
            sleep(wait)
            continue
        if status is None:
            raise Unavailable(f"{url}: {detail} after {attempt} attempt(s)")
        if status == 402 or NO_CREDIT.search(text or ""):
            raise Unavailable(f"{url}: HTTP {status}: no credit ({detail})")
        if retry:
            raise Unavailable(f"{url}: HTTP {status} after {attempt} attempt(s): {detail}")
        if status in (401, 403):
            raise Unavailable(f"{url}: HTTP {status}: refused ({detail})")
        raise BadRequest(f"{url}: HTTP {status}: {detail}")


def headers_for(vendor: str, key: str) -> dict[str, str]:
    h = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if vendor in ("firecrawl", "vercel"):
        h["Authorization"] = "Bearer " + key
    elif vendor == "treg":
        h["X-Treg-Token"] = key
    else:
        h["x-api-key"] = key
    return h


# ------------------------------------------------------------------------------------------- normalising
def excerpt_of(x: Mapping[str, Any]) -> str | None:
    for k in ("excerpts", "highlights"):
        v = x.get(k)
        if isinstance(v, list) and v:
            return "\n".join(str(s) for s in v if s) or None
    for k in ("excerpt", "description", "snippet", "summary", "text", "content"):
        v = x.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return None


def norm_results(items: Any) -> list[dict]:
    """Search results as {url, title, excerpt, published_date, raw}."""
    out = []
    for x in items if isinstance(items, list) else []:
        if not isinstance(x, Mapping):
            continue
        url = x.get("url") or x.get("link")
        if not url:
            continue
        out.append({"url": url, "title": x.get("title"), "excerpt": excerpt_of(x),
                    "published_date": x.get("publishedDate") or x.get("publish_date") or x.get("published_date") or x.get("date"),
                    "raw": dict(x)})
    return out


def norm_scrape(url: str, data: Mapping[str, Any]) -> dict:
    """A scraped page as {url, final_url, title, markdown, links, status, content_type, proxy, error, credits}."""
    data = data.get("data", data) if isinstance(data.get("data"), Mapping) else data
    meta = data.get("metadata") or {}
    title = meta.get("title") or data.get("title") or ""
    if isinstance(title, list):
        title = title[0] if title else ""
    links = [h if isinstance(h, str) else (h.get("url") or h.get("href")) for h in data.get("links") or []]
    return {"url": url, "final_url": meta.get("url") or meta.get("sourceURL") or data.get("url") or url, "title": str(title),
            "markdown": data.get("markdown") or "", "links": [h for h in links if isinstance(h, str)],
            "status": meta.get("statusCode"), "content_type": meta.get("contentType"), "proxy": meta.get("proxyUsed"),
            "error": meta.get("error"), "credits": meta.get("creditsUsed")}


def on_domain(url: str, domain: str) -> bool:
    from .safety import host_of

    h, d = host_of(url), domain.lower().removeprefix("www.")
    return h == d or h.endswith("." + d)


def domain_query(query: str, objective: str | None, domain: str | None) -> tuple[str, str | None]:
    """For providers with no domain parameter: `site:` in the query, and the objective told."""
    if not domain:
        return query, objective
    return f"site:{domain} {query}", (f"{objective} Only pages on {domain}." if objective else None)


# ------------------------------------------------------------------------------------------- vendors
class Api:
    def __init__(self, transport: Transport | None = None, *, sleep: Callable[[float], None] = time.sleep,
                 timeout: float = 60.0, retries: int = 3) -> None:
        self._transport = transport
        self.sleep, self.timeout, self.retries = sleep, timeout, retries

    @property
    def transport(self) -> Transport:
        if self._transport is None:
            self._transport = HttpxTransport()
        return self._transport

    def _post(self, vendor: str, url: str, key: str, body: Any, timeout: float | None = None) -> tuple[Any, int]:
        return post_json(self.transport, url, headers_for(vendor, key), body, secrets=[key],
                         timeout=timeout or self.timeout, retries=self.retries, sleep=self.sleep)

    # search ---------------------------------------------------------------------------------
    @staticmethod
    def search_body(vendor: str, req: Mapping[str, Any]) -> dict:
        n = int(req.get("n") or 10)
        if vendor == "parallel":
            query, objective = domain_query(req["query"], req.get("objective") or req["query"], req.get("domain"))
            body: dict = {"objective": objective, "search_queries": [query]}
            if n != cost.PRICES["parallel"]["included_results"]:
                body["advanced_settings"] = {"max_results": n}
            body["mode"] = "fast"
            return body
        body = {"query": req["query"], "numResults": n}
        if req.get("domain"):
            body["includeDomains"] = [req["domain"]]
        body["type"] = "auto"  # never "contents": page text is billed outside this price
        return body

    def search(self, vendor: str, key: str, req: Mapping[str, Any]) -> Answer:
        body = self.search_body(vendor, req)
        raw, ms = self._post(vendor, URLS[vendor], key, body)
        results = norm_results(raw.get("results") if isinstance(raw, Mapping) else None)
        if vendor == "parallel" and req.get("domain"):
            results = [r for r in results if on_domain(r["url"], req["domain"])]
        n = int(req.get("n") or 10)
        charge = cost.charge_parallel(raw, cost.search_price("parallel", n)) if vendor == "parallel" else cost.charge_exa(raw, n)
        return Answer(results[:n], raw, charge, tool=URLS[vendor], meta={"latency_ms": ms})

    # scrape ---------------------------------------------------------------------------------
    @staticmethod
    def scrape_body(req: Mapping[str, Any]) -> dict:
        # onlyMainContent off: meetings lists often sit in a side column, which the main-content filter drops.
        # parsers []: PDFs at the flat one-credit rate. proxy basic: never stealth or enhanced, never "auto" (which may
        # escalate to stealth on a refusal).
        return {"url": req["url"], "formats": ["markdown", "links"], "parsers": [], "maxAge": 0, "onlyMainContent": False,
                "proxy": "basic"}

    def scrape(self, key: str, req: Mapping[str, Any], vendor: str = "firecrawl") -> Answer:
        body = self.scrape_body(req)
        if vendor == "treg":
            del body["proxy"]  # treg takes only the fields its catalog declares, and its proxy is the basic one
        raw, ms = self._post(vendor, URLS[vendor], key, body, timeout=max(self.timeout, 120.0))
        if isinstance(raw, Mapping) and raw.get("success") is False:
            raise Unavailable(f"Firecrawl: {str(raw.get('error') or 'not successful')[:300]}")
        page = norm_scrape(req["url"], raw if isinstance(raw, Mapping) else {})
        return Answer(page, raw, cost.charge_firecrawl((raw.get("data") or {}).get("metadata") if isinstance(raw, Mapping) else None),
                      tool=URLS[vendor], meta={"latency_ms": ms})

    # judge (Jev) and reason -----------------------------------------------------------------
    def judge(self, key: str, state: Any, questions: Mapping[str, Any], model: str, prices: Mapping[str, Any]) -> Answer:
        body = {"model": model, "state": state, "questions": questions}
        raw, ms = self._post("vercel", URLS["vercel"] + "/evaluate", key, body)
        est = cost.estimate_jev(body, model, prices)
        return Answer(raw, raw, cost.charge_gateway(raw, model, est, prices), tool="/v1/evaluate", model=model,
                      usage=raw.get("usage") if isinstance(raw, Mapping) else None, meta={"latency_ms": ms})

    def reason(self, key: str, body: Mapping[str, Any], prices: Mapping[str, Any]) -> Answer:
        raw, ms = self._post("vercel", URLS["vercel"] + "/chat/completions", key, body, timeout=max(self.timeout, 300.0))
        est = cost.estimate_chat(body["messages"], int(body["max_tokens"]), body["model"], prices)
        return Answer(raw, raw, cost.charge_gateway(raw, body["model"], est, prices), tool="/v1/chat/completions",
                      model=body["model"], usage=raw.get("usage") if isinstance(raw, Mapping) else None, meta={"latency_ms": ms})
