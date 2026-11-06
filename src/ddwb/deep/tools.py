"""Navigation tools: the deep chain's plumbing as commands an agent calls one at a time. Every call is recorded in the
state store (ddwb.deep.state). The workflow and when to use each tool: skills/deep-navigator/SKILL.md.

    nav <session> <tool> [args]        session = <run>:<arm>:<body>, e.g. 20261103-nav1:haiku-jev:cortez-co

What the tools enforce, so the brief does not have to:
- In the Jev arms (haiku-jev, opus-jev, ...) Jev runs inside the tools: on every link of a page fetched, every page checked
  as a meetings list, every link on an accepted list, and every item of a document read. Items Jev puts at p ≤ 0.2 are
  set aside (counted, listable, and recordable if the agent disagrees). The other arms get the same tools without Jev's
  numbers, with plain rules in their place.
- A document can be opened wherever it was seen (on a mapped list, on a page, in search results); one found off the
  mapped lists is counted as such.
- A failed or missing Jev answer is unknown, never "no".
- A skip needs a reason; an event needs the passage it came from; the answer's coverage is computed here, not claimed.
- A skip of several decides only what the agent can have read: a passage that may record buying and was never shown, or
  is one of more than a page (20) skipped at once, or the unopened minutes of a governing meeting, is kept as
  skipped_unreviewed and the answer stays incomplete.
- A governing meeting is covered by its minutes where the map lists them; by any document opened where it does not.
- An undated document takes a date from its title, file name or first page; `record --date` only where none is found.
- Paid calls stop at the run's caps: map (searches, scrapes, Jev on lists and links) and reading (Jev on passages, the
  jev tool), counted per run across its arms. The first `start` of a run fixes them (--cap-map, --cap-reading, else
  DDWB_CAP_MAP / DDWB_CAP_READING, else DEFAULT_CAPS) and records who approved the run if given (--approved-by or
  DDWB_APPROVED_BY). A cap that is hit makes the answer incomplete, never silently short.
- Paid and metered calls go through the provider layer (ddwb.providers): Jev pinned to the Vercel AI Gateway in the Jev
  arms, search pinned to Parallel or Exa, scrape on the configured provider. A host stopped for a bot wall is never
  scraped, and a page the provider flags as one is dropped.
- Our own GETs: User-Agent public-signals-research/0.1, paced per host across every process, stopped at a bot wall.
- A list on a known meeting portal (ddwb.portals: 16 adapters), or a page linking or embedding one, is mapped through the
  portal's adapter; a document of a portal that walls plain HTTP is fetched through the adapter's own transport (a
  browser with a person's settings, decision #123), which stops at any challenge and never solves one.
- Documents dated from 2025-01-01. Limits are by date and dollars only, never by how much is read.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from urllib.parse import unquote, urlsplit


from .. import paths, registry
from .. import portals as portals_prior
from ..portals import registry as portal_registry
from ..portals.base import Meeting as PortalMeeting
from ..portals.base import PortalError
from ..portals.browser import Challenge, Fallback, never_answered
from ..providers import EXA, JEV, PARALLEL, PriceDrift, ProviderError, Providers, load_settings
from ..runlog import Log
from . import diligent, profile, store, text, web
from .chain import BODIES, classify_link, is_file, is_governing, platform_of
from .forks import ACTIONS, BUYING_EVENT, EVENT_TYPES, LIST_PAGE, PEOPLE, jev_questions
from .state import HostClock, State, nid, now

SINCE = date(2025, 1, 1)
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{1,62}")  # the session's body label: used in file names
ARMS = {"haiku": False, "haiku-jev": True, "haiku-jev-relay": True, "sonnet": False, "sonnet-jev": True, "opus": False,
        "opus-jev": True, "test": False, "test-jev": True, "mapper": False}


def judge_connected() -> bool:
    """Whether the settings in force name a judge (Jev) other than off."""
    try:
        return any(spec.family != "off" for spec in load_settings().chain("judge"))
    except Exception:  # unreadable settings say so when a provider is called; here they only mean "not connected"
        return False
SET_ASIDE = 0.2  # Jev's p at or below which a link is low (ranking, skips); an item's cut is the body's set_aside_p (profile)
TYPE_SHOWN = 0.8  # Jev's type is shown to the agent only this sure, and its action never (Jev audit, 2026-11-05)
GENERIC_TITLE = re.compile(r"^\W*(?:download|view|open|click|pdf|packet|agenda|minutes|document|file|here|link|attachment|"
                           r"full|the|\W)+\W*$", re.I)  # link text that says nothing of the meeting: "DOWNLOAD PDF PACKET"
GAP_DAYS = 150  # the gap guard's default; the body's own is profile gap_days (from its meeting rhythm)
DEFAULT_CAPS = {"map": 1.00, "reading": 1.00}  # USD per run, when the run's first `start` sets none
CAP_ENV = {"map": "DDWB_CAP_MAP", "reading": "DDWB_CAP_READING"}
PER_PAGE = 60
PASSAGES_PER_PAGE = 20  # also the most passages one skip may decide at once: a page the agent was shown
MAX_VIEWS = 200  # a runaway guard on one list's other views (years, pages); hitting it is reported, never silent
QUERY = ("What has {name} bought or committed to buy since {since}: contracts, renewals, purchases, bids, grants and "
         "budget decisions, each with vendor, amount and date?")
# Kind definitions calibrated 2026-11-05 (work/deep-discovery/jev-calibration): agenda pages that share a row with the
# minutes, and items that approve earlier minutes, were called minutes (Cortez: 28 links); now 0, with every minutes found.
LINK = {
    "relevant": ("boolean", "Could this agenda item or document record or propose a purchase, contract, renewal, bid, grant or "
                            "budget decision by the body?"),
    "kind": ("choice", "What will this link itself open? Judge by the link's own text and its URL, not only by the row it "
                       "sits in.", {
        "minutes": "the minutes of a meeting: the record of what was decided. Not an agenda item that approves earlier "
                   "minutes, and not an agenda or agenda page, even when its row or heading also mentions minutes",
        "agenda": "a meeting's agenda, or a web page that shows the agenda",
        "packet": "an agenda packet or meeting materials (the agenda with its attachments)",
        "item": "one agenda item: its staff report, resolution, contract or attachment, including an item that approves minutes",
        "meeting_page": "a page for one meeting that lists its documents", "video": "a recording or live stream",
        "other": "something else, such as navigation or a print view"}),
    "governing": ("boolean", "Is it for a meeting of the body's governing board, not of a committee, commission or another board?"),
}
ROUTE = {"route": ("boolean", "Is this link the way to the governing board's meetings: the board's own page (its council, "
                              "commission, board of trustees or board of education), a meetings or agendas page, or a portal "
                              "listing its agendas, minutes or packets, either directly or within one or two clicks?")}
ROUTE_TOP = 15  # a fetched page always shows its 15 best-ranked links, whatever Jev's p: the ranking is what Jev is surest of
UNDECIDED = ("listed", "scored", "peeked", "failed")  # links still waiting for the agent: open, read, skip or unreachable
PORTAL_AHEAD = 60  # days past today a portal's meetings are listed for: agendas are posted ahead
PORTALS_PER_LIST = 3  # portals mapped from one list page that links or embeds several


class Stop(Exception):
    """A tool refuses: what is missing, or a cap that is reached. Printed for the agent, never swallowed."""


def say(*lines) -> None:
    print("\n".join(str(x) for x in lines), flush=True)


def compact(answers: dict | None) -> dict | None:
    """Jev's answers as {question: p} for yes/no and {question: choice, question_p: its probability} for choices."""
    if not answers:
        return None
    out = {}
    for k, a in answers.items():
        if a.get("type") == "boolean":
            out[k] = a.get("probability")
        else:
            out[k] = a.get("choice")
            p = (a.get("probabilities") or {}).get(a.get("choice"))
            if isinstance(p, (int, float)):
                out[f"{k}_p"] = p
    return out


