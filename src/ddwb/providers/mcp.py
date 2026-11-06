"""Vendor MCP servers as providers: we are the MCP client, over streamable HTTP, with the official `mcp` SDK.

Each call opens one connection, lists the server's tools, lets the vendor's adapter pick the tool and fill its
arguments from the tool's own input schema (an argument the tool does not declare is never sent), calls it, and
normalises the answer to our shape. Adapters:

    parallel    search.parallel.ai: web_search (objective, search_queries), web_fetch (urls, full_content)
    firecrawl   mcp.firecrawl.dev: firecrawl_scrape (markdown and links, live fetch, proxy "basic"), firecrawl_search
    exa         mcp.exa.ai: web_search_exa, web_search_advanced_exa (includeDomains), crawling_exa. Built from Exa's
                published tool names; NOT verified live (Exa's server needs a key)
    generic     any other server: a tool named like the work (search; scrape, fetch, crawl) whose schema takes a
                query or a URL
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from . import cost
from .api import Answer, norm_results, norm_scrape, on_domain
from .errors import BadRequest, ProviderError, Unavailable
from .safety import USER_AGENT

SCRAPE_OBJECTIVE = "Read this public body's web page: its text and every link on it."
LIMITED = re.compile(r"rate.?limit|too many requests|quota|credit|payment|unauthori[sz]ed|forbidden|api key|trial|"
                     r"upgrade|sign ?up|exceeded", re.I)


@dataclass
class ToolCall:
    name: str
    arguments: dict


@dataclass
class Outcome:
    """What the server answered: structured content if the tool has an output schema, else its text blocks."""

    tool: str
    arguments: dict
    structured: Any = None
    texts: list[str] = field(default_factory=list)
    is_error: bool = False

    def data(self) -> Any:
        """The answer as data: structured content, else the text parsed as JSON, else None."""
        if self.structured is not None:
            return self.structured
        text = "\n".join(self.texts).strip()
        if text[:1] in "[{":
            try:
                return json.loads(text)
            except ValueError:
                return None
        return None

    @property
    def text(self) -> str:
        return "\n".join(self.texts)


# ------------------------------------------------------------------------------------------- the connection
def _root(error: BaseException) -> BaseException:
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return error


class SdkConnector:
    """One MCP connection per call (list tools, choose, call), through the official SDK."""

    def __init__(self, timeout: float = 120.0) -> None:
        self.timeout = timeout

    def run(self, url: str, headers: Mapping[str, str], choose: Callable[[list[dict]], ToolCall]) -> Outcome:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._run(url, headers, choose))
        box: dict = {}  # called from inside an event loop: run the connection on a thread of its own

        def go():
            try:
                box["out"] = asyncio.run(self._run(url, headers, choose))
            except BaseException as error:  # noqa: BLE001  (re-raised below)
                box["error"] = error
        t = threading.Thread(target=go)
        t.start()
        t.join()
        if "error" in box:
            raise box["error"]
        return box["out"]

    async def _run(self, url: str, headers: Mapping[str, str], choose) -> Outcome:
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client
        from mcp.shared.exceptions import MCPError

        try:
            async with create_mcp_http_client(headers={"User-Agent": USER_AGENT, **headers},
                                              timeout=httpx2.Timeout(30.0, read=self.timeout)) as http:
                async with Client(streamable_http_client(url, http_client=http), mode="legacy",
                                  read_timeout_seconds=self.timeout, cache=None) as client:
                    tools, cursor = [], None
                    for _ in range(20):  # pages of the tool list
                        page = await client.list_tools(cursor=cursor)
                        tools += [{"name": t.name, "description": t.description or "", "inputSchema": t.input_schema or {}}
                                  for t in page.tools]
                        cursor = page.next_cursor
                        if not cursor:
                            break
                    call = choose(tools)
                    res = await client.call_tool(call.name, call.arguments)
                    texts = [b.text for b in res.content if getattr(b, "type", None) == "text"]
                    return Outcome(call.name, call.arguments, res.structured_content, texts, bool(res.is_error))
        except ProviderError:
            raise
        except BaseException as error:  # noqa: BLE001  (anyio wraps failures in exception groups)
            root = _root(error)
            if isinstance(root, ProviderError):
                raise root from None
            if isinstance(root, MCPError):
                if root.code == -32602:  # invalid params: our arguments were wrong
                    raise BadRequest(f"MCP server refused the arguments: {root.message[:300]}") from None
                raise Unavailable(f"MCP error {root.code}: {root.message[:300]}") from None
            status = getattr(getattr(root, "response", None), "status_code", None)
            if status is not None:
                raise Unavailable(f"MCP server answered HTTP {status}") from None
            if isinstance(root, (KeyboardInterrupt, SystemExit)):
                raise root from None
            raise Unavailable(f"MCP connection failed: {type(root).__name__}: {str(root)[:200]}") from None


# ------------------------------------------------------------------------------------------- argument mapping
def props(tool: Mapping[str, Any]) -> dict:
    return (tool.get("inputSchema") or {}).get("properties") or {}


def fit(tool: Mapping[str, Any], args: Mapping[str, Any]) -> dict:
    """Only the arguments the tool declares (all of them if its schema lists none); every required one present."""
    schema = tool.get("inputSchema") or {}
    p = schema.get("properties")
    out = {k: v for k, v in args.items() if v is not None and (not p or k in p)}
    missing = [r for r in schema.get("required") or [] if r not in out]
    if missing:
        raise Unavailable(f"the server's tool {tool['name']!r} requires {missing}, which this call cannot fill")
    return out


def find(tools: list[dict], exact: tuple[str, ...], contains: tuple[str, ...], needs: tuple[str, ...] = ()) -> dict | None:
    """A tool by exact name in order of preference, else the first whose name contains a word and whose schema has one
    of `needs`."""
    by_name = {t["name"]: t for t in tools}
    for name in exact:
        if name in by_name:
            return by_name[name]
    for t in tools:
        low = t["name"].lower()
        if any(w in low for w in contains) and (not needs or any(n in props(t) for n in needs)):
            return t
    return None


def first_prop(tool: Mapping[str, Any], names: tuple[str, ...]) -> str | None:
    p = props(tool)
    return next((n for n in names if n in p), None)


def _results(data: Any) -> Any:
    """The list of results in a search answer, wherever the server put it."""
    if isinstance(data, list):
        return data
    if not isinstance(data, Mapping):
        return None
    for key in ("results", "web", "items", "data"):
        v = data.get(key)
        if isinstance(v, list):
            return v
        if isinstance(v, Mapping):
            inner = _results(v)
            if inner is not None:
                return inner
    return None


_BLOCK = re.compile(r"^(Title|URL|Published(?: Date)?|Author|Text|Highlights|Summary|ID):\s*(.*)$", re.I)


def parse_text_results(text: str) -> list[dict]:
    """Results written as text blocks ("Title: ...\\nURL: ...\\nText: ..."), as some servers return them."""
    out, cur, field_ = [], {}, None
    for line in (text or "").splitlines():
        m = _BLOCK.match(line.strip())
        if m:
            key = m.group(1).lower().split()[0]
            if key == "title" and cur.get("url"):
                out.append(cur)
                cur = {}
            key = {"published": "publishedDate", "text": "text", "highlights": "text", "summary": "text"}.get(key, key)
            cur[key] = m.group(2).strip()
            field_ = key
        elif field_ == "text" and line.strip():
            cur["text"] = (cur.get("text", "") + "\n" + line.strip()).strip()
    if cur.get("url"):
        out.append(cur)
    return out


# ------------------------------------------------------------------------------------------- adapters
class Adapter:
    """The generic adapter: a tool named like the work, arguments mapped from our names to the schema's."""

    vendor: str | None = None

    def plan(self, kind: str, req: Mapping[str, Any], tools: list[dict], session: str) -> ToolCall:
        if kind == "search":
            tool = find(tools, (), ("search",), ("query", "q", "objective", "search_queries", "queries"))
            if tool is None:
                raise Unavailable(f"the server has no search tool (tools: {', '.join(t['name'] for t in tools)})")
            return ToolCall(tool["name"], fit(tool, self.search_args(tool, req)))
        if kind == "scrape":
            tool = find(tools, (), ("scrape", "fetch", "crawl", "read"), ("url", "urls"))
            if tool is None:
                raise Unavailable(f"the server has no scrape or fetch tool (tools: {', '.join(t['name'] for t in tools)})")
            url_key = first_prop(tool, ("url", "urls"))
            return ToolCall(tool["name"], fit(tool, {url_key: req["url"] if url_key == "url" else [req["url"]]}))
        raise Unavailable(f"{kind} over MCP is not supported")

    def search_args(self, tool: Mapping[str, Any], req: Mapping[str, Any]) -> dict:
        p = props(tool)
        domain_key = first_prop(tool, ("includeDomains", "include_domains", "domains", "site"))
        query, objective = req["query"], req.get("objective")
        if req.get("domain") and not domain_key:
            query = f"site:{req['domain']} {query}"
        args: dict = {}
        qkey = first_prop(tool, ("query", "q"))
        if qkey:
            args[qkey] = query
        if "objective" in p:
            args["objective"] = objective or query
        for key in ("search_queries", "queries"):
            if key in p:
                args[key] = [query]
        nkey = first_prop(tool, ("numResults", "num_results", "max_results", "limit", "count"))
        if nkey:
            args[nkey] = int(req.get("n") or 10)
        if req.get("domain") and domain_key:
            args[domain_key] = req["domain"] if domain_key == "site" else [req["domain"]]
        return args

    def normalise(self, kind: str, req: Mapping[str, Any], out: Outcome) -> Answer:
        data = out.data()
        if kind == "search":
            items = _results(data)
            results = norm_results(items) if items is not None else norm_results(parse_text_results(out.text))
            if req.get("domain"):
                results = [r for r in results if on_domain(r["url"], req["domain"])]
            return Answer(results[: int(req.get("n") or 10)], data if data is not None else out.text,
                          cost.Charge(0.0, "unpriced"), tool=out.tool)
        if isinstance(data, Mapping):
            page = norm_scrape(req["url"], data)
            if not page["markdown"]:
                page["markdown"] = str(data.get("content") or data.get("text") or "")
        else:
            page = norm_scrape(req["url"], {"markdown": out.text})
        return Answer(page, data if data is not None else out.text, cost.Charge(0.0, "unpriced"), tool=out.tool)

    def estimate(self, kind: str, req: Mapping[str, Any]) -> float:
        return 0.0

    def charge(self, kind: str, req: Mapping[str, Any], answer: Answer) -> cost.Charge:
        return answer.charge


