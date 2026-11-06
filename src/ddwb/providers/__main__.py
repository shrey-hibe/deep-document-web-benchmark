"""Look at the provider settings, or try a provider by hand.

    uv run python -m ddwb.providers show                  settings in force and where each came from (no keys)
    uv run python -m ddwb.providers tools <mcp url>       the tools an MCP server offers (free: lists only)
    uv run python -m ddwb.providers search "<query>" [--provider SPEC] [--domain D] [--n N]
    uv run python -m ddwb.providers scrape <url> [--provider SPEC]

search and scrape use the configured providers (or the one given) and are recorded in the ledger like any call.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import Providers, ProviderError, load_settings
from .mcp import SdkConnector


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ddwb.providers")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show")
    t = sub.add_parser("tools")
    t.add_argument("url")
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--provider")
    s.add_argument("--domain")
    s.add_argument("--objective")
    s.add_argument("--n", type=int, default=10)
    c = sub.add_parser("scrape")
    c.add_argument("url")
    c.add_argument("--provider")
    for p in (s, c):
        p.add_argument("--run-id")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "show":
            print(json.dumps(load_settings().describe(), indent=2))
            return 0
        if a.cmd == "tools":
            seen: list[dict] = []

            def choose(tools):  # list, then stop before calling anything
                seen.extend(tools)
                raise _Listed()
            try:
                SdkConnector().run(a.url, {}, choose)
            except _Listed:
                pass
            for tool in seen:
                print(f"{tool['name']}: {sorted((tool.get('inputSchema') or {}).get('properties') or {})}")
            return 0
        p = Providers(run_id=a.run_id)
        res = p.search(a.query, objective=a.objective, domain=a.domain, n=a.n, provider=a.provider) if a.cmd == "search" \
            else p.scrape(a.url, provider=a.provider)
        out = {"provider": res.provider, "tool": res.tool, "cost": res.cost, "cached": res.cached, "status": res.status}
        if a.cmd == "search":
            out["results"] = [{k: r[k] for k in ("url", "title")} for r in res.response]
        else:
            out |= {k: res.response.get(k) for k in ("final_url", "title", "status")} | {
                "markdown_chars": len(res.response.get("markdown") or ""), "links": len(res.response.get("links") or [])}
        print(json.dumps(out, indent=2))
        return 0
    except ProviderError as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1


class _Listed(ProviderError):
    """Stops the connection once the tools are listed: nothing is called."""

    def __init__(self) -> None:
        super().__init__("listed")


if __name__ == "__main__":
    sys.exit(main())