def short(s: str | None, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def p_of(node: dict, field: str = "relevant"):
    j = node.get("jev") or {}
    return j.get(field) if isinstance(j, dict) else None


def fmt_p(p) -> str:
    return "p=?   " if p is None else f"p={p:.2f}"


WALLED = (" (refused to plain HTTP and, where the browser tier is set up, to the browser too: stopped there and never worked "
          "around; mark it `unreachable` with this reason, or find the same documents elsewhere: the body's portal, "
          "another of its sites)")


# ------------------------------------------------------------------------------------------- web, shared across processes
class Http(web.Http):
    """Our polite GET, with the host clock and bot walls shared by every tool process. `fresh_since`: a cached copy
    older than this is fetched again (lists are read fresh once per run; documents come from the cache)."""

    def __init__(self) -> None:
        super().__init__()
        self.clock = HostClock()

    def get(self, url: str, use_cache: bool = True, fresh_since: str | None = None) -> web.Response:
        key = self._key(url)
        cached = use_cache and key.with_suffix(".json").exists()
        if cached:
            meta = json.loads(key.with_suffix(".json").read_text())
            size = key.with_suffix(".bin").stat().st_size if key.with_suffix(".bin").exists() else 0
            if (fresh_since and meta.get("fetched_at", "")[:19] < fresh_since[:19]) or web.OLD_CAP <= size < web.OLD_CAP + 100_000:
                cached = use_cache = False
        host = web.host_of(url)
        if not cached:
            why = self.clock.blocked(host) or self.clock.browser_only(host)
            if why:
                return self.through_browser(url, web.Response(url, error=f"plain HTTP refused here earlier: {why}", bot_wall=True))
            self.clock.wait(host, web.HOST_GAP.get(web.site_of(url), web.MIN_GAP))
            self.last.pop(host, None)  # paced by the shared clock already
        resp = super().get(url, use_cache)
        if resp.bot_wall and not resp.from_cache:
            self.clock.block(host, f"HTTP {resp.status}" if resp.status in (403, 429) else "bot-check page")
        if not resp.from_cache and (resp.bot_wall or never_answered(resp)):
            return self.through_browser(url, resp)
        return resp

    def through_browser(self, url: str, plain: web.Response) -> web.Response:
        """A page or file that a site refuses to plain HTTP (a wall, or no answer at all), read through the browser tier
        (decisions #82, #103, #123): a person's browser, the site's front page first, stopping at any challenge that asks
        for an answer. DDWB_BROWSER=off, or no browser here: the plain answer stands, with the reason added."""
        if os.environ.get("DDWB_BROWSER", "").strip().lower() == "off":
            return plain
        fb = self.__dict__.get("_fallback")
        if fb is None:
            fb = self._fallback = Fallback(None, remember=self.clock)
            atexit.register(fb.close)  # the browser, if one opens, is shut when the tool call ends
        tries = [url] if not url.startswith("http://") else ["https://" + url[len("http://"):], url]  # HTTPS first, as browsers do
        for u in tries:
            got = fb.get(u)
            if got.ok and not got.bot_wall:
                self.clock.needs_browser(web.host_of(url), (plain.error or f"HTTP {plain.status}")[:80])
                got.via = "browser"
                return got
        if got.bot_wall:  # a challenge in the browser too: stopped there, never solved
            got.error = f"refused to plain HTTP ({plain.error or plain.status}) and to the browser ({got.error})"
            return got
        plain.error = f"{plain.error or f'HTTP {plain.status}'}; the browser tier: {got.error or f'HTTP {got.status}'}"
        return plain


    def post(self, url: str, data: dict | None = None, json_body: dict | None = None, headers: dict | None = None,
             use_cache: bool = True, fresh_since: str | None = None) -> web.Response:
        """A POST under the same shared host clock and bot-wall memory as get (portals that list meetings by form);
        `fresh_since` as for get."""
        payload = json.dumps({"data": data, "json": json_body, "headers": headers}, sort_keys=True)
        key = self._key(f"POST {url} {payload}")  # web.Http's key for a POST
        cached = use_cache and key.with_suffix(".json").exists()
        if cached and fresh_since and json.loads(key.with_suffix(".json").read_text()).get("fetched_at", "")[:19] < fresh_since[:19]:
            cached = use_cache = False
        host = web.host_of(url)
        if not cached:
            why = self.clock.blocked(host)
            if why:
                return web.Response(url, error=f"host stopped earlier (bot wall): {why}", bot_wall=True)
            self.clock.wait(host, web.HOST_GAP.get(web.site_of(url), web.MIN_GAP))
            self.last.pop(host, None)
        resp = super().post(url, data=data, json_body=json_body, headers=headers, use_cache=use_cache)
        if resp.bot_wall and not resp.from_cache:
            self.clock.block(host, f"HTTP {resp.status}" if resp.status in (403, 429) else "bot-check page")
        return resp


class RunFetch:
    """The session's polite GET and POST as a portal adapter's Fetch, read fresh once per run: a copy cached before the
    run started is fetched again (a portal's lists and meeting pages change as agendas and minutes are posted); within
    the run, the cache. Same User-Agent, host clock and bot-wall memory as every other tool."""

    def __init__(self, cx: Cx) -> None:
        self.cx, self._since = cx, None
        self.sent = 0  # requests that were not answered from the cache (what the map's runaway guard counts)

    def since(self) -> str:
        if self._since is None:
            self._since = self.cx.run_started()
        return self._since

    def _count(self, resp: web.Response) -> web.Response:
        self.sent += not resp.from_cache
        return resp

    def get(self, url: str, use_cache: bool = True) -> web.Response:
        return self._count(self.cx.http.get(url, use_cache, fresh_since=self.since()))

    def post(self, url: str, data: dict | None = None, json_body: dict | None = None, headers: dict | None = None,
             use_cache: bool = True) -> web.Response:
        return self._count(self.cx.http.post(url, data=data, json_body=json_body, headers=headers, use_cache=use_cache,
                                             fresh_since=self.since()))


# ------------------------------------------------------------------------------------------- one session
class Cx:
    def __init__(self, sid: str) -> None:
        parts = sid.split(":")
        if len(parts) != 3:
            raise Stop("session must be <run>:<arm>:<body>, e.g. 20261103-nav1:haiku-jev:cortez-co")
        self.sid, (self.run, self.arm, self.body) = sid, parts
        if not SLUG.fullmatch(self.arm):
            raise Stop("arm must be a short label of lower-case letters, digits and hyphens, e.g. claude or haiku-jev")
        if not SLUG.fullmatch(self.body):
            raise Stop("body must be a short label of lower-case letters, digits and hyphens, e.g. cortez-co")
        # the benchmark's arms as listed; any other arm (a plugin user's) has Jev on when its name ends -jev or when the
        # user has connected a judge (DDWB_JUDGE or providers.toml): turning Jev on in the settings is enough
        self.jev_on = ARMS[self.arm] if self.arm in ARMS else self.arm.endswith("-jev") or judge_connected()
        self.st = State(self.run)
        self.sess = self.st.session(sid)
        self._http, self._providers = None, None

    @property
    def http(self) -> Http:
        if self._http is None:
            self._http = Http()
        return self._http

    def adapter(self, name: str, plain: bool = True):
        """The portal's adapter (ddwb.portals.registry), one per tool call: over our polite GET read fresh once per run
        (`plain`), or made without plain HTTP (a portal that walls it: the adapter's own browser transport)."""
        made = self.__dict__.setdefault("_adapters", {})  # (a subclass may skip __init__)
        if (name, plain) not in made:
            if not made:
                atexit.register(self.close)  # a browser, if one is opened, is shut when the tool call ends
            fetch = self.run_fetch() if plain else None
            if not portal_registry.own_browser(name):  # every adapter without a browser path of its own: plain HTTP
                fetch = Fallback(fetch, remember=getattr(self.http, "clock", None))  # until a host walls it, then the browser
            made[(name, plain)] = portal_registry.adapter(name, fetch)
        return made[(name, plain)]

    def run_fetch(self) -> RunFetch:
        """The one RunFetch every adapter of this tool call shares (its count of requests sent is the map's guard)."""
        return self.__dict__.setdefault("_run_fetch", RunFetch(self))

    def close(self) -> None:
        """Shut what the adapters opened (a browser)."""
        for ad in self.__dict__.pop("_adapters", {}).values():
            shut = getattr(ad, "close", None) or getattr(getattr(ad, "browser", None), "close", None) or \
                (ad.http.close if isinstance(getattr(ad, "http", None), Fallback) else None)
            if callable(shut):
                try:
                    shut()
                except Exception:  # already gone
                    pass

    def caps(self) -> dict[str, float]:
        """This run's caps: fixed by its first `start`, else the defaults."""
        return DEFAULT_CAPS | (self.st.settings().get("caps") or {})

    @property
    def providers(self) -> Providers:
        if self._providers is None:  # the provider layer's limit: the sum of the run's caps, in dollars only
            self._providers = Providers(run_id=self.run, max_usd=sum(self.caps().values()), max_calls=None,
                                        approved_by=self.st.settings().get("approved_by") or "")
        return self._providers

    def need_session(self) -> dict:
        if not self.sess:
            raise Stop(f"no session yet: run `nav {self.sid} start` first")
        return self.sess

    def run_started(self) -> str:
        return self.st.db.execute("SELECT min(started_at) FROM sessions").fetchone()[0] or now()

    def stopped(self) -> str | None:
        r = self.st.db.execute("SELECT stopped FROM sessions WHERE stopped IS NOT NULL LIMIT 1").fetchone()
        return r[0] if r else None

    def stop(self, why: str) -> None:
        self.st.db.execute("UPDATE sessions SET stopped = ? WHERE stopped IS NULL", [why])

    def check(self, budget: str) -> float:
        if self.stopped():
            raise Stop(f"paid calls are stopped for this run: {self.stopped()}. Finish with what you have; `answer` marks it incomplete")
        cap = self.caps()[budget]
        left = cap - self.st.spent(budget)
        if left <= 0.0005:
            raise Stop(f"the ${cap:.2f} {budget} cap is used up. Finish with what you have; `answer` marks it incomplete")
        return left

    def node(self, ref: str, kinds: tuple = ()) -> dict:
        """A node by ID, or a page or link by its URL."""
        ref = ref.strip()
        if re.fullmatch(r"[A-Z][0-9a-f]{7}", ref):
            n = self.st.get(self.sid, ref)
        else:
            url = ref.split("#")[0]
            n = self.st.get(self.sid, nid("K", url)) or self.st.get(self.sid, nid("G", url))
        if not n:
            raise Stop(f"no node {ref!r} in this session (IDs come from the tools' output; a URL must have been seen first)")
        if kinds and n["kind"] not in kinds:
            raise Stop(f"{ref} is a {n['kind']}, this tool takes a {' or '.join(kinds)}")
        return n

    def card(self) -> dict:
        c = (self.sess or {}).get("card") or {}
        return {k: c[k] for k in ("name", "kind", "state", "city", "county") if k in c}

    # ------------------------------------------------------------------------------- paid
    def jev_many(self, items: list[tuple], qs: dict, budget: str) -> dict[str, tuple]:
        """Jev on many states at once: items are (key, state), or (key, state, questions) for an item with questions of
        its own (added to `qs`). Returns {key: (answers or None, cost, error or None)}; a failure is an error, never a "no"."""
        if not items:
            return {}
        left = self.check(budget)
        questions, lock, used, halted = jev_questions(qs), threading.Lock(), [0.0], [None]
        providers = self.providers  # made here: the threads share it (the store is used from this thread only)

        def one(item):
            key, state, *own = item
            with lock:
                if used[0] >= left:
                    return key, (None, 0.0, f"the {budget} cap was reached during this call")
                if halted[0]:
                    return key, (None, 0.0, f"paid calls stopped: {halted[0]}")
            err = None
            try:
                res = providers.judge(state, questions | jev_questions(own[0]) if own else questions, provider=JEV)
            except PriceDrift as error:
                if error.result is None:
                    return key, (None, 0.0, f"PriceDrift: {str(error)[:200]}")
                res, err = error.result, f"price guard: {str(error)[:200]} (answer kept; paid calls stopped)"
                with lock:
                    halted[0] = err
            except Exception as error:  # a failure is unknown, never a "no"
                return key, (None, 0.0, f"{type(error).__name__}: {str(error)[:200]}")
            cost = res.cost or 0.0
            with lock:
                used[0] += cost
            answers = compact((res.response or {}).get("answers"))
            return key, (answers, cost, err if answers else (err or "no answers in Jev's reply"))

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = dict(pool.map(one, items))
        if halted[0]:
            self.stop(halted[0])
        return results

    def scrape(self, url: str) -> tuple[web.Page | None, dict]:
        self.check("map")
        page, facts = web.scrape(self.providers, url)
        if facts.get("stopped"):  # the price guard: the page was paid for and is kept; paid calls stop
            self.stop(f"price guard: {facts['stopped']}")
        return page, facts


# ------------------------------------------------------------------------------------------- tools
def run_settings(cx: Cx, a) -> dict:
    """The run's caps and approver, fixed by its first `start`; a later start that asks for different caps is refused."""
    asked = {}
    for b, env in CAP_ENV.items():
        v = getattr(a, f"cap_{b}", None)
        v = v if v is not None else (float(os.environ[env]) if os.environ.get(env, "").strip() else None)
        if v is not None:
            if v < 0:
                raise Stop(f"--cap-{b} must be zero or more")
            asked[b] = float(v)
    approved_by = (getattr(a, "approved_by", None) or os.environ.get("DDWB_APPROVED_BY", "")).strip()
    had = cx.st.settings()
    fixed = cx.st.fix_settings(caps=DEFAULT_CAPS | asked, **({"approved_by": approved_by} if approved_by else {}))
    clash = {b: v for b, v in asked.items() if fixed["caps"].get(b) != v}
    if had and clash:
        raise Stop(f"this run's caps are already {fixed['caps']} (set by its first start); start a new run to change them")
    return fixed


def pick_body(cx: Cx, a) -> dict:
    """The registry body this session works on: a benchmark body's fixed ID, else --body-id, else the best match for
    --query. An ambiguous or empty match stops with the candidates, so the caller can choose by ID."""
    body_id = BODIES[cx.body]["body_id"] if cx.body in BODIES else (getattr(a, "body_id", None) or "").strip()
    if body_id:
        found = registry.registry().candidate(body_id)
        if not found:
            raise Stop(f"no body {body_id} in the registry")
        return found
    query = (getattr(a, "query", None) or "").strip()
    if not query:
        raise Stop(f"name the body: `nav {cx.sid} start --query \"Cortez, CO\"` or `--body-id <id>`")
    res = registry.resolve(query)
    if not res["candidates"]:
        raise Stop(f"no body in the registry matches {query!r}: add the state, or name it as the body calls itself")
    if res["ambiguous"]:
        raise Stop(f"{query!r} could be several bodies ({res['ambiguity']['why']}); start again with --body-id:\n"
                   + registry.format_result(res))
    return res["candidates"][0]


def homepage_of(body: dict) -> str | None:
    """The registry's chosen website (e-mail addresses typed as websites are already left out), as a URL."""
    sites = body.get("websites") or []
    site = next((w for w in sites if w.get("chosen")), sites[0] if sites else None)
    url = (site or {}).get("url")
    return (url if url.startswith("http") else "https://" + url) if url else None


def other_sites(cx: Cx, home: str | None) -> list[str]:
    """The registry's other websites for this body (sources disagree, and one may be stale)."""
    try:
        body = registry.registry().candidate(cx.sess["body_id"]) or {}
    except Exception:  # the registry is optional here: the line is only a hint
        return []
    urls = [(w.get("url") or "") for w in body.get("websites") or []]
    urls = [u if u.startswith("http") else "https://" + u for u in urls if u]
    return [u for u in dict.fromkeys(urls) if u.rstrip("/") != (home or "").rstrip("/")][:5]


def likely_line(card: dict) -> str:
    """Where bodies like this one publish: a hint for where to look first, not a list to stop at."""
    portals, basis = portals_prior.likely_portals(card)
    if not portals:
        return "LIKELY PORTALS: no prior for this kind of body; most bodies publish on their own sites"
    return ("LIKELY PORTALS (a hint, not a list to stop at; " + basis + "): " + ", ".join(f"{x['portal']} {x['share']:.0%}" for x in portals)
            + ". Most bodies publish on their own sites; a link to one of these portals on the body's site is worth following first")


def t_start(cx: Cx, a) -> None:
    settings = run_settings(cx, a)
    if not cx.sess:
        body = pick_body(cx, a)
        body_id = body["body_id"]
        card = registry.body_card(body_id)
        home = homepage_of(body)
        cx.st.start(cx.sid, run_id=cx.run, arm=cx.arm, body=cx.body, body_id=body_id, query="q1", since=SINCE.isoformat(),
                    started_at=now(), card=card, homepage=home, status="started")
        cx.sess = cx.st.session(cx.sid)
        cx.st.put(cx.sid, "B", "body", "started", url=home, data={"card": card, "run_settings": settings}, tool="start")
    s = cx.sess
    say(f"SESSION {cx.sid}",
        "QUERY: " + QUERY.format(name=s["card"].get("name", cx.body).title(), since=SINCE.isoformat()),
        f"BODY: {json.dumps(cx.card())}",
        f"HOMEPAGE (registry): {s['homepage'] or 'none on record'}",
        *([f"OTHER SITES (registry): {', '.join(others)}"] if (others := other_sites(cx, s['homepage'])) else []),
        "IF THE HOMEPAGE FAILS: the registry's sites can be stale; find the body's official site or meetings portal "
        "with `search`, or your own web search, then `fetch` or `portal detect` its URL",
        likely_line(cx.card()),
        f"WINDOW: documents dated {SINCE.isoformat()} to today",
        f"JEV: {'on: automatic on the links of pages you fetch, pages you check, links on lists you map, and every item you read (items at p ≤ 0.2 are set aside for you)' if cx.jev_on else ('off in this arm' if cx.arm in ARMS else 'off: not connected (Jev in the plugin settings, or DDWB_JUDGE=api:vercel)')}",
        budget_line(cx),
        f"NEXT: nav {cx.sid} fetch {s['homepage']}" if s["homepage"] else f"NEXT: nav {cx.sid} search \"<body name> board meeting agendas\"")


def budget_line(cx: Cx) -> str:
    caps = cx.caps()
    return "CAPS (shared by every arm of this run): " + ", ".join(
        f"{b} ${caps[b] - cx.st.spent(b):.3f} left of ${caps[b]:.2f}" for b in caps) + (f"; STOPPED: {cx.stopped()}" if cx.stopped() else "")


def record_page_links(cx: Cx, page: web.Page, parent: str, show_all: bool) -> list[tuple[str, web.Link]]:
    """A page's links as page nodes: every link in the Jev arms (Jev ranks them), those the plain rule keeps otherwise."""
    shown, seen = [], {page.url.split("#")[0].rstrip("/")}
    for l in page.links:
        if not l.href.startswith("http") or text.SKIP.search(l.href):
            continue
        if not show_all and not cx.jev_on and not text.keep_for_locate(l.href, l.text):
            continue
        url = l.href.split("#")[0]
        if url.rstrip("/") in seen:  # each address once; the page itself and its in-page anchors left out
            continue
        seen.add(url.rstrip("/"))
        g = nid("G", url)
        if not cx.st.get(cx.sid, g):
            cx.st.put(cx.sid, g, "page", "found", url=url, parent=parent, tool="fetch",
                      data={"title": short(l.text, 200), "source": "link", "platform": platform_of(url), "is_file": is_file(url)})
        shown.append((g, l))
    return shown


def score_pages(cx: Cx, gs: list[str], on_page: str) -> None:
    """Jev on a page's links (automatic in the Jev arms): does each lead to the governing board's meetings?"""
    todo = []
    for g in dict.fromkeys(gs):
        n = cx.st.get(cx.sid, g)
        if n and p_of(n, "route") is None and not (n.get("jev") or {}).get("error") and not n["data"].get("is_file"):
            todo.append((g, {"body": cx.card(), "link": {"text": n["data"].get("title"), "url": n["url"], "on_page": on_page}}))
    results = cx.jev_many(todo, ROUTE, "map")
    with cx.st.batch():
        for g, (answers, cost, err) in results.items():
            n = cx.st.get(cx.sid, g)
            cx.st.put(cx.sid, g, "page", n["status"], tool="fetch:jev", jev=(n.get("jev") or {}) | (answers or {}) | ({"error": err} if err else {}),
                      cost=cost, budget="map")


def show_page(cx: Cx, node: str, page: web.Page, a) -> None:
    all_links = getattr(a, "all_links", False)
    links = record_page_links(cx, page, node, all_links)
    if cx.jev_on:
        score_pages(cx, [g for g, _ in links], short(page.title, 120))
        p = {g: p_of(cx.st.get(cx.sid, g), "route") for g, _ in links}
        links.sort(key=lambda x: -1 if p[x[0]] is None else -p[x[0]])  # unknown first: it is never a no
        low = [g for g, _ in links[ROUTE_TOP:] if p[g] is not None and p[g] <= SET_ASIDE]
        if not all_links:
            links = [(g, l) for g, l in links if g not in low]
    pages = [(g, l) for g, l in links if not is_file(l.href)]
    files = [(g, l) for g, l in links if is_file(l.href)]
    dated_files = sum(1 for _, l in files if dated(l, page.url))
    pj = (lambda g: f"{fmt_p(p_of(cx.st.get(cx.sid, g), 'route'))}  ") if cx.jev_on else (lambda g: "")
    if cx.jev_on:
        which = "all, ranked by Jev's p that it leads to the board's meetings" if all_links else \
            f"ranked by Jev's p that it leads to the board's meetings: the top {ROUTE_TOP} and any above p {SET_ASIDE}; " \
            f"{len(low)} more at p ≤ {SET_ASIDE} not shown (--all-links lists them)"
    else:
        which = "of all" if all_links else "that look like meetings, boards or documents; --all-links for every link"
    say(f"PAGE {node}: {short(page.title, 120)}  ({page.url})",
        f"TEXT: {short(page.text, 1200)}",
        f"PAGES LINKED ({len(pages)} {which}):")
    for g, l in pages[:120]:
        tags = platform_of(l.href) or ""
        say(f"  {g}  {pj(g)}{short(l.text, 70)}  → {short(l.href, 140)}" + (f"  [{tags}]" if tags else ""))
    if len(pages) > 120:
        say(f"  … and {len(pages) - 120} more (all recorded; `next` lists unchecked ones)")
    if files:
        say(f"FILES LINKED: {len(files)}, {dated_files} of them dated" + (" — this page may itself be a meetings list: check it" if dated_files >= 3 else "")
            + "; open any with peek or read (a document off the mapped lists is counted as such):")
        for g, l in files[:40]:
            when = dated(l, page.url)
            say(f"  {g}  {pj(g)}{when.isoformat() if when else 'undated   '}  {short(l.text, 70)}")
        if len(files) > 40:
            say(f"  … and {len(files) - 40} more")
    for f in page.forms:  # dropdowns (a year, a month, a board) are often the only way to the other views of a list
        if f["method"] != "get" or not any(x["type"] == "select" for x in f["fields"]):
            continue
        parts = []
        for x in f["fields"]:
            if x["type"] == "select":
                opts = [o[0] for o in x["options"]]
                parts.append(f"{x['name']}=[{' | '.join(opts[:25])}{' …' if len(opts) > 25 else ''}]")
            else:
                parts.append(f"{x['name']}={x['value'] or '<' + x['type'] + '>'}")
        say(f"FORM (GET) {f['action']}: " + "; ".join(parts),
            "  → another view of this page: fetch the action with the fields as ?name=value&name=value")


def t_fetch(cx: Cx, a) -> None:
    cx.need_session()
    url = cx.node(a.target)["url"] if re.fullmatch(r"[A-Z][0-9a-f]{7}", a.target) else a.target.split("#")[0]
    g, started = nid("G", url), time.time()
    resp = cx.http.get(url, fresh_since=cx.run_started())
    ms = int((time.time() - started) * 1000)
    if not resp.ok:
        cx.st.put(cx.sid, g, "page", "failed", url=url, tool="fetch", ms=ms,
                  data={"error": resp.error or f"HTTP {resp.status}", "bot_wall": resp.bot_wall, "via": "http"})
        raise Stop(f"{url}: {resp.error or f'HTTP {resp.status}'}" + (WALLED if resp.bot_wall else
                   ". Try `scrape` (the scrape provider; may be paid) if the page may need rendering"))
    if "html" not in (resp.content_type or ""):
        cx.st.put(cx.sid, g, "page", "fetched", url=url, tool="fetch", ms=ms, data={"content_type": resp.content_type, "via": "http"})
        raise Stop(f"{url} is a file ({resp.content_type}), not a page: open it with `nav {cx.sid} peek {g}` or `read {g}`")
    page = web.read_html(resp.text, resp.final_url or url)
    found, by = portals_of(cx, resp.final_url or url, resp.text)
    cx.st.put(cx.sid, g, "page", "fetched", url=url, tool="fetch", ms=ms,
              data={"title": page.title, "final_url": resp.final_url, "status": resp.status, "n_links": len(page.links), "via": "http"}
              | ({"portals": [list(x) for x in found], "portals_by": by} if found else {}))
    show_page(cx, g, page, a)
    for line in portal_lines(cx, g, found, by, page):
        say(line)


def t_scrape(cx: Cx, a) -> None:
    cx.need_session()
    url = cx.node(a.target)["url"] if re.fullmatch(r"[A-Z][0-9a-f]{7}", a.target) else a.target.split("#")[0]
    g = nid("G", url)
    page, facts = cx.scrape(url)
    cx.st.put(cx.sid, g, "page", "fetched" if page else "failed", url=url, tool="scrape", cost=facts.get("cost_usd") or 0.0,
              budget="map", ms=facts.get("elapsed_ms"),
              data={"title": page.title if page else None, "n_links": len(page.links) if page else 0, "via": "firecrawl",
                    "error": facts.get("error"), "bot_wall": facts.get("bot_wall")})
    if not page:
        raise Stop(f"{url}: the scrape gave no page ({facts.get('error') or facts.get('status')})"
                   + (WALLED if facts.get("bot_wall") else ""))
    found, by = portals_of(cx, page.url or url, links_blob(page))
    if found:
        cx.st.put(cx.sid, g, "page", "fetched", tool="scrape", data={"portals": [list(x) for x in found], "portals_by": by})
    show_page(cx, g, page, a)
    for line in portal_lines(cx, g, found, by, page):
        say(line)


def t_search(cx: Cx, a) -> None:
    cx.need_session()
    cx.check("map")
    engine = a.engine or ("exa" if a.domain else "parallel")
    if engine == "parallel" and a.domain:
        raise Stop("Parallel here takes no domain; use --engine exa with --domain to search within one site")
    started = time.time()
    try:
        if engine == "parallel":
            res = cx.providers.search(a.query, objective=a.query, provider=PARALLEL)
        else:
            res = cx.providers.search(a.query, domain=a.domain, n=10, provider=EXA)
    except PriceDrift as drift:  # paid: keep the answer, stop paid calls
        res = drift.result
        cx.stop(f"price guard: {str(drift)[:200]}")
        if res is None:
            raise Stop(f"search stopped by the price guard: {str(drift)[:200]}")
    except ProviderError as error:
        if type(error).__name__ == "MissingKey":
            raise Stop("no search provider is set up here (a Parallel or Exa key): use your own web search to find the "
                       "body's official website or its meetings portal, then `fetch` or `portal detect` that URL") from None
        raise Stop(f"search failed ({type(error).__name__}: {str(error)[:200]}): not a 'no results'; try the other engine")
    results = res.response
    s = nid("S", f"{engine}|{a.domain}|{a.query}")
    cx.st.put(cx.sid, s, "search", "done", tool="search", cost=res.cost or 0.0, budget="map", ms=int((time.time() - started) * 1000),
              data={"engine": engine, "query": a.query, "domain": a.domain, "n_results": len(results), "cache_hit": res.cached})
    say(f"SEARCH {engine}{' within ' + a.domain if a.domain else ''}: {len(results)} results (${res.cost or 0:.4f})")
    for rank, x in enumerate(results, start=1):
        url = (x.get("url") or "").split("#")[0]
        if not url:
            continue
        g = nid("G", url)
        if not cx.st.get(cx.sid, g):
            cx.st.put(cx.sid, g, "page", "found", url=url, parent=s, tool="search",
                      data={"title": short(x.get("title"), 200), "source": engine, "rank": rank, "platform": platform_of(url),
                            "is_file": is_file(url), "excerpt": short(x.get("excerpt"), 300)})
        tags = " ".join(t for t in (platform_of(url) or "", "file" if is_file(url) else "") if t)
        say(f"  {g}  {short(x.get('title'), 80)}  → {short(url, 140)}" + (f"  [{tags}]" if tags else ""))


def dated(l: web.Link, page_url: str) -> date | None:
    default_year = text.find_year(l.heading) or text.find_year(page_url)
    return text.find_date(l.text) or text.find_date(unquote(l.href)) or text.find_date(l.row, default_year)


def t_check(cx: Cx, a) -> None:
    """Is this page the governing board's meetings list? Facts by plain reading; Jev's answer in the Jev arms."""
    cx.need_session()
    n = cx.node(a.target, ("page",))
    url, platform, cost, page = n["url"], platform_of(n["url"]), 0.0, None
    facts = {"platform": platform}
    found, by = ([], "") if platform == "diligent" else portals_of(cx, url)  # a portal by its address: read through it
    listing = None
    if found:
        name, client = found[0]
        try:
            listing = cx.adapter(name).meetings(client, SINCE, date.today() + timedelta(days=PORTAL_AHEAD))
        except Exception as error:
            say(portal_failure(name, client, error) + "; the page is read as a plain page instead")
            facts["portal_error"] = short(portal_failure(name, client, error), 300)
    if listing is not None:
        gov = sum(1 for m in listing.meetings if portal_governing(cx, m))
        facts |= {"via": f"portal:{name}", "portal": name, "client": client, "meetings_in_window": len(listing.meetings),
                  "governing_meetings": gov, "meetings_per_year": listing.per_year,
                  "boards": sorted({m.board for m in listing.meetings if m.board})[:20],
                  "portal_notes": [short(x, 200) for x in listing.notes[:6]]}
        newest = sorted(listing.meetings, key=lambda m: m.date or date.min, reverse=True)[:30]
        state_page = {"url": url, "title": f"{name} meetings portal ({client})", "boards": facts["boards"],
                      "dated_links": [{"text": short(meeting_row(m), 100), "date": m.date.isoformat() if m.date else None}
                                      for m in newest]}
    elif platform == "diligent":
        resp = cx.http.get(diligent.meetings_url(diligent.origin(url), SINCE, date.today() + timedelta(days=60)), fresh_since=cx.run_started())
        meetings = diligent.read_meetings(resp.body) if resp.ok else []
        facts |= {"via": "diligent_api", "meetings_in_window": len(meetings),
                  "boards": sorted({m.get("MeetingTypeName") or m.get("Name") or "" for m in meetings})[:20]}
        state_page = {"url": url, "title": "Diligent meetings portal",
                      "dated_links": [{"text": m.get("Name"), "date": str(m.get("MeetingDate"))[:10]} for m in meetings[:30]]}
    else:
        if a.via == "firecrawl":
            page, f = cx.scrape(url)
            cost = f.get("cost_usd") or 0.0
            if not page:
                raise Stop(f"the scrape gave no page for {url} ({f.get('error') or f.get('status')})")
            html = links_blob(page)
        else:
            resp = cx.http.get(url, fresh_since=cx.run_started())
            page = web.read_html(resp.text, resp.final_url or url) if resp.ok and "html" in (resp.content_type or "") else None
            if not page:
                cx.st.put(cx.sid, n["node"], "page", "failed", tool="check", data={"error": resp.error or f"HTTP {resp.status}"})
                raise Stop(f"could not read {url} with our GET ({resp.error or f'HTTP {resp.status}'}); try `check {n['node']} --via firecrawl`")
            html = resp.text
        if not found and platform != "destinyhosted":  # a body's own page that links or embeds a portal
            found, by = portals_of(cx, page.url or url, html)
        views = [page]
        if platform == "destinyhosted":  # one month per page: the two months before this one as well, so the check is fair
            m = re.search(r"[?&]id=(\d+)", url)
            for back in (1, 2):
                d = (date.today().replace(day=1) - timedelta(days=31 * back - 15)).replace(day=1)
                r = cx.http.get(f"https://public.destinyhosted.com/agenda_publish.cfm?id={m.group(1)}&mt=ALL&get_month={d.month}"
                                f"&get_year={d.year}", fresh_since=cx.run_started()) if m else None
                if r is not None and r.ok:
                    views.append(web.read_html(r.text, r.final_url or url))
            facts["note"] = f"a Destiny portal shows one month per page; these counts cover {len(views)} months"
        rows = []
        for v in views:
            for l in v.links:
                if text.keep_for_documents(l.href, l.text, l.row):
                    rows.append((dated(l, v.url), l))
        in_window = [(d, l) for d, l in rows if d and d >= SINCE]
        facts |= {"via": a.via or "http", "title": page.title, "n_links": len(page.links), "document_like_links": len(rows),
                  "dated_in_window": len(in_window), "years": sorted({d.year for d, _ in rows if d})}
        ordered = sorted(in_window, key=lambda x: x[0], reverse=True) + [(d, l) for d, l in rows if not d]
        state_page = {"url": url, "title": page.title, "headings": list(dict.fromkeys(l.heading for l in page.links if l.heading))[:15],
                      "dated_links": [{"text": short(l.text, 100), "row": short(l.row, 150), "date": d.isoformat() if d else None}
                                      for d, l in ordered[:30]]}
    jev = None
    if cx.jev_on:
        key = n["node"]
        answers, jcost, err = cx.jev_many([(key, {"body": cx.card(), "page": state_page})], LIST_PAGE, "map")[key]
        jev, cost = (n.get("jev") or {}) | (answers or {}) | ({"error": err} if err else {}), cost + jcost
    keep = n["status"] if n["status"] in ("accepted", "mapped") else "checked"  # a re-check never undoes an accept or a map
    if found:  # what map reads the list through
        facts |= {"portals": [list(x) for x in found], "portals_by": by}
    cx.st.put(cx.sid, n["node"], "page", keep, tool="check", data=facts, jev=jev, cost=cost, budget="map" if cost else None)
    say(f"CHECK {n['node']} {url}", "FACTS: " + json.dumps(facts))
    if cx.jev_on:
        say(f"JEV: is the governing board's meetings list: {fmt_p((jev or {}).get('is_list'))}; whose: {(jev or {}).get('whose')}"
            + (f"; ERROR (unknown, not a no): {jev['error']}" if jev and jev.get("error") else ""))
    for line in portal_lines(cx, n["node"], found, by, page):
        say(line)
    say(f"DECIDE: nav {cx.sid} accept {n['node']} --reason \"...\"   or   nav {cx.sid} skip {n['node']} --reason \"...\"")


def t_accept(cx: Cx, a) -> None:
    cx.need_session()
    n = cx.node(a.target, ("page",))
    if n["status"] != "checked":
        raise Stop(f"check {n['node']} first: a list is accepted on what check found")
    cx.st.put(cx.sid, n["node"], "page", "accepted", reason=a.reason, tool="accept")
    say(f"ACCEPTED {n['node']} as a meetings list. NEXT: nav {cx.sid} map {n['node']}")


# ------------------------------------------------------------------------------------------- portals
# A meeting portal (Legistar, BoardDocs, CivicClerk, ... : ddwb.portals) is read through its adapter: the portal's own
# service, not its page, which is often a script-built shell. What an adapter returns becomes the same nodes a plain
# list's links do (link, meeting), with where it came from (portal, client, the portal's meeting id, the transport it
# was read with), so `download` fetches each document the way the portal allows and readers find the portal's items.
def portals_of(cx: Cx, url: str, html: str | None = None) -> tuple[list[tuple[str, str]], str]:
    """The known portals a page is on, by its address; else those its page links or embeds (each adapter's detect).
    Returns ([(portal, client id)], "address" or "page")."""
    names = portal_registry.names()

    def ask(name: str, page: str | None) -> str | None:
        try:
            return cx.adapter(name).detect(url, page)
        except Exception as error:  # said, and nothing recognised by that adapter on this page
            say(f"PORTAL DETECT: the {name} adapter failed on {short(url, 100)} ({type(error).__name__}: {short(str(error), 120)})")
            return None

    found = [(n, c) for n in names if (c := ask(n, None))]
    if found or not html:
        return found, "address"
    return [(n, c) for n in names if (c := ask(n, html))], "page"


def links_blob(page: web.Page) -> str:
    """A page read by the scrape provider has no HTML: its links, as HTML the detectors can read."""
    return " ".join(f'<a href="{l.href}">{l.text}</a>' for l in page.links)


def portal_failure(name: str, client: str, error: Exception) -> str:
    """An adapter's failure as the agent sees it: a bot wall or challenge (stopped, never solved) or the portal's error."""
    if isinstance(error, Challenge):
        return f"BOT WALL at {name} ({client}): {short(str(error), 240)}; stopped there, never solved"
    kind = "" if isinstance(error, PortalError) else f"{type(error).__name__}: "
    return f"PORTAL ERROR at {name} ({client}): {kind}{short(str(error), 240)}"


def portal_link(cx: Cx, page: web.Page, name: str, client: str) -> str | None:
    """The page node of a link on this page that goes to the portal (recorded if new): the address to check and map."""
    ad = cx.adapter(name)
    for l in page.links:
        if not l.href.startswith("http") or is_file(l.href):
            continue
        try:
            same = ad.detect(l.href) == client
        except Exception:
            same = False
        if same:
            url = l.href.split("#")[0]
            g = nid("G", url)
            if not cx.st.get(cx.sid, g):
                cx.st.put(cx.sid, g, "page", "found", url=url, tool="portal",
                          data={"title": short(l.text, 200), "source": "link", "platform": name, "is_file": False,
                                "portals": [[name, client]], "portals_by": "address"})
            return g
    return None


def portal_lines(cx: Cx, node: str, found: list, by: str, page: web.Page | None = None) -> list[str]:
    """What `fetch` and `check` say of a portal recognised on a page: where it is and how to map it."""
    if not found:
        return []
    if by == "address":
        names = ", ".join(f"{n} (client {c})" for n, c in found)
        return [f"PORTAL: this page is on {names}. Once checked and accepted, `map` lists every meeting and its documents "
                f"through the portal's own service; `portal meetings {node}` shows the meetings now"]
    lines = []
    for name, client in found:
        g = portal_link(cx, page, name, client) if page else None
        lines.append(f"PORTAL ON THIS PAGE: it links or embeds {name} (client {client}). "
                     + (f"Its link {g} goes there: check, accept and map it to read the portal's meetings" if g else
                        "Check, accept and map this page: map reads the portal too")
                     + f"; `portal meetings {name}:{client}` lists them now")
    return lines


def portal_governing(cx: Cx, m: PortalMeeting) -> bool:
    """The governing board's meeting: as the portal says, else the plain rule on its board and title."""
    return m.governing if m.governing is not None else is_governing(f"{m.board} {m.title}", cx.card().get("name", ""))


def meeting_row(m: PortalMeeting) -> str:
    return m.title if not m.board or m.board.lower() in (m.title or "").lower() else f"{m.board}: {m.title}"


def meeting_facts(name: str, client: str, m: PortalMeeting, governing: bool) -> dict:
    return {"date": m.date.isoformat() if m.date else None, "governing": governing, "title": short(meeting_row(m), 300),
            "portal_title": m.title, "board": m.board, "meeting_kind": m.kind, "portal_governing": m.governing,
            "portal": name, "client": client, "meeting_id": m.id, "url": m.url}


def meeting_of(d: dict) -> PortalMeeting:
    """A portal meeting node's facts as the adapter's Meeting again (for its documents, items or a fresh address)."""
    return PortalMeeting(str(d["meeting_id"]), date.fromisoformat(d["date"]) if d.get("date") else None,
                         d.get("portal_title") or d.get("title") or "", board=d.get("board") or "",
                         kind=d.get("meeting_kind") or "other", governing=d.get("portal_governing"), url=d.get("url"))


def portal_entries(name: str, client: str, m: PortalMeeting, mk: str, governing: bool, docs: list, items: list | None,
                   list_url: str | None) -> dict[str, dict]:
    """A portal meeting's documents, and its items' files, as the map's link facts: what a list's links carry (date,
    kind, governing), plus where they came from, so `download` can route them and readers find the meeting."""
    row, out = meeting_row(m), {}
    origin = {"portal": name, "client": client, "meeting_id": m.id, "meeting_node": mk, "meeting_url": m.url}

    def put(doc, text_: str, row_: str, kind: str, item_no: str | None) -> None:
        href = (doc.url or "").split("#")[0]
        if href and href not in out:
            out[href] = {"text": short(text_, 300), "row": short(row_, 300), "heading": short(m.board, 150),
                         "date": m.date.isoformat() if m.date else None, "rule_kind": kind, "rule_governing": governing,
                         "list_page": list_url, **origin, "portal_kind": doc.kind,
                         **({"format": doc.format} if doc.format else {}), **({"item_no": item_no} if item_no else {})}

    for d in docs:
        put(d, d.title or d.kind.title(), row, d.kind, d.item_no)
    for it in items or []:  # an item's file is that item's, whatever it is: minutes filed here are an earlier meeting's
        for d in it.attachments:
            put(d, d.title or "attachment", f"{row} | item {it.number or '?'}: {it.title}", "item", it.number)
    return out


def item_facts(items: list) -> list[dict]:
    """A meeting's items as the portal publishes them: its own numbers, titles, sections, outcomes and files (each with
    the link ID it has once `portal documents` puts it in the map)."""
    out = []
    for it in items:
        f = {"no": it.number, "title": short(it.title, 300),
             "files": [{"id": nid("K", d.url.split("#")[0]), "title": short(d.title, 120), "url": d.url}
                       for d in it.attachments if d.url]}
        for key, n in (("section", 150), ("outcome", 200), ("text", 600)):
            if getattr(it, key, None):
                f[key] = short(getattr(it, key), n)
        out.append(f)
    return out


def transport_of(ad, client: str, url: str | None = None) -> str:
    """"browser" when the adapter reads this portal (this URL's host, when given) through a browser (its own transport,
    or after plain HTTP was walled), else "http". Recorded on each link, so `download` fetches the document the same way."""
    if getattr(ad, "transport", "http") == "browser":
        return "browser"
    if isinstance(getattr(ad, "http", None), Fallback):
        fb = ad.http
        return fb.via(url) if url else ("browser" if fb.http is None or fb.moved else "http")
    fetch_for = getattr(ad, "fetch_for", None)
    if not callable(fetch_for):
        return "http"
    try:
        return "http" if fetch_for(client) is getattr(ad, "http", None) else "browser"
    except Exception:
        return "http"


def list_portals(cx: Cx, url: str, d0: dict) -> tuple[list[tuple[str, str]], str]:
    """The portals an accepted list is on or reads from: as `fetch`, `check` or `portal detect` recorded them, else
    detected now, by its address, then on its page."""
    if d0.get("portals"):
        return [tuple(x) for x in d0["portals"]], d0.get("portals_by") or "page"
    found, by = portals_of(cx, url)
    if found:
        return found, by
    resp = cx.http.get(url, fresh_since=cx.run_started())
    if resp.ok and "html" in (resp.content_type or ""):
        return portals_of(cx, resp.final_url or url, resp.text)
    return [], ""


def map_portal(cx: Cx, g: dict, name: str, client: str, found: dict, rebuild: bool) -> dict:
    """One portal's meetings from SINCE to today + PORTAL_AHEAD through its adapter; each meeting's documents (the
    governing board's first, newest first; at most MAX_VIEWS requests, a runaway guard that is said, never silent); and the
    items of the governing board's meetings held so far, where the portal is read over plain HTTP (a browser portal's
    items cost a request per item: `portal documents <M>` reads one meeting's). Items are kept on the meeting node; their
    files join the map when `portal documents <M>` asks (a county's items carry ~150 files a meeting, mostly covered
    once its minutes are read). Links go into `found` as a plain list's do. Our own failures are notes (gaps); the
    portal's own remarks are portal notes (shown, not gaps)."""
    out = {"name": name, "client": client, "ok": False, "wall": False, "meetings": {}, "notes": [], "info": [],
           "portal_notes": [], "line": "", "view": None}
    v = nid("V", f"portal:{name}:{client}")
    if cx.st.get(cx.sid, v) and not rebuild:
        out |= {"ok": True, "line": f"PORTAL {name} ({client}): already read by an earlier map; its links are in the map"}
        out["info"].append(f"portal {name} ({client}) already read by an earlier map")
        return out
    ad = cx.adapter(name)
    until = date.today() + timedelta(days=PORTAL_AHEAD)
    try:
        listing = ad.meetings(client, SINCE, until)
    except Exception as error:
        out["wall"] = isinstance(error, Challenge)
        out["notes"].append(portal_failure(name, client, error))
        out["line"] = out["notes"][-1]
        return out
    out["ok"] = True
    out["portal_notes"] += listing.notes
    today = date.today()
    ranked = sorted(((m, portal_governing(cx, m)) for m in listing.meetings),
                    key=lambda x: (not x[1], -(x[0].date or date.min).toordinal()))
    rf, sent = cx.run_fetch(), 0  # requests the documents and items cost: a call answered from the listing costs none

    def call(fn, *args):
        nonlocal sent
        before, browser = rf.sent, transport_of(ad, client) == "browser"
        try:
            return fn(*args)
        finally:  # a browser's requests are not seen here: one per call, at the least
            sent += max(1, rf.sent - before) if browser else rf.sent - before

    docs_read = items_read = cut_gov = cut_other = 0
    wall = None
    for m, governing in ranked:
        mk = nid("M", f"{name}|{client}|{m.id}")
        facts = out["meetings"][mk] = meeting_facts(name, client, m, governing)
        label = f"{m.date} {short(meeting_row(m), 70)}"
        if wall or sent >= MAX_VIEWS:
            facts["documents_listed"] = False
            cut_gov, cut_other = cut_gov + governing, cut_other + (not governing)
            continue
        try:
            docs = call(ad.documents, client, m)
            docs_read += 1
        except Exception as error:
            facts["documents_error"] = short(str(error), 300)
            if isinstance(error, Challenge):
                wall = error
                out["notes"].append(portal_failure(name, client, error) + f": documents from {label} on not read")
                facts["documents_listed"] = False
                cut_gov, cut_other = cut_gov + governing, cut_other + (not governing)
                continue
            out["notes"].append(f"{name} meeting {label}: documents not read ({portal_failure(name, client, error)})")
            docs = []
        out["portal_notes"] += [f"{label}: {x}" for x in getattr(docs, "notes", None) or []]
        items = None
        if governing and m.date and m.date <= today and getattr(ad, "transport", "http") == "http" and sent < MAX_VIEWS:
            try:
                items = call(ad.items, client, m)
                items_read += 1
            except Exception as error:  # the documents carry the meeting; its items are extra
                facts["items_error"] = short(str(error), 300)
                if isinstance(error, Challenge):
                    wall = error
                    out["notes"].append(portal_failure(name, client, error) + f": stopped at the items of {label}")
                else:
                    out["portal_notes"].append(f"{label}: items not read ({short(str(error), 160)})")
        if items:
            facts["items"] = item_facts(items)
        entries = portal_entries(name, client, m, mk, governing, docs, None, m.url or g["url"])
        facts["n_links"] = len(entries)
        for href, f in entries.items():
            found.setdefault(href, f)
    if cut_gov:
        out["notes"].append(f"{name} ({client}): documents of {cut_gov} governing-board meetings not listed "
                            f"({'stopped at the bot wall' if wall else f'stopped at {MAX_VIEWS} requests, a runaway guard'}); "
                            "`portal documents <M>` lists one meeting's")
    if cut_other:
        (out["notes"] if wall else out["portal_notes"]).append(
            f"documents of {cut_other} other boards' meetings not listed ({'the bot wall' if wall else f'{MAX_VIEWS} requests'}); "
            "`portal documents <M>` lists one meeting's")
    transport = transport_of(ad, client)
    for href, f in found.items():
        if f.get("portal") == name and f.get("client") == client:
            f["transport"] = transport_of(ad, client, href)
    n_gov = sum(1 for _, gv in ranked if gv)
    years = ", ".join(f"{y}: {n}" for y, n in listing.per_year.items()) or "none"
    out["view"] = (v, {"portal": name, "client": client, "meetings": len(listing.meetings), "per_year": listing.per_year,
                       "governing_meetings": n_gov, "documents_read": docs_read, "items_read": items_read, "transport": transport})
    out["line"] = (f"PORTAL {name} ({client}): {len(listing.meetings)} meetings {SINCE} to {until} ({years}), {n_gov} of the "
                   f"governing board; documents of {docs_read} meetings and items of {items_read} read"
                   + (" through a browser (the portal walls plain HTTP)" if transport == "browser" else ""))
    return out


# ------------------------------------------------------------------------------------------- map
def t_map(cx: Cx, a) -> None:
    cx.need_session()
    g = cx.node(a.target, ("page",))
    if g["status"] not in ("accepted", "mapped"):
        raise Stop(f"{g['node']} is not an accepted list: check it, then accept it")
    if g["status"] == "mapped" and not a.rebuild:
        return show_links(cx, a)
    url, d0 = g["url"], g.get("data") or {}
    platform, found, views, notes, info, cost = d0.get("platform") or platform_of(url), {}, 0, [], [], 0.0  # notes: gaps; info: not gaps

    def add(href, link_text, row, heading, when, kind, governing, page_url, extra=None):
        href = href.split("#")[0]
        if href in found:
            return
        found[href] = {"text": short(link_text, 300), "row": short(row, 300), "heading": short(heading, 150),
                       "date": when.isoformat() if when else None, "rule_kind": kind, "rule_governing": governing,
                       "list_page": page_url, **(extra or {})}

    out_of_window = 0
    # A list on a known portal (by its address), or a page that links or embeds one, is read through the portal's adapter;
    # a page that only links one is read as a page too. Diligent Community and Destiny have no adapter: their own
    # branches below. `--plain` reads the list as a plain page only (a portal recognised by mistake).
    done, by, generic, portal_notes = [], "", True, []
    if platform not in ("diligent", "destinyhosted") and not getattr(a, "plain", False):
        listed, by = list_portals(cx, url, d0)
        for name, client in listed[:PORTALS_PER_LIST]:
            r = map_portal(cx, g, name, client, found, a.rebuild)
            done.append(r)
            notes += r["notes"]
            info += r["info"]
            portal_notes += r["portal_notes"]
        if len(listed) > PORTALS_PER_LIST:
            notes.append(f"portals on this page not read (more than {PORTALS_PER_LIST}): "
                         + ", ".join(f"{n} ({c})" for n, c in listed[PORTALS_PER_LIST:]) + "; `portal meetings <portal>:<client>`")
        if by == "address" and done:
            generic = not any(r["ok"] or r["wall"] for r in done)  # the list is the portal; at a wall, stopped there
            if generic:
                notes.append("the portal could not be read: the list was read as a plain page instead")
    if platform == "diligent":
        portal = diligent.origin(url)
        resp = cx.http.get(diligent.meetings_url(portal, SINCE, date.today() + timedelta(days=60)), fresh_since=cx.run_started())
        if not resp.ok:
            raise Stop(f"Diligent meetings service failed: {resp.error or resp.status}")
        for m in diligent.read_meetings(resp.body):
            when = date.fromisoformat(str(m.get("MeetingDate"))[:10])
            title = m.get("Name") or m.get("MeetingTypeName") or ""
            gov = is_governing(title, cx.card().get("name", "")) and not re.search(r"work ?session|board of control", title, re.I)
            fresh = cx.run_started() if when > date.today() - timedelta(days=45) else None
            r2 = cx.http.get(diligent.documents_url(portal, m["Id"]), fresh_since=fresh)
            views += 1
            if not r2.ok:
                notes.append(f"meeting {when} {title}: documents not read ({r2.error or r2.status})")
                continue
            documents, items, attachments = diligent.read_documents(r2.body, portal)
            if fresh is None and when < date.today() and not any(d["kind"] == "minutes" for d in documents):
                r3 = cx.http.get(diligent.documents_url(portal, m["Id"]), fresh_since=cx.run_started())  # posted since?
                views += 1
                if r3.ok:
                    r2, (documents, items, attachments) = r3, diligent.read_documents(r3.body, portal)
            for doc in documents:
                add(doc["url"], doc["title"], title, "", when, doc["kind"], gov, r2.url)
            for at in attachments:
                item = items[at["item"] - 1]["title"] if at.get("item") and at["item"] <= len(items) else ""
                add(at["url"], at["title"], f"{title} | item {at['item']}: {item}", "", when, "item", gov, r2.url)
    elif generic:
        if platform == "destinyhosted":
            m = re.search(r"[?&]id=(\d+)", url)
            if not m:
                raise Stop("a Destiny list without its id= in the address: check the list page's address")
            base = f"https://public.destinyhosted.com/agenda_publish.cfm?id={m.group(1)}&mt=ALL"
            queue, d = [], date(SINCE.year, SINCE.month, 1)
            while d <= date.today() + timedelta(days=31):
                queue.append(f"{base}&get_month={d.month}&get_year={d.year}")
                d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
            follow = False
        else:
            queue, follow = [url], True
        via, seen = d0.get("via") or "http", set()
        while queue:
            page_url = queue.pop(0)
            if page_url in seen:
                continue
            if views >= MAX_VIEWS:
                notes.append(f"stopped at {MAX_VIEWS} views of this list; {len(queue) + 1} not read (a runaway guard)")
                break
            seen.add(page_url)
            v = nid("V", page_url)
            if cx.st.get(cx.sid, v):  # another map in this session read this view already; its links are in the map
                info.append(f"view already read by an earlier map: {page_url}")
                continue
            views += 1
            why = None
            if via == "firecrawl":
                page, f = cx.scrape(page_url)
                cost += f.get("cost_usd") or 0.0
                why = None if page else (f.get("error") or f"status {f.get('status')}")
            else:
                for attempt in (1, 2):  # one retry after a pause: back off on errors
                    resp = cx.http.get(page_url, fresh_since=cx.run_started())
                    page = web.read_html(resp.text, resp.final_url or page_url) if resp.ok and "html" in (resp.content_type or "") else None
                    why = None if page else (resp.error or f"HTTP {resp.status} {resp.content_type}")
                    if page or resp.bot_wall or attempt == 2:
                        break
                    time.sleep(10)
            if not page:
                notes.append(f"view not read: {page_url} ({why})")
                continue
            cx.st.put(cx.sid, v, "view", "read", url=page_url, parent=g["node"], tool="map", data={"n_links": len(page.links)})
            for l in page.links:
                if not text.keep_for_documents(l.href, l.text, l.row):
                    continue
                when = dated(l, page_url)
                kind, reason = classify_link(l, when, page_url)
                if kind == "listing_page":
                    y = text.find_year(f"{l.text} {unquote(l.href)}")
                    if (follow and (y is None or y >= SINCE.year) and l.href not in seen and not is_file(l.href)
                            and web.site_of(l.href) == web.site_of(url)):
                        queue.append(l.href)
                    continue
                if when and when < SINCE:
                    out_of_window += 1
                    continue
                add(l.href, l.text, l.row, l.heading, when, kind, is_governing(l, cx.card().get("name", "")), page_url)

    meetings = {mk: m for r in done for mk, m in r["meetings"].items()}  # a portal's meetings, by the portal's own ids
    with cx.st.batch():
        for href, f in found.items():
            k = nid("K", href)
            if not cx.st.get(cx.sid, k):
                cx.st.put(cx.sid, k, "link", "listed", url=href, parent=g["node"], tool="map", data=f | {"in_window": bool(f["date"])})
            if f["date"] and not f.get("meeting_node"):
                mk = nid("M", f"{f['date']}|{f['rule_governing']}")
                meetings.setdefault(mk, {"date": f["date"], "governing": f["rule_governing"], "title": f["row"] or f["text"], "n_links": 0})
                meetings[mk]["n_links"] += 1
        for mk, m in meetings.items():
            cx.st.put(cx.sid, mk, "meeting", "listed", parent=g["node"], tool="map", data=m)
        for r in done:
            if r["view"]:
                cx.st.put(cx.sid, r["view"][0], "view", "read", parent=g["node"], tool="map", data=r["view"][1])
    if cx.jev_on:
        score_links(cx, [nid("K", h) for h in found])
    cx.st.put(cx.sid, g["node"], "page", "mapped", tool="map", cost=cost, budget="map" if cost else None,
              data={"views": views, "links_in_window": sum(1 for f in found.values() if f["date"]),
                    "links_undated": sum(1 for f in found.values() if not f["date"]), "out_of_window": out_of_window,
                    "meetings": len(meetings), "notes": notes, "info": info,
                    "portals_read": [[r["name"], r["client"], r["ok"]] for r in done], "portal_notes": portal_notes[:200]})
    for r in done:
        say(r["line"])
    say(f"MAPPED {g['node']}: " + (f"{views} views read, " if generic or platform == "diligent" else "")
        + f"{len(found)} links kept ({sum(1 for f in found.values() if not f['date'])} undated), "
        f"{out_of_window} dated before {SINCE} left out, {len(meetings)} meetings" + (f"; NOTES: {'; '.join(notes[:5])}" if notes else "")
        + (f"; ({len(info)} views already read by an earlier map, their links are in the map)" if info else ""))
    if portal_notes:
        say("PORTAL NOTES (the portal's own remarks; not gaps): " + "; ".join(short(x, 160) for x in portal_notes[:8])
            + (f"; … {len(portal_notes) - 8} more (status shows them)" if len(portal_notes) > 8 else ""))
    covered = [c for n in cx.st.nodes(cx.sid, "link", "read") for c in cover_meeting(cx, n)]  # meetings already read
    if covered:
        say(f"COVERED: {len(covered)} new link(s) of meetings whose minutes (or agenda) you already read")
    before = profile.value(cx, "gap_days")
    after = profile.cadence(cx, list(governing_meetings(cx.st.nodes(cx.sid, "link"))))
    if after is not None and after != before:
        say(f"PROFILE: gap guard {before} → {after} days, from this board's own meeting rhythm")
    show_links(cx, a)


def score_links(cx: Cx, ks: list[str]) -> None:
    """Jev on links (automatic in the Jev arms): could each record a buying decision, what will it open, whose is it."""
    todo = []
    for k in ks:
        n = cx.st.get(cx.sid, k)
        if n and n["status"] == "listed":
            d = n["data"]
            todo.append((k, {"body": cx.card(), "link": {"text": d.get("text"), "row": d.get("row"), "heading": d.get("heading"),
                                                         "url": n["url"], "date": d.get("date")}}))
    results = cx.jev_many(todo, LINK, "map")
    with cx.st.batch():
        for k, (answers, cost, err) in results.items():
            cx.st.put(cx.sid, k, "link", "scored", tool="map:jev", jev=(answers or {}) | ({"error": err} if err else {}),
                      cost=cost, budget="map")


def link_line(cx: Cx, n: dict) -> str:
    d = n["data"]
    gov = "gov" if (p_of(n, "governing") or 0) > 0.5 or (p_of(n, "governing") is None and d.get("rule_governing")) else "oth"
    kind = doc_kind(n) or "?"
    kp = (n.get("jev") or {}).get("kind_p")
    if cx.jev_on and kind == (n.get("jev") or {}).get("kind") and isinstance(kp, (int, float)) and kp < 0.6:
        kind += f" {kp:.2f}"  # Jev's guess, and an unsure one
    row = d.get("row") if d.get("row") and d.get("row") != d.get("text") else ""
    p = f"{fmt_p(p_of(n))}  " if cx.jev_on else ""
    return f"  {n['node']}  {d.get('date') or 'undated   '}  {kind:<12} {gov}  {p}{short(d.get('text'), 70)}" + (f"  | {short(row, 70)}" if row else "") + \
        (f"  [{n['status']}]" if n["status"] not in ("listed", "scored") else "")


def show_links(cx: Cx, a) -> None:
    links = cx.st.nodes(cx.sid, "link")
    status = getattr(a, "status", None) or "undecided"
    sel = [n for n in links if status == "all" or (n["status"] in UNDECIDED if status == "undecided" else n["status"] == status)]
    if getattr(a, "meeting", None):
        sel = [n for n in sel if n["data"].get("date") == a.meeting]
    if getattr(a, "kind", None):
        sel = [n for n in sel if doc_kind(n) == a.kind]
    if getattr(a, "min_p", None) is not None:
        sel = [n for n in sel if (p_of(n) or 0) >= a.min_p]
    if getattr(a, "max_p", None) is not None:
        sel = [n for n in sel if p_of(n) is not None and p_of(n) <= a.max_p]
    sel.sort(key=lambda n: (n["data"].get("date") or "", p_of(n) or 0), reverse=True)
    page = max(1, getattr(a, "page", 1) or 1)
    chunk = sel[(page - 1) * PER_PAGE: page * PER_PAGE]
    counts = {}
    for n in links:
        counts[n["status"]] = counts.get(n["status"], 0) + 1
    say(f"LINKS: {len(links)} in this session's map; by status {json.dumps(counts)}")
    if cx.jev_on:
        und = [n for n in links if n["status"] in UNDECIDED]
        say(f"  undecided by Jev's p(relevant): ≥0.8: {sum(1 for n in und if (p_of(n) or 0) >= 0.8)}, ≤0.2: "
            f"{sum(1 for n in und if p_of(n) is not None and p_of(n) <= 0.2)}, between: "
            f"{sum(1 for n in und if p_of(n) is not None and 0.2 < p_of(n) < 0.8)}, unknown: {sum(1 for n in und if p_of(n) is None)}")
    say(f"SHOWING {status} links {len(sel) and (page - 1) * PER_PAGE + 1}-{(page - 1) * PER_PAGE + len(chunk)} of {len(sel)} "
        f"(newest first; --page {page + 1} for more; filters --meeting YYYY-MM-DD --kind --min-p --max-p --status):")
    for n in chunk:
        say(link_line(cx, n))


def t_links(cx: Cx, a) -> None:
    cx.need_session()
    show_links(cx, a)


# ------------------------------------------------------------------------------------------- documents
def doc_format(resp: web.Response, url: str) -> str:
    """pdf, html, txt, doc, docx, rtf, or binary (a format the tools cannot read, said so rather than read as noise)."""
    b, ct = resp.body, (resp.content_type or "").lower()
    if b[:5] == b"%PDF-":
        return "pdf"
    if b[:4] == b"\xd0\xcf\x11\xe0":  # an old Word (or Excel) file
        return "doc" if not re.search(r"\.xls($|\?)", url, re.I) and "excel" not in ct else "binary"
    if b[:5] == b"{\\rtf":
        return "rtf"
    if b[:4] == b"PK\x03\x04":
        return "docx" if ("wordprocessingml" in ct or b"word/document.xml" in b) else "binary"
    if "text/plain" in ct or "text/csv" in ct:
        return "txt"
    if "html" in ct or "text/" in ct or b[:300].lstrip().lower().startswith((b"<!doctype", b"<html")):
        return "html"
    return "binary" if b"\x00" in b[:4000] else "html"


def portal_fetch(cx: Cx, d: dict):
    """What a link's document is fetched with: our polite GET, except a document of a portal that walls plain HTTP (the
    map recorded `transport: browser` on it: BoardDocs behind CloudFront, Simbli behind Imperva), which is fetched the
    way its adapter reads the portal: its `fetch_for(client)`, else its browser with the meeting's page opened first, as
    a visitor would. The browser stops at any challenge (raises Challenge) and never solves one."""
    if not d.get("portal") or d.get("transport") != "browser":
        return cx.http
    ad = cx.adapter(d["portal"], plain=False)
    fetch_for = getattr(ad, "fetch_for", None)
    if callable(fetch_for):
        return fetch_for(d["client"])
    if isinstance(getattr(ad, "http", None), Fallback):  # a plain-HTTP adapter whose host walled it: the browser, the
        return ad.http                                    # host's front page opened first
    if getattr(ad, "browser", None) is None:  # browser adapters keep theirs as .browser, made on first use
        from ..portals.browser import Browser
        ad.browser = Browser(clock=getattr(cx.http, "clock", None))
    opened = cx.__dict__.setdefault("_opened", set())
    if d.get("meeting_url") and d["meeting_url"] not in opened:
        opened.add(d["meeting_url"])
        try:
            ad.browser.open(d["meeting_url"])
        except Challenge:
            raise
        except PortalError:  # the meeting's page failed (not a wall): the file is asked for anyway
            pass
    return ad.browser


def portal_refresh(cx: Cx, k: dict) -> str | None:
    """A portal document's address now, when the one on the map no longer answers (a print service's PDF that expired, a
    file renamed): the adapter is asked again for the meeting's documents, and the one of the same kind and title kept."""
    d = k["data"]
    mn = cx.st.get(cx.sid, d["meeting_node"]) if d.get("meeting_node") else None
    if not (mn and (mn.get("data") or {}).get("meeting_id")):
        return None
    try:
        ad = cx.adapter(d["portal"], plain=d.get("transport") != "browser")
        docs = ad.documents(d["client"], meeting_of(mn["data"]))
        items = ad.items(d["client"], meeting_of(mn["data"])) if d.get("item_no") and d.get("rule_kind") == "item" else []
    except Exception:  # the map's address stands, with its own error
        return None
    for doc in list(docs) + [x for it in items for x in it.attachments]:
        if doc.kind == d.get("portal_kind") and short(doc.title or doc.kind.title(), 300) == d.get("text"):
            return doc.url
    return None


def download(cx: Cx, k: dict) -> tuple[str | None, str, str | None, web.Response]:
    """The document's bytes, saved under data/deep/<body>/ and recorded in the data manifest. (path, format, error, response)
    A portal's document comes through the portal's transport (portal_fetch); one whose address no longer answers is
    looked up again in the portal (portal_refresh)."""
    d = k.get("data") or {}
    try:
        fetch = portal_fetch(cx, d)
    except Exception as error:  # the browser could not start, or met a wall: said, not read
        msg = portal_failure(d.get("portal", "?"), d.get("client", "?"), error)
        return None, "", msg, web.Response(k["url"], error=msg, bot_wall=isinstance(error, Challenge))
    resp = fetch.get(web.direct_url(k["url"]))  # a shared Google document: its content, not its viewer page
    if not resp.ok and not resp.bot_wall and d.get("portal"):
        fresh = portal_refresh(cx, k)
        if fresh and fresh != k["url"]:
            resp = fetch.get(web.direct_url(fresh))
            if resp.ok:
                cx.st.put(cx.sid, k["node"], "link", k["status"], tool="download", data={"refreshed_url": fresh})
    if not resp.ok:
        return None, "", resp.error or f"HTTP {resp.status}", resp
    fmt = doc_format(resp, k["url"])
    when = k["data"].get("date") or "undated"
    kind = doc_kind(k) or "doc"
    rel, _ = web.save_download(cx.body, f"{when}-{kind}-{resp.sha256[:8]}.{'bin' if fmt == 'binary' else fmt}", resp.body)
    if fmt == "binary":
        return rel, fmt, f"a file the tools cannot read ({resp.content_type or 'unknown type'})", resp
    return rel, fmt, None, resp


def office_text(path: str) -> str | None:
    """Word and RTF files as text, by macOS's own textutil (nothing downloaded or installed)."""
    import shutil
    import subprocess
    if not shutil.which("textutil"):
        return None
    res = subprocess.run(["textutil", "-convert", "txt", "-stdout", str(paths.stored(path))], capture_output=True,
                         text=True, timeout=120)
    return res.stdout if res.returncode == 0 and res.stdout.strip() else None


def pdf_pages(path: str, pages: list[int] | None) -> tuple[list[tuple[int, str, bool]], int]:
    """Pages numbered from 1, as a reader and a PDF viewer count them (pdf-inspector counts from 0)."""
    import pdf_inspector
    full = str(paths.stored(path))
    total = pdf_inspector.classify_pdf(full).page_count
    res = pdf_inspector.extract_pages_markdown(full, pages=[p - 1 for p in pages] if pages else None)
    return [(p.page + 1, p.markdown or "", bool(p.needs_ocr)) for p in res.pages], total


def ocr_pages(path: str, pages: list[int]) -> dict[int, str]:
    """Text of image-only pages by macOS's own OCR (PDFKit renders, Vision reads; ocr.swift, compiled once into
    data/bin/). Nothing is downloaded. Returns {page counted from 1: text}; {} where OCR is not available."""
    import shutil
    import subprocess
    src = paths.REPO / "src" / "ddwb" / "deep" / "ocr.swift"
    binary = paths.DATA / "bin" / "ocr"
    if not shutil.which("swiftc") or not pages:
        return {}
    if not binary.exists() or binary.stat().st_mtime < src.stat().st_mtime:
        binary.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["swiftc", "-O", str(src), "-o", str(binary)], check=True, capture_output=True)
    res = subprocess.run([str(binary), str(paths.stored(path)), ",".join(map(str, pages))], capture_output=True, text=True,
                         timeout=60 + 30 * len(pages))
    return {int(k): v for k, v in json.loads(res.stdout or "{}").items()} if res.returncode == 0 else {}