class ParallelAdapter(Adapter):
    vendor = "parallel"

    def plan(self, kind, req, tools, session):
        if kind == "search":
            tool = find(tools, ("web_search", "web_search_preview"), ("search",), ("objective", "search_queries"))
            if tool is None:
                raise Unavailable(f"Parallel's server has no web_search tool (tools: {', '.join(t['name'] for t in tools)})")
            query, objective = req["query"], req.get("objective") or req["query"]
            if req.get("domain"):  # the tool takes site: with a bare domain in a query
                query, objective = f"site:{req['domain']} {query}", f"{objective} Only pages on {req['domain']}."
            return ToolCall(tool["name"], fit(tool, {"objective": objective, "search_queries": [query], "session_id": session}))
        if kind == "scrape":
            tool = find(tools, ("web_fetch",), ("fetch",), ("urls",))
            if tool is None:
                raise Unavailable("Parallel's server has no web_fetch tool")
            return ToolCall(tool["name"], fit(tool, {"urls": [req["url"]], "full_content": True,
                                                     "objective": req.get("objective"), "session_id": session}))
        return super().plan(kind, req, tools, session)

    def normalise(self, kind, req, out):
        data = out.data()
        if kind == "scrape":
            first = (_results(data) or [{}])[0] if data is not None else {}
            first = first if isinstance(first, Mapping) else {}
            md = first.get("full_content") or "\n\n".join(first.get("excerpts") or []) or ("" if data is not None else out.text)
            page = {"url": req["url"], "final_url": first.get("url") or req["url"], "title": first.get("title") or "",
                    "markdown": md, "links": [], "status": None, "content_type": None, "proxy": None,
                    "error": None, "credits": None}
            return Answer(page, data, cost.Charge(0.0, "keyless"), tool=out.tool, usage=(data or {}).get("usage") if isinstance(data, Mapping) else None)
        ans = super().normalise(kind, req, out)
        ans.usage = data.get("usage") if isinstance(data, Mapping) else None
        return ans

    def estimate(self, kind, req):
        return cost.PRICES["parallel"]["search"]

    def charge(self, kind, req, answer):
        return cost.charge_parallel(answer.raw, self.estimate(kind, req))


