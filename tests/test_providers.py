"""The provider layer, offline: settings, keys, per-run limits, cache, ledger, error semantics, the price guard, the MCP
adapters and the scrape safety rules. Every network edge is a fake; MCP tool schemas are the real ones (fixture)."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ddwb.deep import web
from ddwb.providers import (EXA, FIRECRAWL, JEV, PARALLEL, BadRequest, BotWall, BudgetExceeded, ConfigError, LimitReached,
                            MissingKey, NotAvailable, Off, PriceDrift, Providers, RunStopped, Unavailable, UseAgent)
from ddwb.providers import api as api_mod
from ddwb.providers import cost, settings
from ddwb.providers.mcp import Adapter, Outcome, adapter_for, parse_text_results

FIX = json.loads((Path(__file__).parent / "fixtures" / "mcp_servers.json").read_text())
PARALLEL_MCP = "mcp:https://search.parallel.ai/mcp"
FIRECRAWL_MCP = "mcp:https://mcp.firecrawl.dev/v2/mcp"
KEYS = {"PARALLEL_API_KEY": "par-secret-0123456789abcdef", "EXA_API_KEY": "exa-secret-0123456789abcdef",  # gitleaks:allow (made-up test keys)
        "FIRECRAWL_API_KEY": "fc-secret-0123456789abcdef", "AI_GATEWAY_API_KEY": "vck-secret-0123456789abcdef"}  # gitleaks:allow (made-up test keys)


# ------------------------------------------------------------------------------------------- fakes
class Guard:
    def __init__(self, blocked=None):
        self.b = dict(blocked or {})

    def blocked(self, host):
        return self.b.get(host)

    def block(self, host, why):
        self.b[host] = why


class FakeConnector:
    """An MCP server: its tools (real schemas) and an answer function (tool, arguments) -> Outcome fields."""

    def __init__(self, tools, answer):
        self.tools, self.answer, self.calls = tools, answer, []

    def run(self, url, headers, choose):
        call = choose(self.tools)
        self.calls.append((url, dict(headers), call.name, call.arguments))
        return Outcome(call.name, call.arguments, **self.answer(call.name, call.arguments))


class FakeHttp:
    """A vendor API: records each request; answers with `reply(url, body) -> (status, text)`."""

    def __init__(self, reply):
        self.reply, self.sent = reply, []

    def __call__(self, method, url, headers, body, timeout):
        b = json.loads(body)
        self.sent.append({"url": url, "headers": dict(headers), "body": b})
        status, payload = self.reply(url, b)
        return status, {}, payload if isinstance(payload, str) else json.dumps(payload)


def make(tmp_path, *, chains=None, http=None, connector=None, guard=None, env=None, run_id="r1", **kw):
    st = settings.defaults()
    if chains:
        st = settings.Settings({**st.chains, **{k: settings.parse_chain(k, v) for k, v in chains.items()}})
    return Providers(st, run_id=run_id, ledger_path=tmp_path / "ledger.jsonl", cache_path=tmp_path / "cache.sqlite",
                     api=api_mod.Api(http, sleep=lambda s: None), connector=connector, host_guard=guard or Guard(),
                     env=KEYS if env is None else env, **kw)


def ledger(tmp_path):
    return [json.loads(l) for l in (tmp_path / "ledger.jsonl").read_text().splitlines()]


def jev_reply(tokens=717, reported=None, p=0.9):
    def reply(url, body):
        meta = {"gateway": {"cost": reported if reported is not None else tokens * 0.042e-6, "generationId": "gen_1"}}
        return 200, {"answers": {q: {"type": "boolean", "probability": p} for q in body["questions"]},
                     "usage": {"inputTokens": tokens, "outputTokens": 0}, "providerMetadata": meta}
    return reply


Q = {"official_site": {"type": "boolean", "instructions": "Is this the body's own site?"}}


# ------------------------------------------------------------------------------------------- settings
def test_settings_precedence_env_then_local_file_then_user_file_then_defaults(tmp_path):
    local, user = tmp_path / "providers.toml", tmp_path / "user.toml"
    local.write_text('[scrape]\nuse = "api:firecrawl"\n[limits]\nmax_usd = 0.5\n')
    user.write_text('[scrape]\nuse = "off"\n[judge]\nuse = "api:vercel"\n[limits]\nmax_usd = 9\nmax_calls = 7\n')
    s = settings.load({"DDWB_SEARCH": "api:exa, agent"}, local=local, user=user)
    assert [x.text for x in s.chain("search")] == ["api:exa", "agent"] and s.sources["search"] == "env DDWB_SEARCH"
    assert [x.text for x in s.chain("scrape")] == ["api:firecrawl"] and s.sources["scrape"] == str(local)
    assert [x.text for x in s.chain("judge")] == ["api:vercel"] and s.sources["judge"] == str(user)
    assert [x.text for x in s.chain("browse")] == ["off"] and s.sources["browse"] == "default"
    assert s.max_usd == 0.5 and s.max_calls == 7
    assert settings.load({"DDWB_MAX_USD": "2.5"}, local=local, user=user).max_usd == 2.5


def test_defaults_with_nothing_set(tmp_path):
    s = settings.load({}, local=tmp_path / "none.toml", user=tmp_path / "none2.toml")
    assert [x.text for x in s.chain("search")] == [PARALLEL_MCP, "agent"]
    assert [x.text for x in s.chain("scrape")] == [FIRECRAWL_MCP, "off"]
    assert [x.text for x in s.chain("judge")] == ["off"] and [x.text for x in s.chain("browse")] == ["off"]
    assert s.max_calls == settings.DEFAULT_MAX_CALLS and s.max_usd == settings.DEFAULT_MAX_USD


def test_headers_from_env_json_and_from_files(tmp_path):
    local = tmp_path / "p.toml"
    local.write_text('[search]\nheaders = { Authorization = "Bearer ${PARALLEL_API_KEY}" }\n')
    s = settings.load({"DDWB_SCRAPE_HEADERS": '{"X-Trace": "abc"}'}, local=local, user=tmp_path / "x")
    assert s.headers["search"] == {"Authorization": "Bearer ${PARALLEL_API_KEY}"} and s.headers["scrape"] == {"X-Trace": "abc"}


def test_keys_are_references_never_values(tmp_path):
    bad = tmp_path / "p.toml"
    bad.write_text('[keys]\nexa = "exa-0123456789abcdef"\n')
    with pytest.raises(ConfigError, match="reference"):
        settings.load({}, local=bad, user=tmp_path / "x")
    bad.write_text('[search]\nheaders = { Authorization = "Bearer abc123" }\n')
    with pytest.raises(ConfigError, match="credential"):
        settings.load({}, local=bad, user=tmp_path / "x")
    ok = tmp_path / "ok.toml"
    ok.write_text('[keys]\nexa = "${MY_EXA}"\n')
    s = settings.load({}, local=ok, user=tmp_path / "x")
    assert s.key_ref("exa") == "${MY_EXA}" and s.key_ref("parallel") == "${PARALLEL_API_KEY}"
    assert "exa-secret" not in json.dumps(s.describe())
    assert settings.expand("Bearer ${EXA_API_KEY}", KEYS) == ("Bearer " + KEYS["EXA_API_KEY"], [KEYS["EXA_API_KEY"]])
    with pytest.raises(MissingKey):
        settings.expand("${NOPE}", {})


def test_specs_and_chains_are_checked():
    with pytest.raises(ConfigError, match="https"):
        settings.parse_spec("mcp:http://example.com/mcp")
    with pytest.raises(ConfigError):
        settings.parse_spec("api:google")
    with pytest.raises(ConfigError, match="cannot use"):
        settings.parse_chain("scrape", "agent")  # the agent's own tool is for search only
    with pytest.raises(ConfigError, match="ends a chain"):
        settings.parse_chain("search", "agent, api:exa")
    with pytest.raises(ConfigError, match="cannot use"):
        settings.parse_chain("judge", "mcp:https://x.example/mcp")
    assert settings.parse_spec("mcp:https://mcp.exa.ai/mcp?exaApiKey=${EXA_API_KEY}").vendor == "exa"
    assert settings.parse_spec("mcp:http://localhost:8000/mcp").vendor is None


# ------------------------------------------------------------------------------------------- direct APIs: request shapes
def test_direct_search_requests_are_the_benchmarks(tmp_path):
    http = FakeHttp(lambda url, b: (200, {"results": [{"url": "https://a.gov", "title": "A", "excerpts": ["x", "y"],
                                                         "publish_date": "2026-01-01"}],
                                         "usage": [{"name": "sku_search", "count": 1}], "costDollars": {"total": 0.007}}))
    p = make(tmp_path, http=http)
    r = p.search("Cortez city council", objective="The council's meetings page", provider=PARALLEL)
    assert http.sent[0]["url"] == "https://api.parallel.ai/v1/search"
    assert http.sent[0]["body"] == {"objective": "The council's meetings page", "search_queries": ["Cortez city council"], "mode": "fast"}
    assert http.sent[0]["headers"]["x-api-key"] == KEYS["PARALLEL_API_KEY"]
    assert http.sent[0]["headers"]["User-Agent"] == "public-signals-research/0.1"
    assert r.response[0] == {"url": "https://a.gov", "title": "A", "excerpt": "x\ny", "published_date": "2026-01-01",
                             "raw": {"url": "https://a.gov", "title": "A", "excerpts": ["x", "y"], "publish_date": "2026-01-01"}}
    assert r.cost == pytest.approx(0.001) and r.cost_source == "usage_x_price"
    p.search("board meeting agendas minutes", domain="cortezco.gov", n=10, provider=EXA)
    assert http.sent[1]["url"] == "https://api.exa.ai/search"
    assert http.sent[1]["body"] == {"query": "board meeting agendas minutes", "numResults": 10,
                                    "includeDomains": ["cortezco.gov"], "type": "auto"}
    assert "contents" not in http.sent[1]["body"]


def test_jev_and_reason_requests(tmp_path):
    http = FakeHttp(lambda url, b: (200, jev_reply()(url, b)[1]) if url.endswith("/evaluate") else
                    (200, {"choices": [{"message": {"content": '{"ok": true}'}}], "usage": {"prompt_tokens": 100, "completion_tokens": 10}}))
    p = make(tmp_path, http=http)
    r = p.judge({"body": {"name": "X"}}, Q, provider=JEV)
    assert http.sent[0]["url"] == "https://ai-gateway.vercel.sh/v1/evaluate"
    assert http.sent[0]["body"] == {"model": "typesafe-ai/jev-1.14.0", "state": {"body": {"name": "X"}}, "questions": Q}
    assert http.sent[0]["headers"]["Authorization"] == "Bearer " + KEYS["AI_GATEWAY_API_KEY"]
    assert r.answers["official_site"]["probability"] == 0.9 and r.cost == pytest.approx(717 * 0.042e-6)
    r = p.reason("Say ok in JSON", "anthropic/claude-haiku-5.5", temperature=0)
    assert http.sent[1]["body"] == {"model": "anthropic/claude-haiku-5.5", "messages": [{"role": "user", "content": "Say ok in JSON"}],
                                    "max_tokens": 8000, "temperature": 0}
    assert r.text == '{"ok": true}' and r.cost == pytest.approx(100 * 0.10e-6 + 10 * 0.50e-6)
    with pytest.raises(ConfigError, match="no price"):
        p.reason("x", "some/unpriced-model")


# ------------------------------------------------------------------------------------------- errors: never "no results"
def test_a_failure_is_an_error_and_an_empty_search_is_a_result(tmp_path):
    http = FakeHttp(lambda url, b: (503, "busy"))
    p = make(tmp_path, http=http)
    with pytest.raises(Unavailable, match="503"):
        p.search("x", provider=EXA)
    assert len(http.sent) == 4  # retried three times, then an error
    http.reply = lambda url, b: (200, {"results": [], "costDollars": {"total": 0.007}})
    r = p.search("nothing like this", provider=EXA)
    assert r.response == [] and r.status == "empty"
    http.reply = lambda url, b: (422, "bad field")
    with pytest.raises(BadRequest):
        p.search("y", provider=EXA)
    http.reply = lambda url, b: (402, "payment required")
    with pytest.raises(Unavailable, match="no credit"):
        p.search("z", provider=EXA)


def test_a_chain_moves_on_only_when_a_provider_gives_no_answer(tmp_path):
    down = FakeConnector(FIX["parallel"], lambda name, args: {"texts": ["rate limit exceeded"], "is_error": True})
    p = make(tmp_path, connector=down)
    with pytest.raises(UseAgent) as e:  # the default chain: Parallel's keyless MCP, then the agent's own search
        p.search("Cortez council minutes")
    assert "use your own web search tool" in str(e.value) and "rate limit" in str(e.value)
    with pytest.raises(Unavailable):  # pinned: no fallback
        p.search("Cortez council minutes", provider=PARALLEL_MCP)
    empty = FakeConnector(FIX["parallel"], lambda name, args: {"structured": {"search_id": "s", "results": []}})
    r = make(tmp_path, connector=empty, run_id="r2").search("nothing at all here")
    assert r.status == "empty" and r.provider == PARALLEL_MCP  # an empty answer never falls through to the agent


def test_off_and_browse(tmp_path):
    p = make(tmp_path)
    with pytest.raises(Off, match="Jev"):
        p.judge({"x": 1}, Q)  # judge is off by default
    with pytest.raises(NotAvailable, match="not available yet"):
        p.browse("https://boarddocs.example/x")
    with pytest.raises(Off, match="plain fetch"):
        make(tmp_path, chains={"scrape": "off"}).scrape("https://example.com")


def test_missing_key_falls_back_and_never_sends(tmp_path):
    http = FakeHttp(lambda url, b: (200, {"results": []}))
    p = make(tmp_path, http=http, env={}, chains={"search": "api:exa, agent"})
    with pytest.raises(UseAgent, match="EXA_API_KEY"):
        p.search("x")
    assert http.sent == []


# ------------------------------------------------------------------------------------------- limits, cache, ledger
def test_limits_are_per_run(tmp_path):
    http = FakeHttp(lambda url, b: (200, {"results": [{"url": "https://a.gov"}], "costDollars": {"total": 0.007}}))
    p = make(tmp_path, http=http, max_calls=2)
    p.search("one", provider=EXA)
    p.search("two", provider=EXA)
    with pytest.raises(LimitReached):
        p.search("three", provider=EXA)
    p.search("one", provider=EXA)  # a cache hit is free and not a call
    other = make(tmp_path, http=http, max_calls=2, run_id="r2")  # a new run starts from zero
    other.search("four", provider=EXA)
    dollars = make(tmp_path, http=http, max_usd=0.01, run_id="r3")
    dollars.search("five", provider=EXA)
    with pytest.raises(BudgetExceeded):
        dollars.search("six", provider=EXA)  # $0.007 spent + $0.007 estimate > $0.01
    assert [e["outcome"] for e in ledger(tmp_path) if e["run_id"] == "r3"] == ["ok", "refused"]
    free = make(tmp_path, http=http, max_calls=None, max_usd=None, run_id="r4")  # the benchmark's runs: no call limit
    for i in range(3):
        free.search(f"q{i}", provider=EXA)
    assert free.totals().calls == {EXA: 3}
    assert settings.load({"DDWB_MAX_CALLS": "none"}, local=tmp_path / "x", user=tmp_path / "y").max_calls is None


def test_keyless_calls_pass_a_zero_dollar_limit(tmp_path):
    srv = FakeConnector(FIX["parallel"], lambda name, args: {"structured": FIX["parallel_search_answer"]})
    r = make(tmp_path, connector=srv, max_usd=0.0).search("Cortez Colorado city council minutes")
    assert r.cost == 0.0 and r.cost_source == "keyless" and len(r.response) == 3


def test_cache_and_ledger(tmp_path):
    http = FakeHttp(lambda url, b: (200, {"results": [{"url": "https://a.gov", "title": "A"}], "costDollars": {"total": 0.007}}))
    p = make(tmp_path, http=http, approved_by="the run's owner")
    first = p.search("q", provider=EXA)
    again = p.search("q", provider=EXA)
    assert len(http.sent) == 1 and not first.cached and again.cached and again.cost == 0.0
    assert again.response == first.response
    p.search("q", provider=EXA, n=5)  # another request: not the same cache entry
    assert len(http.sent) == 2
    lines = ledger(tmp_path)
    assert [l["outcome"] for l in lines] == ["ok", "cached", "ok"] and lines[1]["original_cost"] == 0.007
    assert all(l["approved_by"] == "the run's owner" and l["provider"] == EXA for l in lines)
    text = (tmp_path / "ledger.jsonl").read_text() + (tmp_path / "cache.sqlite").read_bytes().decode("latin-1")
    assert not any(k in text for k in KEYS.values())
    assert p.totals().usd == pytest.approx(0.014) and p.totals().calls == {EXA: 2}


def test_errors_never_carry_a_key(tmp_path):
    http = FakeHttp(lambda url, b: (401, f"invalid key {KEYS['EXA_API_KEY']}"))
    with pytest.raises(Unavailable) as e:
        make(tmp_path, http=http).search("x", provider=EXA)
    assert KEYS["EXA_API_KEY"] not in str(e.value) and "redacted" in str(e.value)


# ------------------------------------------------------------------------------------------- the price guard
def test_price_guard_compares_the_reported_charge_with_usage_times_price(tmp_path):
    # 2026-11-05: a table-heavy state cost more than a character-based estimate, but exactly usage x price: no drift.
    big_state = {"passage": "|a|b|c|\n" * 2000}
    http = FakeHttp(jev_reply(tokens=30_000))
    p = make(tmp_path, http=http)
    r = p.judge(big_state, Q, provider=JEV)
    assert r.reported_cost == pytest.approx(30_000 * 0.042e-6) and r.expected_cost == pytest.approx(r.reported_cost)
    # the price changed: the gateway reports twice usage x the table
    http.reply = jev_reply(tokens=717, reported=2 * 717 * 0.042e-6)
    with pytest.raises(PriceDrift) as e:
        p.judge({"other": 1}, Q, provider=JEV)
    assert e.value.result is not None and e.value.result.answers["official_site"]["probability"] == 0.9  # the paid answer
    with pytest.raises(RunStopped):  # the run's paid calls stop
        p.judge({"third": 1}, Q, provider=JEV)
    assert p.judge(big_state, Q, provider=JEV).cached  # a cached answer costs nothing and is still served
    http.reply = jev_reply()  # the table fixed: a new run is not stopped
    assert not make(tmp_path, http=http, run_id="next").judge({"x": 2}, Q, provider=JEV).cached


def test_price_guard_on_exa_and_firecrawl(tmp_path):
    http = FakeHttp(lambda url, b: (200, {"results": [], "costDollars": {"total": 0.05}}))
    with pytest.raises(PriceDrift, match="price changed"):
        make(tmp_path, http=http).search("x", provider=EXA)
    page = {"success": True, "data": {"markdown": "# Hi", "links": [], "metadata": {"statusCode": 200, "creditsUsed": 5}}}
    http = FakeHttp(lambda url, b: (200, page))
    with pytest.raises(PriceDrift):  # five credits for one page: billed outside the table
        make(tmp_path, http=http, run_id="r9").scrape("https://example.com", provider=FIRECRAWL)
    assert cost.charge_firecrawl({"creditsUsed": 1}).drift is None and cost.charge_exa({"costDollars": {"total": 0.007}}, 10).drift is None


# ------------------------------------------------------------------------------------------- MCP adapters
def test_parallel_mcp_maps_to_web_search_and_normalises(tmp_path):
    srv = FakeConnector(FIX["parallel"], lambda name, args: {"structured": FIX["parallel_search_answer"]})
    p = make(tmp_path, connector=srv)
    r = p.search("Cortez Colorado city council minutes")
    url, headers, tool, args = srv.calls[0]
    assert url == "https://search.parallel.ai/mcp" and tool == "web_search" and headers == {}
    assert args == {"objective": "Cortez Colorado city council minutes", "search_queries": ["Cortez Colorado city council minutes"],
                    "session_id": p.session}
    assert r.tool == "web_search" and r.response[0]["url"].startswith("https://") and r.response[0]["excerpt"]
    r = p.search("council minutes", domain="cortezco.gov", objective="Minutes", n=2)
    args = srv.calls[1][3]
    assert args["search_queries"] == ["site:cortezco.gov council minutes"] and "cortezco.gov" in args["objective"]
    assert all(web.host_of(x["url"]).endswith("cortezco.gov") for x in r.response)  # results outside the domain dropped


def test_firecrawl_mcp_scrape_arguments_never_ask_for_stealth(tmp_path):
    answer = {"structured": {"markdown": "# Example Domain\n[More](https://iana.org/domains/example)",
                             "links": ["https://iana.org/domains/example"],
                             "metadata": {"title": "Example Domain", "statusCode": 200, "url": "https://example.com/",
                                          "proxyUsed": "basic"}}}
    srv = FakeConnector(FIX["firecrawl"], lambda name, args: answer)
    r = make(tmp_path, connector=srv).scrape("https://example.com")
    _, _, tool, args = srv.calls[0]
    assert tool == "firecrawl_scrape" and args["proxy"] == "basic" and args["maxAge"] == 0 and args["onlyMainContent"] is False
    assert args["formats"] == ["markdown", "links"] and args["objective"]  # objective is required by the tool's schema
    assert r.response["title"] == "Example Domain" and r.response["links"] == ["https://iana.org/domains/example"]
    assert r.cost == 0.0 and r.cost_source == "keyless"


def test_keyed_mcp_is_priced_and_the_key_stays_a_reference(tmp_path):
    srv = FakeConnector(FIX["parallel"], lambda name, args: {"structured": {**FIX["parallel_search_answer"],
                                                                            "usage": [{"name": "sku_search", "count": 1}]}})
    st = settings.Settings({**settings.defaults().chains, "search": settings.parse_chain("search", PARALLEL_MCP)},
                           headers={"search": {"Authorization": "Bearer ${PARALLEL_API_KEY}"}})
    p = Providers(st, run_id="k1", ledger_path=tmp_path / "l.jsonl", cache_path=tmp_path / "c.sqlite", connector=srv,
                  host_guard=Guard(), env=KEYS)
    r = p.search("x")
    assert srv.calls[0][1] == {"Authorization": "Bearer " + KEYS["PARALLEL_API_KEY"]}
    assert r.cost == pytest.approx(0.001) and r.est_cost == pytest.approx(0.001)
    assert KEYS["PARALLEL_API_KEY"] not in (tmp_path / "l.jsonl").read_text()


def test_arguments_follow_the_tools_own_schema():
    tool = {"name": "search_things", "inputSchema": {"properties": {"q": {}, "limit": {}, "include_domains": {}}, "required": ["q"]}}
    call = Adapter().plan("search", {"query": "board minutes", "n": 3, "domain": "x.gov"}, [tool], "s")
    assert call.arguments == {"q": "board minutes", "limit": 3, "include_domains": ["x.gov"]}
    strict = {"name": "web_search", "inputSchema": {"properties": {"query": {}}, "required": ["query", "token"]}}
    with pytest.raises(Unavailable, match="requires"):
        Adapter().plan("search", {"query": "x"}, [strict], "s")
    with pytest.raises(Unavailable, match="no search tool"):
        Adapter().plan("search", {"query": "x"}, [{"name": "firecrawl_parse", "inputSchema": {}}], "s")


def test_exa_adapter_from_published_tool_names():
    tools = [{"name": "web_search_exa", "inputSchema": {"properties": {"query": {}, "numResults": {}}, "required": ["query"]}},
             {"name": "web_search_advanced_exa", "inputSchema": {"properties": {"query": {}, "numResults": {}, "includeDomains": {}}}}]
    a = adapter_for("exa")
    assert a.plan("search", {"query": "x", "n": 5}, tools, "s").arguments == {"query": "x", "numResults": 5}
    call = a.plan("search", {"query": "x", "n": 5, "domain": "c.gov"}, tools, "s")
    assert call.name == "web_search_advanced_exa" and call.arguments["includeDomains"] == ["c.gov"]
    text = "Title: Council minutes\nURL: https://c.gov/minutes\nPublished Date: 2026-01-02\nText: The council met\nand voted.\n\n" \
           "Title: Agendas\nURL: https://c.gov/agendas\nText: List"
    rows = parse_text_results(text)
    assert [r["url"] for r in rows] == ["https://c.gov/minutes", "https://c.gov/agendas"] and "voted" in rows[0]["text"]
    ans = a.normalise("search", {"query": "x"}, Outcome("web_search_exa", {}, texts=[text]))
    assert ans.response[0]["title"] == "Council minutes" and ans.response[0]["published_date"] == "2026-01-02"


# ------------------------------------------------------------------------------------------- scrape safety
def test_scrape_refuses_a_host_stopped_for_a_bot_wall(tmp_path):
    srv = FakeConnector(FIX["firecrawl"], lambda name, args: {"structured": {"markdown": "ok"}})
    p = make(tmp_path, connector=srv, guard=Guard({"go.boarddocs.com": "HTTP 403"}))
    with pytest.raises(BotWall, match="stopped"):
        p.scrape("https://www.go.boarddocs.com/co/x/Board.nsf")
    assert srv.calls == []  # never sent


def test_scrape_drops_a_bot_wall_page_and_stops_the_host(tmp_path):
    wall = {"structured": {"markdown": "Just a moment... Verify you are human", "metadata": {"statusCode": 200}}}
    guard = Guard()
    srv = FakeConnector(FIX["firecrawl"], lambda name, args: wall)
    p = make(tmp_path, connector=srv, guard=guard)
    with pytest.raises(BotWall) as e:
        p.scrape("https://portal.example.org/meetings")
    assert guard.blocked("portal.example.org") and e.value.result is not None
    with pytest.raises(BotWall):  # and it is not in the cache: the next try is refused before any call
        p.scrape("https://portal.example.org/meetings")
    assert len(srv.calls) == 1
    forbidden = FakeHttp(lambda url, b: (200, {"success": True, "data": {"markdown": "", "metadata": {"statusCode": 403}}}))
    with pytest.raises(BotWall):
        make(tmp_path, http=forbidden, run_id="r5").scrape("https://other.example.org/", provider=FIRECRAWL)


def test_direct_firecrawl_never_asks_for_stealth(tmp_path):
    http = FakeHttp(lambda url, b: (200, {"success": True, "data": {"markdown": "# A", "metadata": {"statusCode": 200, "creditsUsed": 1}}}))
    make(tmp_path, http=http).scrape("https://example.com", provider=FIRECRAWL)
    body = http.sent[0]["body"]
    assert http.sent[0]["url"] == "https://api.firecrawl.dev/v2/scrape" and body["proxy"] == "basic"
    assert body == {"url": "https://example.com", "formats": ["markdown", "links"], "parsers": [], "maxAge": 0,
                    "onlyMainContent": False, "proxy": "basic"}


def test_firecrawl_through_treg_sends_the_same_call_with_the_treg_token(tmp_path):
    http = FakeHttp(lambda url, b: (200, {"success": True, "data": {"markdown": "# A", "metadata": {"statusCode": 200, "creditsUsed": 1}}}))
    res = make(tmp_path, http=http, env={**KEYS, "TREG_TOKEN": "tok-treg"}).scrape("https://example.com", provider="api:treg")
    sent = http.sent[0]
    assert sent["url"] == "https://treg.to/call/firecrawl.web.scrape" and sent["headers"]["X-Treg-Token"] == "tok-treg"
    assert "Authorization" not in sent["headers"] and "proxy" not in sent["body"]  # treg refuses undeclared fields
    assert sent["body"]["parsers"] == [] and res.cost == pytest.approx(0.005)


def test_web_scrape_reports_walls_and_switched_off_scrape_as_no_page():
    def wall(url):
        raise BotWall("bot-check page", result=SimpleNamespace(cost=0.005, cached=False, response={"status": 200}))
    page, facts = web.scrape(SimpleNamespace(scrape=wall), "https://x.example/")
    assert page is None and facts["bot_wall"] is True and facts["cost_usd"] == 0.005

    def off(url):
        raise Off("scrape is off: plain fetch only")
    page, facts = web.scrape(SimpleNamespace(scrape=off), "https://x.example/")
    assert page is None and "plain fetch" in facts["error"]
    paid = SimpleNamespace(cost=0.01, cached=False, vendor="firecrawl", provider=FIRECRAWL,
                           response={"markdown": "| [Agenda](/a.pdf) | 2026-01-05 |", "final_url": "https://x.example/m", "title": "M"})

    def drift(url):
        raise PriceDrift("price changed", result=paid)
    page, facts = web.scrape(SimpleNamespace(scrape=drift), "https://x.example/m")
    assert page.links[0].href == "https://x.example/a.pdf" and facts["stopped"] and facts["cost_usd"] == 0.01


# ------------------------------------------------------------------------------------------- the nav tools: caps per run
def test_nav_caps_are_per_run_and_fixed_by_the_first_start(tmp_path, monkeypatch):
    import argparse

    from ddwb.deep import state as nav_state
    from ddwb.deep import tools
    monkeypatch.setattr(nav_state, "NAV", tmp_path)
    for k in ("DDWB_CAP_MAP", "DDWB_CAP_READING", "DDWB_APPROVED_BY"):
        monkeypatch.delenv(k, raising=False)
    old = tools.Cx("old:test:cortez-co")
    old.st.put(old.sid, "S1", "search", "done", cost=5.0, budget="map")  # an earlier run spent a lot
    cx = tools.Cx("new:test:cortez-co")
    fixed = tools.run_settings(cx, argparse.Namespace(cap_map=1.0, cap_reading=1.65, approved_by="run owner"))
    assert fixed == {"caps": {"map": 1.0, "reading": 1.65}, "approved_by": "run owner"}
    assert cx.check("map") == pytest.approx(1.0)  # the old run's spend does not count against this run
    other_arm = tools.Cx("new:test-jev:cortez-co")
    assert other_arm.caps() == {"map": 1.0, "reading": 1.65}
    with pytest.raises(tools.Stop, match="already"):
        tools.run_settings(other_arm, argparse.Namespace(cap_map=3.0, cap_reading=None, approved_by=None))
    assert tools.Cx("fresh:test:cortez-co").caps() == tools.DEFAULT_CAPS
    assert other_arm.providers.max_usd == pytest.approx(2.65) and other_arm.providers.approved_by == "run owner"
    assert other_arm.providers.max_calls is None  # dollars only, never how much is read


# ------------------------------------------------------------------------------------------- the router's cache, imported
def router_cache(path, rows):
    """A tiny cache in the router's own format (router_cache, keyed on the vendor request it sent)."""
    import sqlite3
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE router_cache (key TEXT PRIMARY KEY, route TEXT NOT NULL, kind TEXT, request TEXT NOT NULL, "
               "response TEXT NOT NULL, status INTEGER, latency_ms REAL, cost REAL, cost_source TEXT, vendor_ref TEXT, "
               "created_at TEXT NOT NULL, run_id TEXT)")
    for i, (route, kind, request, response, status, price) in enumerate(rows):
        db.execute("INSERT INTO router_cache VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (f"k{i}", route, kind, json.dumps(request), json.dumps(response), status, 100.0, price, "usage_x_price_table",
                    None, "2026-11-04T10:00:00Z", "20261105-nav3"))
    db.commit()
    db.close()