def parse_pages(spec: str | None, total: int | None = None) -> list[int] | None:
    if not spec or spec == "all":
        return None
    out = []
    for part in spec.split(","):
        lo, _, hi = part.partition("-")
        out += list(range(int(lo), int(hi or lo) + 1))
    return [p for p in out if not total or p <= total]


def doc_pages(k: dict, path: str, fmt: str, resp: web.Response, want: list[int] | None):
    """A document's text as pages (page, text, image-only?), with its total pages, pages left without text, pages
    read by OCR, and, for a web page, the page itself (for its links)."""
    if fmt == "pdf":
        pages, total = pdf_pages(path, want)
        no_text = [p for p, md, o in pages if o or len(md.strip()) < 30]
        ocred = []
        if no_text:  # image-only pages: read by OCR (macOS), so every page is read
            texts = ocr_pages(path, no_text)
            pages = [(p, f"[OCR]\n{texts[p]}" if texts.get(p, "").strip() else md, o) for p, md, o in pages]
            ocred = [p for p in no_text if texts.get(p, "").strip()]
            no_text = [p for p in no_text if p not in ocred]
        return pages, total, no_text, ocred, None
    if fmt == "txt":
        return [(1, resp.text, False)], 1, [], [], None
    if fmt in ("doc", "docx", "rtf"):
        body = office_text(path)
        if body is None:
            raise Stop(f"could not read the {fmt} file at {k['url']}: mark it unreachable with the reason")
        return [(1, body, False)], 1, [], [], None
    page = web.read_html(resp.text, resp.final_url or k["url"])
    return [(1, page.text, False)], 1, [], [], page