class FirecrawlAdapter(Adapter):
    vendor = "firecrawl"

    def plan(self, kind, req, tools, session):
        if kind == "scrape":
            tool = find(tools, ("firecrawl_scrape",), ("scrape",), ("url",))
            if tool is None:
                raise Unavailable(f"Firecrawl's server has no scrape tool (tools: {', '.join(t['name'] for t in tools)})")
            args = {"url": req["url"], "formats": ["markdown", "links"], "onlyMainContent": False, "maxAge": 0, "parsers": [],
                    "objective": req.get("objective") or SCRAPE_OBJECTIVE}
            if "proxy" in props(tool):
                args["proxy"] = "basic"  # never stealth, enhanced or auto (auto may escalate to stealth)
            return ToolCall(tool["name"], fit(tool, args))
        if kind == "search":
            tool = find(tools, ("firecrawl_search",), ("search",), ("query",))
            if tool is None:
                raise Unavailable("Firecrawl's server has no search tool")
            args = {"query": req["query"], "limit": int(req.get("n") or 10),
                    "includeDomains": [req["domain"]] if req.get("domain") else None}
            if req.get("domain") and "includeDomains" not in props(tool):
                args["query"] = f"site:{req['domain']} {req['query']}"
            return ToolCall(tool["name"], fit(tool, args))
        return super().plan(kind, req, tools, session)

    def normalise(self, kind, req, out):
        if kind == "scrape":
            data = out.data()
            if isinstance(data, Mapping):
                return Answer(norm_scrape(req["url"], data), data, cost.Charge(0.0, "keyless"), tool=out.tool)
            return Answer(norm_scrape(req["url"], {"markdown": out.text}), out.text, cost.Charge(0.0, "keyless"), tool=out.tool)
        return super().normalise(kind, req, out)

    def estimate(self, kind, req):
        return cost.PRICES["firecrawl"]["scrape"] if kind == "scrape" else 0.0

    def charge(self, kind, req, answer):
        if kind == "scrape":
            raw = answer.raw if isinstance(answer.raw, Mapping) else {}
            raw = raw.get("data", raw) if isinstance(raw.get("data"), Mapping) else raw
            return cost.charge_firecrawl(raw.get("metadata") if isinstance(raw, Mapping) else None)
        return answer.charge