def doc_link(cx: Cx, ref: str) -> dict:
    """The link to open: a link on the map, or a document seen elsewhere (on a page, in search results), which joins the
    map as a link off the mapped lists, dated from its title or address when they give a date."""
    n = cx.node(ref, ("link", "page"))
    if n["kind"] == "link":
        return n
    k = nid("K", n["url"])
    if not cx.st.get(cx.sid, k):
        d = n.get("data") or {}
        title = d.get("title") or ""
        blob = f"{title} {unquote(n['url'])}"
        when = text.find_date(blob)
        if when and when < SINCE:
            raise Stop(f"{n['node']} is dated {when}, before the window ({SINCE}): not opened")
        kind = next((k_ for k_, w in (("minutes", "minute"), ("agenda", "agenda"), ("packet", "packet")) if w in blob.lower()), "item")
        cx.st.put(cx.sid, k, "link", "listed", url=n["url"], parent=n["node"], tool="open",
                  data={"text": short(title, 300), "row": short(d.get("excerpt"), 300), "heading": "", "rule_kind": kind,
                        "date": when.isoformat() if when else None, "rule_governing": None, "in_window": bool(when),
                        "off_map": True, "from_page": n["node"]})
        if cx.jev_on:
            score_links(cx, [k])
    return cx.st.get(cx.sid, k)


def expand(cx: Cx, k: dict, page: web.Page, tool: str) -> list[str]:
    """A page's own document links (a meeting's items, an item's attachments such as an expenditure list) become
    links of the same meeting, scored by Jev in the Jev arms."""
    children = []
    for l in page.links:
        href = l.href.split("#")[0]
        if not text.keep_for_documents(l.href, l.text, l.row) or href == k["url"]:
            continue
        c = nid("K", href)
        if cx.st.get(cx.sid, c):
            continue
        kind, _ = classify_link(l, None, k["url"])
        if kind in ("listing_page", "meeting_page", "document", "other"):  # on a meeting's or an item's page: its parts
            kind = "item"
        cx.st.put(cx.sid, c, "link", "listed", url=href, parent=k["node"], tool=tool,
                  data={"text": short(l.text, 300), "row": short(l.row, 300), "heading": short(l.heading, 150),
                        "date": k["data"].get("date"), "rule_kind": kind, "rule_governing": k["data"].get("rule_governing"),
                        "in_window": bool(k["data"].get("date")), "from_page": k["node"]})
        children.append(c)
    if cx.jev_on and children:
        score_links(cx, children)
    return children


def date_document(k: dict, pages: list, page: web.Page | None, fmt: str, path: str) -> tuple[date | None, str | None]:
    """A date for a document its link left undated: from its title as listed, its file name, its own title (a web page),
    else the meeting its first page names. Events from an undated document can never be matched to a meeting.
    Returns (date, where it was found)."""
    d = k["data"]
    found = text.loose_date(f"{d.get('text') or ''} {d.get('row') or ''}")
    if found:
        return found, "title"
    found = text.loose_date(unquote(urlsplit(k["url"]).path.rsplit("/", 1)[-1]))
    if found:
        return found, "file name"
    found = text.loose_date(page.title) if page else None
    if found:
        return found, "page title"
    first = next((md for p, md, _ in pages if p == 1), None)
    if first is None and fmt == "pdf":
        first = next(iter(pdf_pages(path, [1])[0]), (1, "", False))[1]
    found = text.first_page_date(first)
    return (found, "first page") if found else (None, None)


def dated_now(cx: Cx, k: dict, pages: list, page: web.Page | None, fmt: str, path: str, tool: str) -> dict:
    """The date facts to keep on a link read undated (empty when it has a date or none is found). The link carries the
    date from here on, so the passages read from it and the document links it adds carry it too. A document dated
    before the window is not read."""
    if k["data"].get("date"):
        return {}
    found, source = date_document(k, pages, page, fmt, path)
    if not found:
        return {}
    facts = {"date": found.isoformat(), "date_from": source, "in_window": found >= SINCE}
    k["data"] |= facts
    if found < SINCE and tool == "read":
        cx.st.put(cx.sid, k["node"], "link", k["status"], tool=tool, data=facts | {"path": path, "content_type": fmt})
        raise Stop(f"{k['node']} is dated {found} by its {source}, before the window ({SINCE}): not read. Skip it with that reason")
    return facts


def show_children(cx: Cx, children: list[str]) -> None:
    if children:
        say(f"ITS DOCUMENT LINKS ({len(children)}, now in the map under the same date):")
        for c in children:
            say(link_line(cx, cx.st.get(cx.sid, c)))


def t_peek(cx: Cx, a) -> None:
    """A first look: page 1 (or the pages asked for) of a document, or a page's text and its own document links."""
    cx.need_session()
    k = doc_link(cx, a.target)
    path, fmt, err, resp = download(cx, k)
    if err:
        cx.st.put(cx.sid, k["node"], "link", "failed", tool="peek", data={"error": err})
        raise Stop(f"could not open {k['url']}: {err}. If it needs a browser or keeps failing, mark it: nav {cx.sid} unreachable {k['node']} --reason \"...\"")
    pages, total, no_text, ocred, page = doc_pages(k, path, fmt, resp, parse_pages(a.pages or "1") if fmt == "pdf" else None)
    body = "\n\n".join(f"[page {p}]\n{md}" for p, md, _ in pages) if fmt == "pdf" else pages[0][1]
    dating = dated_now(cx, k, pages, page, fmt, path, "peek")
    children = expand(cx, k, page, "peek") if page else []
    facts = {"path": path, "content_type": fmt, "bytes": len(resp.body), "looked": True, "links_added": len(children)} | dating
    if fmt == "pdf":
        facts |= {"pages_total": total, "peeked_pages": [p for p, *_ in pages]}
    cx.st.put(cx.sid, k["node"], "link", "peeked", tool="peek", data=facts)
    say(f"PEEK {k['node']} ({fmt}{', ' + str(total) + ' pages' if fmt == 'pdf' else ''}): {k['url']}"
        + (f"  [undated on the map; dated {dating['date']} by its {dating['date_from']}"
           + (f", before the window ({SINCE})" if not dating["in_window"] else "") + "]" if dating else ""), short(body, 3000))
    show_children(cx, children)
    if cx.jev_on:
        say(f"(Jev on whether to read it in full is yours to ask: nav {cx.sid} jev {k['node']} \"...\")")


def t_read(cx: Cx, a) -> None:
    """Read a document (or some of its pages): every item. In the Jev arms Jev labels every item and those at p ≤ 0.2 are
    set aside; in the others the plain filter keeps or drops each, counted. A web page's own document links join the map."""
    cx.need_session()
    k = doc_link(cx, a.target)
    first = None if getattr(a, "anyway", False) else minutes_first(cx, k)
    if first:
        raise Stop(f"read the minutes of this meeting first: nav {cx.sid} read {first} (they record what the board decided; "
                   f"an agenda, packet or item adds amounts and backup). To read this anyway, for an amount the minutes "
                   f"leave out: read {k['node']} --anyway")
    started = time.time()
    path, fmt, err, resp = download(cx, k)
    if err:
        cx.st.put(cx.sid, k["node"], "link", "failed", tool="read", data={"error": err})
        raise Stop(f"could not open {k['url']}: {err}. If it keeps failing, mark it: nav {cx.sid} unreachable {k['node']} --reason \"...\"")
    pages, total, no_text, ocred, page = doc_pages(k, path, fmt, resp, parse_pages(a.pages) if fmt == "pdf" else None)
    dating = dated_now(cx, k, pages, page, fmt, path, "read")
    children = expand(cx, k, page, "read") if page else []
    items, kept, dropped, procedural, context = [], [], 0, [], {}
    structured = structure_document(pages, k, path, fmt, profile.value(cx, "procedural_max"))
    if structured is not None:  # the structure layer: one item per agenda item, across page breaks, outcome attached
        for i, it in enumerate(structured, start=1):
            entry = {"text": it["text"], "item_no": it["number"], "section": it["section"], "part": None, "refs": it["refs"],
                     "outcome": it["outcome"], "people": it["people"], "pages": it["pages"]}
            items.append((it["pages"][0] if it["pages"] else 1, i, entry))
            if it["procedural"]:
                procedural.append((items[-1][0], i, entry, it["procedural"]))
    else:  # the line splitter, if the structure layer failed on this document
        for p, md, _ in pages:
            for i, it in enumerate(text.labelled_items(md, context=context), start=1):
                items.append((p, i, it))
    tagged = {(p, i) for p, i, _, _ in procedural}
    for p, i, it in items:
        if (p, i) in tagged:
            continue
        if cx.jev_on or a.all_items or text.could_be_buying(it["text"]):
            kept.append((p, i, it))
        else:
            dropped += 1
    when = k["data"].get("date")
    pnodes = []
    with cx.st.batch():
        for p, i, it in kept:
            pn = nid("P", f"{k['node']}:{p}:{i}")
            if not cx.st.get(cx.sid, pn):
                cx.st.put(cx.sid, pn, "passage", "kept", parent=k["node"], url=k["url"], tool="read",
                          data={"doc": k["node"], "page": p, "item": i, "date": when, "text": it["text"], "chars": len(it["text"]),
                                "amounts": text.amounts(it["text"])[:5]}
                          | {f: it[f] for f in ("item_no", "section", "part", "refs", "outcome", "people", "pages") if it.get(f)})
            pnodes.append(pn)
        for p, i, it, tag in procedural:  # kept, visible and counted, never sent to the buying question
            pn = nid("P", f"{k['node']}:{p}:{i}")
            if not cx.st.get(cx.sid, pn):
                cx.st.put(cx.sid, pn, "passage", "skipped", parent=k["node"], url=k["url"], tool="read",
                          reason=f"procedural ({tag}): set aside by the structure layer",
                          data={"doc": k["node"], "page": p, "item": i, "date": when, "text": it["text"], "chars": len(it["text"]),
                                "procedural": tag} | {f: it[f] for f in ("item_no", "section") if it.get(f)})
    cost, aside = 0.0, 0
    if cx.jev_on:
        cost, aside = label_passages(cx, pnodes, k)
    cx.st.put(cx.sid, k["node"], "link", "read", tool="read", ms=int((time.time() - started) * 1000),
              data={"path": path, "content_type": fmt, "pages_total": total, "looked": True,
                    "pages_read": "all" if not (fmt == "pdf" and a.pages) else a.pages, "pages_without_text": no_text,
                    "pages_read_by_ocr": ocred, "links_added": len(children),
                    "items_total": len(items), "items_kept": len(kept), "items_dropped_by_filter": dropped,
                    "items_set_aside_on_jev": aside,
                    "filter": "none: Jev labels every item" if cx.jev_on else "none (--all-items)" if a.all_items
                    else "a dollar amount, a buying word or a decision word"} | dating)
    if cx.jev_on:
        sifted = (f"{len(items)} items, every one labelled by Jev: {aside} set aside at p ≤ {profile.value(cx, 'set_aside_p')} (see them with "
                  f"`passages --doc {k['node']} --status skipped`; `record` works on them too), {len(kept) - aside} left for you")
    else:
        sifted = f"{len(items)} items, {len(kept)} kept, {dropped} dropped by the plain filter (--all-items keeps every item)"
    say(f"READ {k['node']} ({fmt}): {len(pages)} of {total} pages, {sifted}"
        + (f"; undated on the map, dated {dating['date']} by its {dating['date_from']}" if dating else "")
        + ("; no date on the map or in the document: `record` asks you for the meeting's date" if not k["data"].get("date") else "")
        + (f"; pages without a text layer: {no_text[:20]}" if no_text else "")
        + (f"; pages read by OCR: {ocred[:20]}" if ocred else "") + (f"; Jev ${cost:.4f}" if cost else ""))
    show_children(cx, children)
    mk = k["data"].get("meeting_node")
    if mk and k["data"].get("portal"):
        n_items = len(((cx.st.get(cx.sid, mk) or {}).get("data") or {}).get("items") or [])
        say(f"PORTAL ITEMS: this meeting's {str(n_items) + ' ' if n_items else ''}items as {k['data']['portal']} numbers them, "
            f"with each item's files (added to the map to open): nav {cx.sid} portal documents {mk}")
    covered = cover_meeting(cx, cx.st.get(cx.sid, k["node"]))
    if covered:
        say(f"COVERED: {len(covered)} other document(s) of this meeting ({', '.join(covered[:8])}) are covered by what you read: "
            f"its {'minutes record what the board decided' if is_minutes(k) else 'agenda lists what the board would consider'}. "
            f"To open one anyway, for an amount or backup it leaves out: read <K> --anyway")
    show_passages(cx, argparse.Namespace(doc=k["node"], page=1, min_p=None, status="kept"))


def structure_document(pages: list, k: dict, path: str, fmt: str, procedural_max: int | None = None) -> list[dict] | None:
    """The document as the structure layer's items (the same mapping the pipeline uses), or None if it failed."""
    try:
        from ..pipeline.items import structure_items  # the pipeline imports tools: imported here, at call time
        return structure_items(pages, kind=doc_kind(k), pdf_path=paths.stored(path) if fmt == "pdf" and path else None,
                               procedural_max=procedural_max)
    except Exception:  # never lose a read to the structure layer: the line splitter takes over
        return None


def label_passages(cx: Cx, pnodes: list[str], k: dict) -> tuple[float, int]:
    """Jev on each item read; those at or below the body's cut (profile set_aside_p, 0.2 by default) are set aside (a
    failure is unknown and stays for the agent)."""
    todo = []
    for pn in pnodes:
        n = cx.st.get(cx.sid, pn)
        if n["status"] == "kept" and not n.get("jev"):
            d = n["data"]
            doc = {"kind": doc_kind(k), "date": k["data"].get("date")}
            if not GENERIC_TITLE.match(k["data"].get("text") or ""):
                doc["title"] = k["data"].get("text")
            if k["data"].get("row") and k["data"].get("row") != k["data"].get("text"):
                doc["meeting"] = short(k["data"]["row"], 200)  # the map's row: the meeting's own title
            state = {"body": cx.card(), "document": doc, "passage": d["text"]}
            if d.get("item_no") or d.get("section") or d.get("outcome"):
                state["item"] = {f: d[f] for f in ("item_no", "section", "outcome") if d.get(f)}
            todo.append((pn, state))
    results, cost, aside = cx.jev_many(todo, BUYING_EVENT | PEOPLE, "reading"), 0.0, 0
    cut = profile.value(cx, "set_aside_p")  # this body's cut (decision #126)
    with cx.st.batch():
        for pn, (answers, c, err) in results.items():
            cost += c
            p = (answers or {}).get("is_event")
            low = isinstance(p, (int, float)) and p <= cut
            aside += low
            cx.st.put(cx.sid, pn, "passage", "skipped" if low else "kept", tool="read:jev", cost=c, budget="reading",
                      jev=(answers or {}) | ({"error": err} if err else {}), data={"skipped_on_jev": True} if low else None,
                      reason=f"set aside by the tools: Jev p={p:.2f} ≤ {cut}" if low else None)
    return cost, aside