class ExaAdapter(Adapter):
    """From Exa's published tool names (not verified live)."""

    vendor = "exa"

    def plan(self, kind, req, tools, session):
        if kind == "search":
            names = {t["name"]: t for t in tools}
            adv = names.get("web_search_advanced_exa")
            if req.get("domain") and adv is not None and "includeDomains" in props(adv):
                tool = adv
            else:
                tool = find(tools, ("web_search_exa",), ("search",), ("query",))
            if tool is None:
                raise Unavailable(f"Exa's server has no web_search_exa tool (tools: {', '.join(names)})")
            args = {"query": req["query"], "numResults": int(req.get("n") or 10), "type": "auto"}
            if req.get("domain"):
                if "includeDomains" in props(tool):
                    args["includeDomains"] = [req["domain"]]
                else:
                    args["query"] = f"site:{req['domain']} {req['query']}"
            return ToolCall(tool["name"], fit(tool, args))
        if kind == "scrape":
            tool = find(tools, ("crawling_exa",), ("crawl", "fetch"), ("url", "urls"))
            if tool is None:
                raise Unavailable("Exa's server has no crawling_exa tool")
            key = first_prop(tool, ("url", "urls")) or "url"
            return ToolCall(tool["name"], fit(tool, {key: req["url"] if key == "url" else [req["url"]]}))
        return super().plan(kind, req, tools, session)

    def normalise(self, kind, req, out):
        if kind == "scrape":
            data = out.data()
            first = (_results(data) or [{}])[0] if data is not None else {}
            first = first if isinstance(first, Mapping) else {}
            page = norm_scrape(req["url"], {"markdown": first.get("text") or out.text, "title": first.get("title"),
                                            "url": first.get("url") or req["url"]})
            return Answer(page, data if data is not None else out.text, cost.Charge(0.0, "keyless"), tool=out.tool)
        return super().normalise(kind, req, out)

    def estimate(self, kind, req):
        return cost.search_price("exa", int(req.get("n") or 10)) if kind == "search" else 0.0

    def charge(self, kind, req, answer):
        if kind == "search" and isinstance(answer.raw, Mapping):
            return cost.charge_exa(answer.raw, int(req.get("n") or 10))
        return cost.Charge(self.estimate(kind, req), "price_table")


ADAPTERS: dict[str | None, Adapter] = {"parallel": ParallelAdapter(), "firecrawl": FirecrawlAdapter(), "exa": ExaAdapter(),
                                       None: Adapter()}


def adapter_for(vendor: str | None) -> Adapter:
    return ADAPTERS.get(vendor, ADAPTERS[None])


def call(connector, url: str, headers: Mapping[str, str], vendor: str | None, kind: str, req: Mapping[str, Any],
         session: str) -> Answer:
    """One MCP call: discover, map, call, normalise. A tool error is an error (Unavailable), never an empty answer."""
    adapter = adapter_for(vendor)
    out = connector.run(url, headers, lambda tools: adapter.plan(kind, req, tools, session))
    if out.is_error:
        raise Unavailable(f"the server's tool {out.tool!r} failed: {out.text[:300]}"
                          + (" (rate limit, credits or key)" if LIMITED.search(out.text) else ""))
    answer = adapter.normalise(kind, req, out)
    answer.meta["arguments"] = out.arguments
    return answer