def passage_line(cx: Cx, n: dict, chars: int = 500) -> str:
    j, d = n.get("jev") or {}, n["data"]
    sure = isinstance(j.get("type_p"), (int, float)) and j["type_p"] >= TYPE_SHOWN
    lab = f"event {fmt_p(j.get('is_event'))}{' ' + j['type'] if sure and j.get('type') else ''}  " if cx.jev_on else ""
    if cx.jev_on and isinstance(j.get("people"), (int, float)) and j["people"] > 0.5:
        lab += f"people {fmt_p(j['people'])} {j.get('people_change') or '?'}  "
    if d.get("item_no"):
        lab = f"item {d['item_no']}{' part ' + str(d['part']) if d.get('part') else ''}  " + lab
    st = f"[{n['status']}{' → ' + ', '.join(n['data']['events']) if n['status'] == 'recorded' and n['data'].get('events') else ''}] " \
        if n["status"] != "kept" else ""
    return f"  {n['node']}  p.{d.get('page')}  {lab}{st}{short(d.get('text'), chars)}"


def show_passages(cx: Cx, a) -> None:
    ps = cx.st.nodes(cx.sid, "passage")
    if getattr(a, "doc", None):
        ref = a.doc if re.fullmatch(r"K[0-9a-f]{7}", a.doc) else cx.node(a.doc, ("link",))["node"]
        ps = [n for n in ps if n["data"].get("doc") == ref]
    status = getattr(a, "status", None) or "kept"
    ps = [n for n in ps if status == "all" or n["status"] == status]
    if getattr(a, "min_p", None) is not None:
        ps = [n for n in ps if ((n.get("jev") or {}).get("is_event") or 0) >= a.min_p]
    if getattr(a, "max_p", None) is not None:
        ps = [n for n in ps if ((n.get("jev") or {}).get("is_event") or 0) <= a.max_p]
    if cx.jev_on:
        ps.sort(key=lambda n: -((n.get("jev") or {}).get("is_event") or 0))
    page = max(1, getattr(a, "page", 1) or 1)
    chunk = ps[(page - 1) * PASSAGES_PER_PAGE: page * PASSAGES_PER_PAGE]
    cx.st.mark_shown(cx.sid, [n["node"] for n in chunk])
    say(f"PASSAGES ({status}): {len(ps)}{', most likely events first' if cx.jev_on else ', in document order'}; showing "
        f"{len(chunk)} (--page {page + 1} for more; nav {cx.sid} passage <P> for the full text). Record events with `record`, "
        f"set others aside with `skip`:")
    for n in chunk:
        say(passage_line(cx, n))


def t_passages(cx: Cx, a) -> None:
    cx.need_session()
    show_passages(cx, a)


def t_passage(cx: Cx, a) -> None:
    cx.need_session()
    n = cx.node(a.target, ("passage",))
    cx.st.mark_shown(cx.sid, [n["node"]])
    doc = cx.st.get(cx.sid, n["data"].get("doc") or "") or {}
    say(f"PASSAGE {n['node']} ({n['data'].get('date')}, page {n['data'].get('page')} of {n['data'].get('doc')}"
        f"{', ' + doc['url'] if doc.get('url') else ''}) [{n['status']}]"
        + (f" ({n['reason']})" if n["status"] == "skipped" and n.get("reason") else ""), n["data"]["text"])
    events = [cx.st.get(cx.sid, e) for e in n["data"].get("events") or []]
    if events:
        say("EVENTS FROM IT: " + "; ".join(f"{e['node']} [{e['status']}] {e['data'].get('type')}/{e['data'].get('action')} "
                                           f"{short(e['data'].get('what'), 60)}" for e in events if e))
    if cx.jev_on:
        say("JEV: " + json.dumps(n.get("jev")))


def t_jev(cx: Cx, a) -> None:
    """Your own question to Jev about a link (with what peek showed), a passage or a page: yes or no, as a probability."""
    cx.need_session()
    if not cx.jev_on:
        raise Stop("Jev is off in this arm")
    n = cx.node(a.target)
    state = {"body": cx.card(), n["kind"]: {k: v for k, v in (n.get("data") or {}).items() if k in ("text", "row", "heading", "date", "title", "page")}}
    if n["kind"] == "link" and n["status"] == "peeked" and n["data"].get("path"):
        if n["data"].get("content_type") == "pdf":
            pages, _ = pdf_pages(n["data"]["path"], [1])
            state["first_page"] = short(pages[0][1] if pages else "", 4000)
        else:
            state["first_page"] = short(web.read_html(paths.stored(n["data"]["path"]).read_text(errors="replace"), n["url"]).text, 4000)
    key = n["node"]
    answers, cost, err = cx.jev_many([(key, state)], {"q": ("boolean", a.question)}, "reading")[key]
    asked = list((n.get("data") or {}).get("asked") or []) + [{"question": a.question, "p": (answers or {}).get("q"), "error": err}]
    cx.st.put(cx.sid, key, n["kind"], n["status"], tool="jev", data={"asked": asked}, cost=cost, budget="reading")
    say(f"JEV on {key}: {fmt_p((answers or {}).get('q'))}" + (f"  ERROR (unknown, not a no): {err}" if err else ""))


def t_record(cx: Cx, a) -> None:
    """A buying event, typed, from a passage you read. The tool checks the passage and the fields."""
    cx.need_session()
    p = cx.node(a.passage, ("passage",))
    buying = [t for t in EVENT_TYPES if t not in ("none", "staffing")]  # the query is about buying, not staffing
    if a.type not in buying:
        raise Stop(f"--type must be one of {', '.join(buying)}")
    if a.action not in ACTIONS:
        raise Stop(f"--action must be one of {', '.join(ACTIONS)}")
    amount = None
    if a.amount:
        try:
            amount = float(re.sub(r"[$,\s]", "", a.amount))
        except ValueError:
            raise Stop("--amount must be one number in dollars, the amount decided, e.g. 1459951.50 ($ and commas are fine); "
                       "put a second figure (a new total, an appropriation) in --what") from None
    when = record_date(cx, p, getattr(a, "date", None))
    quote = short(p["data"].get("text"), 300)
    n_events = len(cx.st.nodes(cx.sid, "event"))
    e = nid("E", f"{p['node']}:{n_events}")
    data = {"passage": p["node"], "doc": p["data"].get("doc"), "page": p["data"].get("page"), "date": when,
            "type": a.type, "action": a.action, "vendor": a.vendor, "amount": amount, "term": a.term, "what": a.what,
            "source_url": p["url"], "quote": quote} \
        | ({"item_no": p["data"]["item_no"]} if p["data"].get("item_no") else {}) \
        | ({"date_from": "the agent (the document has no date)"} if getattr(a, "date", None) else {})
    cx.st.put(cx.sid, e, "event", "recorded", parent=p["node"], url=p["url"], tool="record", data=data)
    cx.st.put(cx.sid, p["node"], "passage", "recorded", tool="record", data={"events": list(p["data"].get("events") or []) + [e]})
    say(f"RECORDED {e}: {a.type}/{a.action} {a.vendor or ''} {'$' + format(amount, ',.0f') if amount else ''} on {data['date']}")
    twins = same_item(cx, cx.st.get(cx.sid, p["node"]))
    label = p["data"].get("item_no") or ", ".join(p["data"].get("refs") or [])
    if twins:
        with cx.st.batch():
            for t in twins:
                cx.st.put(cx.sid, t, "passage", "same_item", tool="record:same-item", data={"same_item_as": p["node"]},
                          reason=f"the same item {label} of {when} in another document, recorded from {p['node']} as {e}")
        say(f"SAME ITEM: {len(twins)} passage(s) of item {label} in this meeting's other documents set aside as the "
            f"same decision ({', '.join(twins[:8])}); `unrecord` reopens them with the event")


def first_words(s: str | None) -> set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", (s or "").split("\n")[0].lower())}


def same_item(cx: Cx, p: dict) -> list[str]:
    """Undecided passages in the meeting's other documents (agenda, packet, minutes) that carry the same item number as
    this one and open with mostly the same words: one decision, so recording it once covers them."""
    no, when = p["data"].get("item_no"), p["data"].get("date")
    if not (no or p["data"].get("refs")) or not when:
        return []
    mine = first_words(p["data"].get("text"))
    out = []
    for n in cx.st.nodes(cx.sid, "passage", "kept"):
        d = n["data"]
        amounts_differ = d.get("amounts") and p["data"].get("amounts") and not set(d["amounts"]) & set(p["data"]["amounts"])
        if text.same_item(d, p["data"]) and d.get("date") == when and d.get("doc") != p["data"].get("doc") and not amounts_differ:
            theirs = first_words(d.get("text"))
            if mine and theirs and len(mine & theirs) / min(len(mine), len(theirs)) >= 0.5:
                out.append(n["node"])
    return out


def possibly_buying(n: dict, cut: float = SET_ASIDE) -> bool:
    """A passage the plain rule (an amount, a buying or a decision word) or Jev (p above the body's cut) marks as
    possibly a buying decision."""
    p = p_of(n, "is_event")
    return text.could_be_buying((n.get("data") or {}).get("text") or "") or (isinstance(p, (int, float)) and p > cut)


RULE_KNOWS = ("minutes", "agenda", "packet", "item", "video", "meeting_page", "listing_page")


def doc_kind(n: dict) -> str | None:
    """What a link opens: the map's own rule (chain.classify_link) where it names a kind, Jev's kind only where the rule
    cannot tell (a file it calls document or other). The Jev audit (2026-11-05, work/deep-discovery/jev-audit) found the
    rule right where Jev's kind sent agents astray (nav3: 86 Cortez agenda pages labelled minutes), and 60% of Jev's kind
    answers below 0.6."""
    rule = (n.get("data") or {}).get("rule_kind")
    kind = (n.get("jev") or {}).get("kind")
    return rule if rule in RULE_KNOWS or not isinstance(kind, str) else kind


def is_minutes(n: dict) -> bool:
    return doc_kind(n) == "minutes"


MEETING_TYPE = re.compile(r"\b(special|regular|emergency|annual|organi[sz]ational|reorgani[sz]ation|work ?session|study session|"
                          r"workshop|budget hearing|public hearing|joint|executive session)\b", re.I)


def meeting_types(n: dict) -> set[str]:
    """The kind of meeting a link names (regular, special, work session, ...): two on one date are two meetings."""
    d = n.get("data") or {}
    return {re.sub(r"\s+", " ", m.lower()) for m in MEETING_TYPE.findall(f"{d.get('text') or ''} {d.get('row') or ''}")}


COVERED_BY = {"minutes": ("agenda", "packet", "item", "meeting_page", "video"), "agenda": ("packet", "item", "video")}


def cover_meeting(cx: Cx, k: dict) -> list[str]:
    """After a meeting's minutes are read, its agenda, packet, item and video links need no reading: the minutes record
    what was decided (the reading plan, Shrey 2026-11-05: minutes first, a packet only for an amount). A meeting with no
    readable minutes is covered by its agenda the same way, except for its minutes. Only links of the same date and the
    same board (governing or not) are covered; each stays readable with --anyway. Returns the nodes covered."""
    when, kind = (k.get("data") or {}).get("date"), doc_kind(k)
    if k.get("status") != "read" or not when or kind not in COVERED_BY:
        return []
    same = [n for n in cx.st.nodes(cx.sid, "link") if (n.get("data") or {}).get("date") == when and governing(n) == governing(k)]
    if kind == "agenda" and any(is_minutes(n) and n["status"] != "unreachable" for n in same):
        return []  # the minutes are listed: they, not the agenda, cover the meeting
    mine = meeting_types(k)
    out = [n["node"] for n in same if n["status"] in UNDECIDED and doc_kind(n) in COVERED_BY[kind]
           and (not mine or not meeting_types(n) or meeting_types(n) == mine)]
    with cx.st.batch():
        for node in out:
            cx.st.put(cx.sid, node, "link", "skipped", tool="cover", data={"covered_by": k["node"]},
                      reason=f"covered by this meeting's {kind} {k['node']}, which was read")
    return out


def minutes_first(cx: Cx, k: dict) -> str | None:
    """The minutes link to read before this agenda, packet or item of the same meeting, if the map lists minutes for
    that date and none of them is read, skipped or unreachable yet. Reading a packet first is what buried agents in
    backup pages (PYLUSD, nav3): the minutes record the decisions, a packet the paper behind them."""
    when = (k.get("data") or {}).get("date")
    if is_minutes(k) or not when:
        return None
    same = [n for n in cx.st.nodes(cx.sid, "link") if is_minutes(n) and (n.get("data") or {}).get("date") == when]
    if not same or any(n["status"] in ("read", "skipped", "unreachable") for n in same):
        return None
    return same[0]["node"]


def minutes_read_dates(links: list[dict]) -> set[str]:
    return {n["data"]["date"] for n in links if is_minutes(n) and n["status"] == "read" and n["data"].get("date")}


def record_date(cx: Cx, p: dict, given: str | None) -> str | None:
    """The event's date: its document's. `--date` is taken only for a document with no date (none on the map, in its title,
    file name or first page), and then it is needed: an undated event can never be matched to a meeting."""
    doc = cx.st.get(cx.sid, p["data"]["doc"]) if p["data"].get("doc") else None
    has = p["data"].get("date") or ((doc or {}).get("data") or {}).get("date")
    if has:
        if given:
            raise Stop(f"the document is dated {has}: --date is only for a document with no date")
        return has
    if not given:
        raise Stop("this passage's document has no date (none on the map, in its title, file name or first page): give the "
                   "date of the meeting it records as the document states it, --date YYYY-MM-DD, or --date unknown")
    if given == "unknown":
        return None
    try:
        when = date.fromisoformat(given)
    except ValueError:
        raise Stop("--date must be YYYY-MM-DD (or unknown)") from None
    if when < SINCE:
        raise Stop(f"{when} is before the window ({SINCE}): not a decision the question asks about")
    return when.isoformat()


def t_skip(cx: Cx, a, status: str = "skipped") -> None:
    cx.need_session()
    if not a.reason or len(a.reason.strip()) < 3:
        raise Stop("a skip needs a reason (--reason \"...\")")
    targets = [cx.node(t) for t in a.targets]
    done = [n["node"] for n in targets if n["status"] in ("read", "recorded", "mapped", "answered")]
    if done:
        raise Stop(f"already read or recorded, so not {status}: {', '.join(done)}")
    if a.rest:
        rest = [n for n in cx.st.nodes(cx.sid, "link") if n["status"] in UNDECIDED]
        if a.meeting:
            rest = [n for n in rest if n["data"].get("date") == a.meeting]
        if a.max_p is not None:
            rest = [n for n in rest if p_of(n) is not None and p_of(n) <= a.max_p]
        targets += rest
    if not targets:
        raise Stop("nothing to skip: give node IDs, or --rest (with --meeting or --max-p to narrow it)")
    # A skip of one node is a decision on it. A skip of several decides only what the agent can have read: a passage that
    # may record buying counts as decided only if a tool printed it and the skip names no more passages than one page of
    # them (lists of IDs gathered by paging `passages` in a script and skipped together were how nav3's arms faked review);
    # unopened minutes of a governing meeting whose minutes nobody read are never decided in bulk. The rest is recorded as
    # skipped_unreviewed, and the answer stays incomplete until each is read and decided or skipped on its own.
    bulk = status == "skipped" and (a.rest or len(targets) > 1)
    too_many = sum(1 for n in targets if n["kind"] == "passage") > PASSAGES_PER_PAGE
    shown = cx.st.shown(cx.sid) if bulk else set()
    links = cx.st.nodes(cx.sid, "link") if bulk else []
    covered = minutes_read_dates(links)
    today = date.today().isoformat()
    unseen = on_jev = unreviewed = 0
    with cx.st.batch():
        for n in targets:
            new = {"page": "rejected" if status == "skipped" else status}.get(n["kind"], status)
            facts = {}
            if n["kind"] == "link" and status == "skipped" and not (n.get("data") or {}).get("looked"):
                # never opened: a skip on Jev's low score is one thing, a skip unseen another; coverage shows both
                low = p_of(n) is not None and p_of(n) <= SET_ASIDE
                facts = {"skipped_on_jev": low, "skipped_unseen": not low}
                on_jev, unseen = on_jev + low, unseen + (not low)
                when = n["data"].get("date")
                if bulk and is_minutes(n) and governing(n) and when and when <= today and when not in covered:
                    new = "skipped_unreviewed"
            elif n["kind"] == "passage" and bulk and (too_many or n["node"] not in shown) and \
                    possibly_buying(n, profile.value(cx, "set_aside_p")):
                new = "skipped_unreviewed"
            unreviewed += new == "skipped_unreviewed"
            cx.st.put(cx.sid, n["node"], n["kind"], new, reason=a.reason, tool=status if status != "skipped" else "skip", data=facts)
    say(f"{status.upper()}: {len(targets)} node(s), reason: {a.reason}"
        + (f"; {unseen} never opened (counted as skipped unseen in coverage)" if unseen else "")
        + (f"; {on_jev} on Jev's p ≤ 0.2" if on_jev else ""))
    if unreviewed:
        say(f"SKIPPED UNREVIEWED: {unreviewed} of them, skipped in bulk without being read: passages that may record buying "
            f"(never shown to you, or more than {PASSAGES_PER_PAGE} in one skip), or minutes of governing meetings never opened. "
            "The answer stays incomplete while any are left: `passages --status skipped_unreviewed` or `links --status "
            f"skipped_unreviewed` shows them; read and decide each (record it, read it, or skip at most {PASSAGES_PER_PAGE} "
            "passages at a time that a tool has shown you), or skip each on its own with its reason.")


def t_unrecord(cx: Cx, a) -> None:
    """Take back an event recorded by mistake (a duplicate, a wrong passage). Kept in the log as retracted."""
    cx.need_session()
    if not a.reason or len(a.reason.strip()) < 3:
        raise Stop("retracting an event needs a reason (--reason \"...\")")
    e = cx.node(a.event, ("event",))
    if e["status"] != "recorded":
        raise Stop(f"{e['node']} is {e['status']}, not a recorded event")
    cx.st.put(cx.sid, e["node"], "event", "retracted", reason=a.reason, tool="unrecord")
    p = cx.st.get(cx.sid, e["data"]["passage"])
    if p and not any((cx.st.get(cx.sid, x) or {}).get("status") == "recorded" for x in p["data"].get("events") or []):
        cx.st.put(cx.sid, p["node"], "passage", "kept", tool="unrecord")  # its only event is gone: the passage is open again
        for t in cx.st.nodes(cx.sid, "passage", "same_item"):  # and so are its twins in the meeting's other documents
            if t["data"].get("same_item_as") == p["node"]:
                cx.st.put(cx.sid, t["node"], "passage", "kept", tool="unrecord", data={"same_item_as": None})
    say(f"RETRACTED {e['node']} ({e['data'].get('type')}: {short(e['data'].get('what'), 60)}), reason: {a.reason}")


# ------------------------------------------------------------------------------------------- where things stand
def governing(n: dict) -> bool:
    """Whose meeting a link belongs to: Jev's answer in the Jev arms, else the plain rule."""
    g = (n.get("jev") or {}).get("governing")
    return g > 0.5 if isinstance(g, (int, float)) else bool((n.get("data") or {}).get("rule_governing"))


def governing_meetings(links: list[dict]) -> dict[str, list[dict]]:
    """The governing board's meetings dated up to today, each with its links."""
    by_date: dict[str, list] = {}
    for n in links:
        when = (n.get("data") or {}).get("date")
        if when and SINCE.isoformat() <= when <= date.today().isoformat() and governing(n):
            by_date.setdefault(when, []).append(n)
    return by_date


def meetings_unopened(links: list[dict]) -> list[tuple[str, dict]]:
    """The governing board's meetings (dated up to today) of which no document was opened, each with a link to start
    from (minutes first, then agenda, packet, item)."""
    order = {"minutes": 0, "agenda": 1, "packet": 2, "item": 3}
    out = []
    for when, ns in sorted(governing_meetings(links).items(), reverse=True):
        if not any((n.get("data") or {}).get("looked") for n in ns):
            out.append((when, min(ns, key=lambda n: order.get(doc_kind(n), 9))))
    return out


def minutes_unread(links: list[dict]) -> list[tuple[str, dict]]:
    """The governing board's meetings with a document opened whose minutes, listed on the map, nobody has read: the
    minutes say what the board decided, an agenda only what it would consider. A meeting with no minutes link (not yet
    posted, or not listed) is covered by any document opened; one whose minutes are unreachable is counted there."""
    out = []
    for when, ns in sorted(governing_meetings(links).items(), reverse=True):
        minutes = [n for n in ns if is_minutes(n) and n["status"] != "unreachable"]
        if minutes and any((n.get("data") or {}).get("looked") for n in ns) and not any(n["status"] == "read" for n in minutes):
            out.append((when, minutes[0]))
    return out


def map_gaps(links: list[dict], gap_days: float = GAP_DAYS) -> list[tuple[str, str, int]]:
    """Stretches of more than gap_days (the body's, else GAP_DAYS) between the window's start, the governing board's
    meetings on the map, and today:
    a board meets at least quarterly, so a long stretch means a view of the list (a year, an archive) or a list is
    missing. This is what sees a map that is too thin, which the unopened-meetings count cannot."""
    today = date.today().isoformat()
    dates = sorted(governing_meetings(links))
    points = [SINCE.isoformat(), *dates, today]
    return [(a, b, (date.fromisoformat(b) - date.fromisoformat(a)).days) for a, b in zip(points, points[1:])
            if (date.fromisoformat(b) - date.fromisoformat(a)).days > gap_days]


def coverage(cx: Cx) -> dict:
    links = cx.st.nodes(cx.sid, "link")
    ps = cx.st.nodes(cx.sid, "passage")
    pages = cx.st.nodes(cx.sid, "page")
    by = lambda ns: {s: sum(1 for n in ns if n["status"] == s) for s in sorted({n["status"] for n in ns})}  # noqa: E731
    reads = [n for n in links if n["status"] == "read"]
    unopened = meetings_unopened(links)
    mapped = [n for n in pages if n["status"] == "mapped"]
    return {
        "map_gaps": [f"{a} to {b} ({d} days)" for a, b, d in map_gaps(links, profile.value(cx, "gap_days"))] if mapped else [],
        "lists_checked_undecided": [n["node"] for n in pages if n["status"] == "checked"],
        "documents_off_map": sum(1 for n in links if n["data"].get("off_map") and n["data"].get("looked")),
        "passages_set_aside_on_jev": sum(1 for n in ps if n["status"] == "skipped" and n["data"].get("skipped_on_jev")),
        "governing_meetings": len(governing_meetings(links)),
        "governing_meetings_unopened": [w for w, _ in unopened],
        "governing_meetings_minutes_unread": [w for w, _ in minutes_unread(links)],
        "links_skipped_unseen": sum(1 for n in links if n["status"] == "skipped" and n["data"].get("skipped_unseen")),
        "links_skipped_on_jev": sum(1 for n in links if n["status"] == "skipped" and n["data"].get("skipped_on_jev")),
        "links_skipped_unreviewed": sum(1 for n in links if n["status"] == "skipped_unreviewed"),
        "links_covered_by_minutes_or_agenda": sum(1 for n in links if n["status"] == "skipped" and n["data"].get("covered_by")),
        "lists_accepted": [n["node"] for n in cx.st.nodes(cx.sid, "page", ("accepted", "mapped"))],
        "lists_mapped": [n["node"] for n in cx.st.nodes(cx.sid, "page", "mapped")],
        "meetings": len(cx.st.nodes(cx.sid, "meeting")),
        "links": len(links), "links_by_status": by(links), "links_undecided": sum(1 for n in links if n["status"] in UNDECIDED),
        "documents_read": len(reads),
        "pages_read": sum(len(parse_pages(n["data"]["pages_read"]) or []) if n["data"].get("pages_read") not in (None, "all")
                          else (n["data"].get("pages_total") or 0) for n in reads),
        "pages_without_text": sum(len(n["data"].get("pages_without_text") or []) for n in reads),
        "items_dropped_by_filter": sum(n["data"].get("items_dropped_by_filter") or 0 for n in reads),
        "passages": len(ps), "passages_by_status": by(ps), "passages_undecided": sum(1 for n in ps if n["status"] == "kept"),
        "passages_skipped_unreviewed": sum(1 for n in ps if n["status"] == "skipped_unreviewed"),
        "passages_same_item": sum(1 for n in ps if n["status"] == "same_item"),
        "events": len(cx.st.nodes(cx.sid, "event", "recorded")),
        "events_retracted": len(cx.st.nodes(cx.sid, "event", "retracted")),
        "unreachable": [n["node"] for n in links if n["status"] == "unreachable"],
        "map_notes": [x for n in mapped for x in (n["data"].get("notes") or [])
                      if not x.startswith("view already read by an earlier map")],  # runs before 2026-11-04 kept that note here
        "map_portal_notes": [x for n in mapped for x in (n["data"].get("portal_notes") or [])][:12],  # remarks, not gaps
        "session_cost_usd": round(cx.st.db.execute("SELECT coalesce(sum(cost_usd), 0) FROM events WHERE session = ?", [cx.sid]).fetchone()[0], 5),
    }


def t_status(cx: Cx, a) -> None:
    cx.need_session()
    say("COVERAGE: " + json.dumps(coverage(cx)), budget_line(cx))


def t_next(cx: Cx, a) -> None:
    cx.need_session()
    c = coverage(cx)
    say(budget_line(cx))
    if not c["lists_accepted"]:
        pages = cx.st.nodes(cx.sid, "page")
        checked = [n for n in pages if n["status"] == "checked"]
        found = [n for n in pages if n["status"] == "found" and not n["data"].get("is_file")]
        if cx.jev_on:  # most likely routes to the board's meetings first; unknown counts as likely
            found.sort(key=lambda n: -1 if p_of(n, "route") is None else -p_of(n, "route"))
        say("STAGE locate: no meetings list accepted yet.",
            f"  pages checked, not yet accepted or skipped: {', '.join(n['node'] for n in checked) or 'none'}",
            f"  pages found, not checked: {len(found)}; the first: " + ", ".join(f"{n['node']} {short(n['data'].get('title'), 40)}" for n in found[:8]),
            "  Reach the homepage (fetch), find candidate lists (its links; search), check them, accept the board's list(s).")
        return
    unmapped = [x for x in c["lists_accepted"] if x not in c["lists_mapped"]]
    if unmapped:
        say(f"STAGE map: accepted lists not mapped yet: {', '.join(unmapped)} → nav {cx.sid} map {unmapped[0]}")
        return
    say(f"MAP: {c['meetings']} meetings, {c['links']} links; by status {json.dumps(c['links_by_status'])}")
    if c["map_gaps"]:
        say(f"STAGE map gaps: the map has no governing-board meeting from {'; from '.join(c['map_gaps'][:4])}. Another view of "
            "the list (a year, an archive, a portal) or another list may hold them: find, check and map it. The answer is "
            "incomplete while a gap is left; if the board truly did not meet, say so in `answer --note`.")
    if c["lists_checked_undecided"]:
        say(f"NOTE: pages checked as meetings lists but neither accepted nor skipped: {', '.join(c['lists_checked_undecided'])} "
            "(accept one if it lists board meetings the map lacks; else skip it with a reason)")
    unopened = meetings_unopened(cx.st.nodes(cx.sid, "link"))
    if unopened:
        say(f"STAGE cover: {len(unopened)} of the governing board's {c['governing_meetings']} meetings have no document opened "
            f"yet; the answer is incomplete while any are left. Newest first, with a document to start from:")
        for when, n in unopened[:12]:
            say("  " + link_line(cx, n).strip())
        if len(unopened) > 12:
            say(f"  … and {len(unopened) - 12} more")
    unread = minutes_unread(cx.st.nodes(cx.sid, "link"))
    if unread:
        say(f"STAGE minutes: {len(unread)} of the governing board's meetings have a document opened but not their minutes, which "
            "the map lists: the minutes say what the board decided, an agenda only what it would consider. The answer is "
            "incomplete while any are left. Read each meeting's minutes (or mark them unreachable with the reason):")
        for when, n in unread[:12]:
            say("  " + link_line(cx, n).strip())
        if len(unread) > 12:
            say(f"  … and {len(unread) - 12} more")
    if c["passages_skipped_unreviewed"] or c["links_skipped_unreviewed"]:
        say(f"STAGE review: {c['passages_skipped_unreviewed']} passages that may record buying and {c['links_skipped_unreviewed']} "
            "minutes of governing meetings were skipped in bulk without being read (skipped_unreviewed); the answer is incomplete "
            "while any are left. `passages --status skipped_unreviewed` and `links --status skipped_unreviewed` show them. Read "
            f"and decide each (record it, read it, or skip at most {PASSAGES_PER_PAGE} passages at a time that a tool has shown "
            "you), or skip each on its own with its reason.")
    if c["links_skipped_unseen"]:
        say(f"NOTE: {c['links_skipped_unseen']} links were skipped without being opened (counted as such in the coverage).")
    if c["links_undecided"]:
        say(f"STAGE triage: {c['links_undecided']} links not yet read, skipped or marked unreachable. `links` to see them; peek or "
            "read what may hold buying decisions; skip the rest with reasons (several IDs at once, or --rest with --meeting / --max-p).")
    if c["passages_undecided"]:
        say(f"STAGE extract: {c['passages_undecided']} passages read but not recorded or skipped. `passages` to see them; "
            "record each buying event (record), skip the rest with a reason.")
    if (not c["links_undecided"] and not c["passages_undecided"] and not unopened and not unread and not c["map_gaps"]
            and not c["passages_skipped_unreviewed"] and not c["links_skipped_unreviewed"]):
        say("STAGE answer: every link and passage is decided, every governing meeting opened (its minutes read where the map "
            f"lists them), nothing skipped unreviewed, and the map has no gap. nav {cx.sid} answer")
    if c["map_notes"]:
        say("MAP NOTES: " + "; ".join(c["map_notes"][:5]))


def t_answer(cx: Cx, a) -> None:
    cx.need_session()
    c = coverage(cx)
    events = sorted(cx.st.nodes(cx.sid, "event", "recorded"), key=lambda n: n["data"].get("date") or "")
    incomplete = []
    if not c["lists_mapped"]:
        incomplete.append("no list of the board's meetings was mapped")
    if c["map_gaps"]:
        incomplete.append(f"the map has no governing-board meeting from {'; from '.join(c['map_gaps'][:3])}")
    if c["governing_meetings_unopened"]:
        incomplete.append(f"{len(c['governing_meetings_unopened'])} of {c['governing_meetings']} governing meetings never opened "
                          f"({', '.join(c['governing_meetings_unopened'][:6])}{', …' if len(c['governing_meetings_unopened']) > 6 else ''})")
    if c["governing_meetings_minutes_unread"]:
        m = c["governing_meetings_minutes_unread"]
        incomplete.append(f"{len(m)} of {c['governing_meetings']} governing meetings opened without reading the minutes the map "
                          f"lists ({', '.join(m[:6])}{', …' if len(m) > 6 else ''})")
    if c["links_undecided"]:
        incomplete.append(f"{c['links_undecided']} links undecided")
    if c["passages_undecided"]:
        incomplete.append(f"{c['passages_undecided']} passages undecided")
    if c["passages_skipped_unreviewed"] or c["links_skipped_unreviewed"]:
        incomplete.append(f"skipped in bulk without being read: {c['passages_skipped_unreviewed']} passages that may record "
                          f"buying, {c['links_skipped_unreviewed']} minutes of governing meetings (read and decide each, or skip "
                          "each on its own with its reason)")
    if cx.stopped():
        incomplete.append(f"paid calls stopped: {cx.stopped()}")
    if c["unreachable"]:
        incomplete.append(f"{len(c['unreachable'])} links unreachable")
    if c["map_notes"]:
        incomplete.append("map notes: " + "; ".join(c["map_notes"][:3]))
    answer = {"query": QUERY.format(name=(cx.sess["card"] or {}).get("name", cx.body).title(), since=SINCE.isoformat()),
              "as_of": date.today().isoformat(), "complete": not incomplete, "incomplete_because": incomplete, "note": a.note,
              "events": [{"id": e["node"], **{k: e["data"].get(k) for k in ("date", "type", "action", "vendor", "amount", "term",
                                                                            "what", "source_url", "page", "quote")}} for e in events],
              "coverage": c}
    cx.st.put(cx.sid, "A", "answer", "answered", tool="answer", data=answer)
    cx.st.set_session(cx.sid, status="answered")
    # to the warehouse: the events not copied by an earlier answer of this session
    exported = (cx.st.get(cx.sid, "B")["data"] or {}).get("exported_seq") or 0
    new = [ev for ev in cx.st.events(cx.sid) if ev["seq"] > exported]
    log = Log(cx.run, specs=(store.NAV_EVENTS,))
    for ev in new:
        log.add(store.NAV_EVENTS, {"session": cx.sid, "arm": cx.arm, "body_id": cx.sess["body_id"], "seq": ev["seq"], "at": ev["at"],
                                   "node": ev["node"], "kind": ev["kind"], "parent": ev["parent"], "url": ev["url"], "status": ev["status"],
                                   "data": ev["data"], "jev": ev["jev"], "reason": ev["reason"], "tool": ev["tool"],
                                   "cost_usd": ev["cost_usd"], "budget": ev["budget"], "ms": ev["ms"]})
    log.close()
    if new:
        cx.st.put(cx.sid, "B", "body", "answered", tool="answer", data={"exported_seq": new[-1]["seq"]})
    say(f"ANSWER ({'complete' if not incomplete else 'INCOMPLETE: ' + '; '.join(incomplete)}): {len(events)} events")
    for e in answer["events"]:
        say(f"  {e['date']}  {e['type']}/{e['action']}  {e['vendor'] or '-'}  {'$' + format(e['amount'], ',.0f') if e['amount'] else '-'}"
            f"  {short(e['what'], 80)}  ({e['source_url']} p.{e['page']})")
    say("COVERAGE: " + json.dumps(c))


# ------------------------------------------------------------------------------------------- portal tools
def portal_target(cx: Cx, ref: str) -> tuple[list[tuple[str, str]], str, str | None, str | None]:
    """The portal(s) a `portal` command names: <portal>:<client>, a URL or a page ID (by its address, else on its page,
    which is fetched). Returns ([(portal, client)], "address"/"page"/"name", the page node or None, the URL or None).
    A page that cannot be read is an error, never "no portal"."""
    ref, names = ref.strip(), portal_registry.names()
    is_id = re.fullmatch(r"[A-Z][0-9a-f]{7}", ref)
    if not is_id and not re.match(r"https?://", ref, re.I):
        name, _, client = ref.partition(":")
        if name in names and client.strip():
            return [(name, client.strip())], "name", None, None
        raise Stop(f"give a URL, a page ID or <portal>:<client> (e.g. legistar:wake); the portals: {', '.join(names)}")
    n = cx.node(ref, ("page",)) if is_id else None
    url = n["url"] if n else ref.split("#")[0]
    g = n["node"] if n else nid("G", url)
    if n and (n.get("data") or {}).get("portals"):
        return [tuple(x) for x in n["data"]["portals"]], n["data"].get("portals_by") or "page", g, url
    found, by = portals_of(cx, url)
    if not found:
        resp = cx.http.get(url, fresh_since=cx.run_started())
        if not resp.ok:
            raise Stop(f"{url}: {resp.error or f'HTTP {resp.status}'}"
                       + (" (a bot wall: stopped there, never solved)" if resp.bot_wall else "") + ": not read, so not known to be on a portal or not")
        if "html" in (resp.content_type or ""):
            found, by = portals_of(cx, resp.final_url or url, resp.text)
    old = cx.st.get(cx.sid, g)
    cx.st.put(cx.sid, g, "page", old["status"] if old else "found", url=url, tool="portal",
              data=({"portals": [list(x) for x in found], "portals_by": by} if found else {})
              | ({} if old else {"title": "", "source": "portal", "platform": found[0][0] if found else platform_of(url),
                                 "is_file": is_file(url)}))
    return found, by, g, url


def t_portal_detect(cx: Cx, a) -> None:
    cx.need_session()
    found, by, g, url = portal_target(cx, a.target)
    if not found:
        say(f"PORTAL DETECT {g} {url}: none of the {len(portal_registry.names())} known portals, by its address or on its "
            f"page ({', '.join(portal_registry.names())}). Read it as a page: fetch, check, accept, map")
        return
    say(f"PORTAL DETECT {g or ''} {url or ''}".rstrip() + f": by its {by}")
    for name, client in found:
        say(f"  {name}  client {client}")
    nxt = f"NEXT: nav {cx.sid} portal meetings {g or f'{found[0][0]}:{found[0][1]}'}"
    say(nxt + (f"   (or check, accept and map {g}: map lists every meeting's documents through the portal)" if g else ""))


def t_portal_meetings(cx: Cx, a) -> None:
    """A portal's meetings from SINCE to today + PORTAL_AHEAD, as meeting nodes (IDs for `portal documents`)."""
    cx.need_session()
    found, by, g, url = portal_target(cx, a.target)
    if not found:
        raise Stop(f"no known portal at {url}, by its address or on its page: `portal detect` lists the portals known")
    until, failed, tried = date.today() + timedelta(days=PORTAL_AHEAD), [], found[:PORTALS_PER_LIST]
    for name, client in tried:
        try:
            listing = cx.adapter(name).meetings(client, SINCE, until)
        except Exception as error:
            failed.append(portal_failure(name, client, error))
            say(failed[-1] + ": not an empty result; the portal could not be read")
            continue
        rows = []
        with cx.st.batch():
            for m in listing.meetings:
                governing = portal_governing(cx, m)
                mk = nid("M", f"{name}|{client}|{m.id}")
                old = cx.st.get(cx.sid, mk)
                cx.st.put(cx.sid, mk, "meeting", old["status"] if old else "listed", parent=g, tool="portal meetings",
                          data=meeting_facts(name, client, m, governing))
                rows.append((mk, m, governing))
        board = getattr(a, "board", None)
        if board == "":
            rows = [r for r in rows if r[2]]
        elif board:
            rows = [r for r in rows if board.lower() in f"{r[1].board} {r[1].title}".lower()]
        rows.sort(key=lambda r: r[1].date or date.min, reverse=True)
        page = max(1, getattr(a, "page", 1) or 1)
        chunk = rows[(page - 1) * PER_PAGE: page * PER_PAGE]
        years = ", ".join(f"{y}: {n}" for y, n in listing.per_year.items()) or "none"
        n_gov = sum(1 for m in listing.meetings if portal_governing(cx, m))
        say(f"PORTAL {name} ({client}, by its {by}): {len(listing.meetings)} meetings {SINCE} to {until} ({years}), {n_gov} of "
            f"the governing board" + (f"; showing {'the governing board' if board == '' else repr(board)}: {len(rows)}" if board is not None else "")
            + f"; newest first, {len(chunk)} shown" + (f" (--page {page + 1} for more)" if page * PER_PAGE < len(rows) else ""))
        if listing.notes:
            say("PORTAL NOTES (the portal's own remarks): " + "; ".join(short(x, 160) for x in listing.notes[:6]))
        for mk, m, governing in chunk:
            say(f"  {mk}  {m.date.isoformat() if m.date else 'undated   '}  {'gov' if governing else 'oth'}  {m.kind:<12} "
                f"{short(meeting_row(m), 90)}")
    if len(failed) == len(tried):
        raise Stop("the portal could not be read (above): not an empty result. Mark what depends on it unreachable with the "
                   "reason, or find the board's meetings another way")
    say(f"NEXT: nav {cx.sid} portal documents <M> lists one meeting's documents and items"
        + (f"; to map every meeting's documents, check, accept and map {g}" if g else ""))


def portal_meeting(cx: Cx, ref: str) -> dict:
    """A portal meeting node by its ID, or by its date when one portal meeting in this session is on it."""
    ref = ref.strip()
    if re.fullmatch(r"M[0-9a-f]{7}", ref):
        n = cx.node(ref, ("meeting",))
        if not (n.get("data") or {}).get("portal"):
            raise Stop(f"{ref} is a meeting of a plain list, not of a portal: its documents are on the map "
                       f"(`links --meeting {(n.get('data') or {}).get('date')}`)")
        return n
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", ref):
        ms = [n for n in cx.st.nodes(cx.sid, "meeting") if (n.get("data") or {}).get("portal") and n["data"].get("date") == ref]
        if len(ms) == 1:
            return ms[0]
        if not ms:
            raise Stop(f"no portal meeting on {ref} in this session: `portal meetings` or `map` lists them first")
        raise Stop(f"{len(ms)} portal meetings on {ref}; name one: "
                   + "; ".join(f"{n['node']} {short(n['data'].get('title'), 60)}" for n in ms))
    raise Stop("give a meeting ID (M..., from `portal meetings` or a map) or its date, YYYY-MM-DD")


def t_portal_documents(cx: Cx, a) -> None:
    """One portal meeting's documents and items (the portal's own numbers, titles, outcomes and files), into the map."""
    cx.need_session()
    mn = portal_meeting(cx, a.meeting)
    d = mn["data"]
    name, client, m = d["portal"], d["client"], meeting_of(d)
    ad = cx.adapter(name)
    try:
        docs = ad.documents(client, m)
    except Exception as error:
        raise Stop(portal_failure(name, client, error) + f": the documents of {d.get('date')} {short(d.get('title'), 60)} were not "
                   "read; not an empty result") from None
    items, items_error = [], None
    try:
        items = ad.items(client, m)
    except Exception as error:
        items_error = portal_failure(name, client, error)
    entries = portal_entries(name, client, m, mn["node"], d.get("governing"), docs, items, d.get("url"))
    transport, new = transport_of(ad, client), []
    with cx.st.batch():
        for href, f in entries.items():
            k = nid("K", href)
            if not cx.st.get(cx.sid, k):
                cx.st.put(cx.sid, k, "link", "listed", url=href, parent=mn["node"], tool="portal documents",
                          data=f | {"transport": transport_of(ad, client, href), "in_window": bool(f["date"])})
                new.append(k)
        cx.st.put(cx.sid, mn["node"], "meeting", mn["status"], tool="portal documents",
                  data={"n_links": len(entries), "documents_listed": True} | ({"items": item_facts(items)} if items else {})
                  | ({"items_error": items_error} if items_error else {}))
    if cx.jev_on and new:
        score_links(cx, new)
    say(f"PORTAL DOCUMENTS {mn['node']} {d.get('date')} {short(d.get('title'), 80)} ({name}, {client}): {len(docs)} documents, "
        f"{len(items)} items with {sum(len(it.attachments) for it in items)} files; {len(new)} new links in the map"
        + ("; read through a browser" if transport == "browser" else ""))
    if getattr(docs, "notes", None):
        say("PORTAL NOTES: " + "; ".join(short(x, 160) for x in docs.notes[:5]))
    hrefs = list(dict.fromkeys(doc.url.split("#")[0] for doc in docs if doc.url))
    for href in hrefs[:80]:
        say(link_line(cx, cx.st.get(cx.sid, nid("K", href))))
    if len(hrefs) > 80:
        say(f"  … and {len(hrefs) - 80} more documents (`links --meeting {d.get('date')}`)")
    if items:
        say("ITEMS (the portal's own numbers; open a file with peek or read):")
        for it, f in zip(items[:120], item_facts(items[:120])):
            if not (it.title or "").strip() and not f["files"]:
                continue  # an empty row of the portal's agenda (kept on the meeting node)
            say(f"  item {it.number or '?':<8} {short(it.title, 90)}" + (f"  [{short(it.outcome, 40)}]" if it.outcome else "")
                + (f"  files: {', '.join(x['id'] for x in f['files'][:6])}" + (" …" if len(f["files"]) > 6 else "") if f["files"] else ""))
        if len(items) > 120:
            say(f"  … and {len(items) - 120} more (kept on the meeting node)")
    if items_error:
        say(f"ITEMS NOT READ: {items_error} (not an empty agenda)")


def t_portal(cx: Cx, a) -> None:
    {"detect": t_portal_detect, "meetings": t_portal_meetings, "documents": t_portal_documents}[a.portal_tool](cx, a)


def t_profile(cx: Cx, a) -> None:
    """This body's tuned values (decision #126): show them with what moved them, or move one with its evidence."""
    cx.need_session()
    if a.action == "set":
        if not (a.knob and a.value is not None and a.evidence and a.reason):
            raise Stop("profile set <knob> <value> --evidence <passage, link or URL> --reason \"...\"")
        if a.knob not in profile.knobs():
            raise Stop(f"no knob {a.knob!r}: {', '.join(profile.knobs())}")
        if not (a.evidence.startswith(("http://", "https://")) or cx.st.get(cx.sid, a.evidence)):
            raise Stop(f"{a.evidence} is not a node of this session or a URL: the evidence must be something you read")
        before = profile.value(cx, a.knob)
        after = profile.move(cx, a.knob, a.value, source="agent", evidence=a.evidence, reason=a.reason)
        if after is None:
            raise Stop("this session runs fixed (a benchmark arm): its profile does not move")
        k = profile.knobs()[a.knob]
        say(f"PROFILE {a.knob}: {before} → {after}" + (f" (kept within {k['low']} to {k['high']})" if after != a.value else ""))
        return
    for name, k in profile.knobs().items():
        v = profile.value(cx, name)
        say(f"  {name:15} {v}{'' if v == k['default'] else ' (default ' + str(k['default']) + ')'}"
            f"  bounds {k['low']} to {k['high']}: {k['what']}")
    moves = profile.log(cx)
    say(f"PROFILE ({'fixed: the defaults' if not getattr(cx, 'profiled', True) else f'{len(moves)} entries'}); the last:")
    for m in moves[-8:]:
        say(f"  {m['at'][:16]} {m['knob']}: {m.get('from')} → {m.get('to')} by {m['source']}: {m['reason']} "
            f"({json.dumps(m.get('evidence'), default=str)[:140]})")


# ------------------------------------------------------------------------------------------- command line
TOOLS = {"start": t_start, "fetch": t_fetch, "scrape": t_scrape, "search": t_search, "check": t_check, "accept": t_accept,
         "map": t_map, "links": t_links, "peek": t_peek, "read": t_read, "passages": t_passages, "passage": t_passage,
         "jev": t_jev, "record": t_record, "unrecord": t_unrecord, "skip": t_skip, "unreachable": lambda cx, a: t_skip(cx, a, "unreachable"),
         "next": t_next, "status": t_status, "answer": t_answer, "portal": t_portal, "profile": t_profile}


STEP_HELP = {  # what each step does and when to call it: the MCP tools' descriptions (mcp_server) come from here
    "start": "Call first. Begins a session for one body: resolves it in the registry (--query \"Cortez, CO\" or "
             "--body-id), prints its homepage, the portals likely for its kind, the window, the spending caps and the next step.",
    "fetch": "Open a page (a URL or page ID) and list its links with dates and kinds; start from the body's homepage to "
             "find where its meetings are listed.",
    "scrape": "Like fetch, through a paid scraper (Firecrawl), for a page whose links only appear in a browser; costs money.",
    "search": "Search the web (paid: Parallel, or Exa with --domain for one site) when the homepage does not lead to "
              "the meetings; costs money.",
    "check": "Is this page (a URL or page ID) the governing board's meetings list? Counts its dated documents, "
             "recognises a portal, and says what to do next.",
    "accept": "Accept a checked page as the body's meetings list, with --reason; `map` then reads it.",
    "map": "Build the map from an accepted list or a portal: every governing-board meeting since 2025-01-01 with its "
           "documents (minutes, agenda, packet), gaps named.",
    "links": "List the map's links, filtered by --meeting, --kind, --status or Jev's p, to choose what to read.",
    "peek": "A first look at a document or page (page 1, or --pages) without reading it in full: its date, kind and links.",
    "read": "Read a document (minutes first) into items; prints the items kept for you to decide on, each a passage ID "
            "to `record` or `skip`.",
    "passages": "List the passages read so far (by --doc, --status, Jev's p), to find what is still undecided.",
    "passage": "Show one passage's full text (a passage ID from read or passages), to check an amount or vendor before recording.",
    "jev": "Ask Jev (the cheap judge, when connected) your own yes-or-no question about a link, passage or page.",
    "record": "Record a decision found in a passage: --type, --action, --what, and the vendor, amount and term when "
              "stated. The tool checks the fields against the passage.",
    "unrecord": "Take back an event recorded by mistake, with --reason (kept in the log as retracted).",
    "skip": "Set links or passages aside with --reason (not a decision, another board, outside the window, ...); "
            "--rest for every undecided link.",
    "unreachable": "Mark links that cannot be read (failed, walled, gone) with --reason; they are named as gaps in the answer.",
    "next": "What to do next: the next meeting to open or passage to decide, and the spend so far.",
    "status": "The session's state: map, coverage, events recorded, spend against the caps.",
    "answer": "Call last. The answer: every decision with its source and quote, the coverage, and what is incomplete and why.",
    "portal": "A meeting portal through its adapter (Legistar, BoardDocs, IQM2, eScribe, ...): detect, meetings, documents.",
    "profile": "This body's tuned values (Jev's cut, the check threshold, the procedural limit, the gap guard) and what "
               "moved them; `profile set <knob> <value> --evidence <passage> --reason` moves one, within its bounds.",
}


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nav", description="Navigation tools (skills/deep-navigator/SKILL.md)")
    p.add_argument("session")
    sub = p.add_subparsers(dest="tool", required=True)
    s = sub.add_parser("start", help=STEP_HELP["start"])
    s.add_argument("--cap-map", type=float, help=f"USD for the map (default DDWB_CAP_MAP, else {DEFAULT_CAPS['map']})")
    s.add_argument("--cap-reading", type=float, help=f"USD for reading (default DDWB_CAP_READING, else {DEFAULT_CAPS['reading']})")
    s.add_argument("--approved-by", help="who approved the run (free text, recorded; default DDWB_APPROVED_BY)")
    s.add_argument("--query", help="the body in words, e.g. \"Cortez, CO\" (not needed for the benchmark bodies)")
    s.add_argument("--body-id", help="the registry body_id, when the query is ambiguous")
    for name in ("fetch", "scrape"):
        s = sub.add_parser(name, help=STEP_HELP[name])
        s.add_argument("target")
        s.add_argument("--all-links", action="store_true")
    s = sub.add_parser("search", help=STEP_HELP["search"])
    s.add_argument("query")
    s.add_argument("--engine", choices=["parallel", "exa"])
    s.add_argument("--domain")
    s = sub.add_parser("check", help=STEP_HELP["check"])
    s.add_argument("target")
    s.add_argument("--via", choices=["http", "firecrawl"])
    s = sub.add_parser("accept", help=STEP_HELP["accept"])
    s.add_argument("target")
    s.add_argument("--reason", required=True)
    for name in ("map", "links"):
        s = sub.add_parser(name, help=STEP_HELP[name])
        if name == "map":
            s.add_argument("target")
            s.add_argument("--rebuild", action="store_true")
            s.add_argument("--plain", action="store_true", help="read the list as a plain page, even where a portal is recognised")
        s.add_argument("--page", type=int, default=1)
        s.add_argument("--meeting")
        s.add_argument("--kind")
        s.add_argument("--min-p", type=float)
        s.add_argument("--max-p", type=float)
        s.add_argument("--status", default="undecided", help="undecided (default), all, or one status")
    s = sub.add_parser("peek", help=STEP_HELP["peek"])
    s.add_argument("target")
    s.add_argument("--pages", help="e.g. 1-3 (default 1)")
    s = sub.add_parser("read", help=STEP_HELP["read"])
    s.add_argument("target")
    s.add_argument("--pages", help="e.g. 4-19 or 2,5,7 (default: all)")
    s.add_argument("--all-items", action="store_true", help="keep every item, not only those with an amount or a buying word")
    s.add_argument("--anyway", action="store_true", help="read an agenda, packet or item before its meeting's minutes")
    s = sub.add_parser("passages", help=STEP_HELP["passages"])
    s.add_argument("--doc")
    s.add_argument("--page", type=int, default=1)
    s.add_argument("--min-p", type=float)
    s.add_argument("--max-p", type=float, help="with --status skipped and --min-p: the band just under this body's cut")
    s.add_argument("--status", default="kept")
    s = sub.add_parser("passage", help=STEP_HELP["passage"])
    s.add_argument("target")
    s = sub.add_parser("jev", help=STEP_HELP["jev"])
    s.add_argument("target")
    s.add_argument("question")
    s = sub.add_parser("record", help=STEP_HELP["record"])
    s.add_argument("passage")
    s.add_argument("--type", required=True, help="one of: " + ", ".join(t for t in EVENT_TYPES if t not in ("none", "staffing"))
                   + " (a decision that is none of these, e.g. a land-use permit, is not recorded: skip it with the reason)")
    s.add_argument("--action", required=True, help="one of: " + ", ".join(ACTIONS))
    s.add_argument("--vendor", help="who is paid or contracted, as the record names them")
    s.add_argument("--amount", help="one number in dollars, the amount decided (e.g. 1459951.50; $ and commas are fine); "
                                    "a second figure (an appropriation, a new total) goes in --what")
    s.add_argument("--term")
    s.add_argument("--what", required=True, help="what was bought or decided, in a few words")
    s.add_argument("--date", help="YYYY-MM-DD (or unknown): the meeting's date, only for a document with no date")
    s = sub.add_parser("unrecord", help=STEP_HELP["unrecord"])
    s.add_argument("event")
    s.add_argument("--reason", required=True)
    for name in ("skip", "unreachable"):
        s = sub.add_parser(name, help=STEP_HELP[name])
        s.add_argument("targets", nargs="*")
        s.add_argument("--reason", required=True)
        s.add_argument("--rest", action="store_true", help="every undecided link (narrow with --meeting, --max-p)")
        s.add_argument("--meeting")
        s.add_argument("--max-p", type=float)
    sub.add_parser("next", help=STEP_HELP["next"])
    sub.add_parser("status", help=STEP_HELP["status"])
    s = sub.add_parser("profile", help=STEP_HELP["profile"])
    s.add_argument("action", nargs="?", choices=["show", "set"], default="show")
    s.add_argument("knob", nargs="?")
    s.add_argument("value", nargs="?", type=float)
    s.add_argument("--evidence", help="the passage or link ID, or the URL, that shows it")
    s.add_argument("--reason")
    s = sub.add_parser("answer", help=STEP_HELP["answer"])
    s.add_argument("--note")
    s = sub.add_parser("portal", help=STEP_HELP["portal"])
    portal = s.add_subparsers(dest="portal_tool", required=True)
    s = portal.add_parser("detect", help="which known portal a URL or page is on, or links or embeds")
    s.add_argument("target", help="a URL or a page ID")
    s = portal.add_parser("meetings", help="the portal's meetings from 2025-01-01 to 60 days ahead, with IDs")
    s.add_argument("target", help="a URL, a page ID, or <portal>:<client> (e.g. legistar:wake)")
    s.add_argument("--board", nargs="?", const="", default=None,
                   help="alone: only the governing board's meetings; with words: meetings whose board or title has them")
    s.add_argument("--page", type=int, default=1)
    s = portal.add_parser("documents", help="one meeting's documents and items, into the map")
    s.add_argument("meeting", help="a meeting ID (M...) from `portal meetings` or a map, or its date YYYY-MM-DD")
    return p


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    cx = None
    try:
        cx = Cx(a.session)
        TOOLS[a.tool](cx, a)
        return 0
    except Stop as why:
        say(f"REFUSED: {why}")
        return 2
    except BrokenPipeError:  # the reader stopped early (e.g. | head): the work was recorded
        import os
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    finally:
        if cx is not None:
            cx.close()  # a browser an adapter opened


if __name__ == "__main__":
    sys.exit(main())
