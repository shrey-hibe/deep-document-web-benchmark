"""The deep chain, Phase 1: plain plumbing, no Jev. From a body's homepage to its board's meetings, documents, buying
events and three standard answers, every step logged (ddwb.deep.store) and scored against what we already know.

    reach    the registry's homepage, our own GET
    locate   candidates from homepage links, an open search (Parallel) and a search restricted to the body's site (Exa);
             a plain score picks them; Firecrawl (the scrape provider) reads the top ones to see which list dated meeting documents
    route    a known portal with an adapter (Diligent) goes to the adapter; anything else stays on Firecrawl
    list     meetings since 2024 from each list: links typed by plain rules (text, address, row), dates from the
             link, its address or its row; earlier years and 2026 meeting pages are followed
    fetch    2026 agendas, packets and minutes: PDFs by our own GET, read locally (pdf-inspector)
    items    passages with a dollar amount or a buying word; amount, vendor, term, type and action by rule; the same
             event in one meeting's agenda, packet and minutes is merged
    answer   Q1 what was bought, Q2 what is open, Q3 what is next (state over time comes later, from the event log)

Run (paid calls through the provider layer: search pinned to Parallel and Exa, scrape on the configured provider):
    uv run python -m ddwb.deep.chain run [--bodies summit-county-oh,cortez-co] [--no-paid] [--budget USD] [--approved-by "..."]
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlparse, unquote

from .. import paths, tables
from ..evals import log as eval_log
from ..evals.run import Runner, body_card, names_body
from ..providers import EXA, PARALLEL, PriceDrift
from ..runlog import now
from . import diligent, store, text, web

LIST_SINCE, DOCS_SINCE = date(2024, 1, 1), date(2026, 1, 1)
DISCOVERY = paths.DISCOVERY
BODIES = {
    "summit-county-oh": {"body_id": "bf62a1a7cef59", "discovery": "counties/summit-county-oh.json"},
    "cortez-co": {"body_id": "b2378a7debda8", "discovery": "cities/cortez-co.json"},
    "pueblo-library-co": {"body_id": "bf8108811f00b", "discovery": "special-districts/pueblo-library-co.json"},
    "pylusd-ca": {"body_id": "be594dc0a1aa2", "hand_map": {"date": "2026-04-14", "items": 49, "attachments": 98,
                                                           "note": "Session 1's hand map of the 14 April 2026 meeting"}},
}
MAX_CHECKED, MAX_SOURCES, MAX_SCRAPES, MAX_DOCS = 6, 3, 30, 60
DOC_ORDER = {"minutes": 0, "agenda": 1, "packet": 2, "document": 3, "attachment": 4}

KIND_WORD = {"county": "county", "municipality": "city", "special_district": "district", "school_district": "school district",
             "library_system": "library"}
BOARD_WORD = {"county": "council commissioners", "municipality": "city council", "special_district": "board of directors trustees",
              "school_district": "board of education trustees", "library_system": "board of trustees"}


# ------------------------------------------------------------------------------------------- plain rules
def platform_of(url: str) -> str | None:
    if diligent.is_portal(url):
        return "diligent"
    m = text.PLATFORMS.search(url or "")
    return m.group(0).lower().split(".")[0] if m else None


def score_candidate(url: str, title: str, home_site: str | None) -> tuple[int, dict]:
    """The plain locate rule: a known portal, 'agenda'/'minutes', meeting words, the body's own site."""
    blob, reasons = f"{title} {url}".lower(), {}
    platform = platform_of(url)
    if platform:
        reasons["platform"] = 3
    if re.search(r"agenda|minute", blob):
        reasons["agenda_or_minutes"] = 2
    if re.search(r"meeting|council|commission|board|trustee|packet", blob):
        reasons["meeting_words"] = 1
    if home_site and web.site_of(url) == home_site:
        reasons["own_site"] = 1
    elif not platform:
        reasons["other_site"] = -2
    if is_file(url):
        reasons["a_file_not_a_list"] = -3
    if re.search(r"committee|planning|zoning|advisory|livestream|live-stream|video", blob):
        reasons["committee_or_video"] = -1
    return sum(reasons.values()), reasons


GOVERNING_NOT = re.compile(r"committee|planning|zoning|p ?& ?z\b|advisory|historic|parks|design review|board of adjustment|"
                           r"work ?shop|foundation|retirement|pension|civil service|library board|authority", re.I)


def is_governing(link: web.Link | str, body: str = "") -> bool:
    """Plain rule: the governing board unless the link, its row or its heading names a committee or another board.
    A word that is in the body's own name ("library" for a library district, "authority" for a transit authority) does
    not make it another board."""
    blob = link if isinstance(link, str) else f"{link.text} {link.row} {link.heading}"
    own = set(re.findall(r"[a-z]+", (body or "").lower()))
    return not any(not (set(re.findall(r"[a-z]+", m.group(0).lower())) & own) for m in GOVERNING_NOT.finditer(blob))


def is_file(url: str) -> bool:
    """A document, not a page that lists documents: .pdf, CMS file stores, download handlers."""
    return bool(PDFISH.search(url) or re.search(r"/sites/default/files/|/media/\d+|sendfile|/files/|"
                                                r"\.(?:docx?|xlsx?|pptx?|csv|zip|mp[34]|wav|jpe?g|png)($|\?)", url, re.I))


PDFISH = re.compile(r"\.pdf($|\?)|viewfile|download|showdocument|getfile|/document/|blobdload|displayagendapdf", re.I)


def classify_link(link: web.Link, found: date | None, page_url: str = "") -> tuple[str, str]:
    """Plain rule for what a link on a meetings list is. Returns (kind, the rule that decided)."""
    u = urlparse(link.href)
    t = f"{link.text} {unquote(u.path.rsplit('/', 1)[-1])} {unquote(u.query)}".lower()
    same_site = not page_url or web.site_of(link.href) == web.site_of(page_url) or bool(platform_of(link.href))
    named = None
    if re.search(r"minute", t):
        named = "minutes"
    elif re.search(r"packet|board ?book|materials|full agenda|supporting doc", t):
        named = "packet"
    elif re.search(r"agenda", t):
        named = "agenda"
    if named:
        if not found and not is_file(link.href) and same_site:
            return "listing_page", f"undated '{named}' page: a list of them"
        return named, f"says {named}"
    if re.search(r"video|youtube|livestream|watch|recording|audio|vimeo", f"{t} {link.href.lower()}"):
        return "video", "says video"
    if PDFISH.search(link.href):
        return ("document", "a file, dated") if found else ("other", "a file, undated")
    if found and same_site:
        return "meeting_page", "a dated page on the site"
    if same_site and page_url and link.href.split("?")[0] == page_url.split("?")[0] and link.href != page_url:
        return "listing_page", "the same list, another view"
    blob = f"{link.text} {unquote(link.href)}".lower()
    if same_site and (re.fullmatch(r"\s*20[1-4]\d\s*", link.text or "") or re.search(r"[?&/_-](year|dir|yr)=?20[1-4]\d", blob)
                      or (re.search(r"archive|previous|older|\b20[1-4]\d\b", blob) and re.search(r"meeting|agenda|minute|board|council|packet", blob))):
        return "listing_page", "a year or an archive of meetings"
    return "other", "no rule"


EVENT_RULES = (("renewal", r"renew|extension|extend|amend"),
               ("bid", r"\bbids?\b|\brfp\b|\brfq\b|request for (?:proposals|qualifications)|invitation to bid|solicit"),
               ("grant", r"\bgrants?\b|funding from|award(?:ed)? (?:by|from)"),
               ("contract", r"contract|agreement|professional services"),
               ("purchase", r"purchase|procure|invoice|payment|\bclaims?\b|quote|lease"),
               ("budget", r"budget|appropriat|supplemental|transfer of funds"),
               ("staffing", r"\bhire|hiring|appoint|resign|retire|employment|personnel|position"))


def event_type(passage: str) -> str:
    for name, pattern in EVENT_RULES:
        if re.search(pattern, passage, re.I):
            return name
    return "none"


def action(passage: str, doc_kind: str) -> str:
    if doc_kind == "minutes":
        if re.search(r"tabled|denied|failed|postponed|withdrawn", passage, re.I):
            return "rejected_or_tabled"
        if re.search(r"approv|adopt|carried|passed|unanimous|motion", passage, re.I):
            return "approved"
        return "unclear"
    return "to_consider" if doc_kind in ("agenda", "packet") else "unclear"


# ------------------------------------------------------------------------------------------- one body
@dataclass
class Doc:
    url: str
    kind: str
    meeting_date: date | None
    title: str
    listing_url: str
    governing: bool
    text: str | None = None  # already in hand (a scraped HTML agenda, a Diligent agenda)


@dataclass
class BodyRun:
    short: str
    spec: dict
    r: Runner
    http: web.Http
    paid: bool
    seq: int = 0
    scraped: dict = field(default_factory=dict)  # url -> Page
    docs: dict = field(default_factory=dict)  # url -> Doc
    meetings: dict = field(default_factory=dict)  # (date, governing) -> {title, source_url, platform, n}
    items: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    scrapes: int = 0
    portal_counts: dict = field(default_factory=dict)  # meeting date -> (agenda items, attachments), from an adapter

    @property
    def body_id(self) -> str:
        return self.spec["body_id"]

    def step(self, stage, action, url=None, outcome=None, detail=None, cost=0.0, elapsed_ms=None):
        self.seq += 1
        self.r.log.add(store.STEPS, {"body_id": self.body_id, "seq": self.seq, "stage": stage, "action": action, "url": url,
                                     "outcome": outcome, "detail": detail, "cost_usd": cost, "elapsed_ms": elapsed_ms, "at": now()})

    def fetch_log(self, stage, via, resp: web.Response | None = None, facts: dict | None = None, path=None):
        row = {"body_id": self.body_id, "stage": stage, "via": via, "fetched_at": now(), "path": path}
        if resp is not None:
            row |= {"url": resp.url, "final_url": resp.final_url, "status": resp.status, "content_type": resp.content_type,
                    "bytes": len(resp.body), "sha256": resp.sha256, "error": resp.error, "elapsed_ms": resp.elapsed_ms,
                    "from_cache": resp.from_cache, "bot_wall": resp.bot_wall, "cost_usd": 0.0}
        if facts is not None:
            row |= {k: facts.get(k) for k in ("url", "final_url", "status", "content_type", "bytes", "sha256", "error",
                                              "elapsed_ms", "bot_wall", "proxy", "cost_usd")} | {"from_cache": bool(facts.get("cache_hit"))}
        self.r.log.add(store.FETCHES, row)

    def can_pay(self) -> bool:
        return self.paid and self.r.spent < self.r.budget and not self.r.stopped

    def charge(self, cost):
        with self.r.log.lock:
            self.r.spent += cost or 0.0
            self.r.calls += 1

    # ----------------------------------------------------------------------------------- reach
    def reach(self, card: dict, website: str | None) -> web.Page | None:
        if not website:
            self.step("reach", "no website in the registry", outcome="none")
            return None
        url = website if website.startswith("http") else "http://" + website
        resp = self.http.get(url)
        self.fetch_log("reach", "http", resp)
        ok = resp.ok and "html" in (resp.content_type or "")
        self.step("reach", "GET homepage", url, "ok" if ok else (resp.error or f"HTTP {resp.status}"),
                  {"final_url": resp.final_url, "bot_wall": resp.bot_wall}, elapsed_ms=resp.elapsed_ms)
        return web.read_html(resp.text, resp.final_url or url) if ok else None

    # ----------------------------------------------------------------------------------- locate
    def search(self, provider: str, kind: str, payload: dict, query: str, **call) -> list[dict]:
        """One search, pinned to `provider` (parallel or exa); `payload` is logged as the request it was sent."""
        if not self.can_pay():
            return []
        call_id, started = f"{self.r.run_id}:{self.short}:{provider}:{len(query)}", time.time()
        row = {"call_id": call_id, "item_id": self.short, "provider": provider, "kind": kind, "query": query,
               "payload": payload, "called_at": now()}
        try:
            try:
                res = self.r.providers.search(query, provider={"parallel": PARALLEL, "exa": EXA}[provider], **call)
            except PriceDrift as drift:  # paid: keep the answer, stop paid calls
                if drift.result is None:
                    raise
                res, self.r.stopped = drift.result, str(drift)[:300]
            results = res.response
            row |= {"route": res.route, "cost_usd": res.cost, "cost_source": res.cost_source, "cache_hit": res.cached,
                    "status": res.status, "n_results": len(results)}
        except Exception as error:
            results, row = [], row | {"status": type(error).__name__, "error": str(error)[:500], "n_results": 0}
        row["latency_ms"] = int((time.time() - started) * 1000)
        self.charge(row.get("cost_usd"))
        self.r.log.add(eval_log.SEARCH_CALLS, row)
        for rank, x in enumerate(results, start=1):
            self.r.log.add(eval_log.SEARCH_RESULTS, {"call_id": call_id, "item_id": self.short, "provider": provider, "rank": rank,
                                                     "url": x.get("url"), "host": web.host_of(x.get("url") or ""), "title": x.get("title"),
                                                     "published_date": x.get("published_date"),
                                                     "excerpt": (x.get("excerpt") or "")[:2000] or None, "raw": x.get("raw")})
        self.step("locate", f"search ({provider})", None, f"{len(results)} results", {"query": query, "status": row.get("status")},
                  cost=row.get("cost_usd") or 0.0, elapsed_ms=row["latency_ms"])
        return results

    def scrape(self, url: str, stage: str) -> web.Page | None:
        if url in self.scraped:
            return self.scraped[url]
        if not self.can_pay() or self.scrapes >= MAX_SCRAPES or web.host_of(url) in self.http.blocked:
            return None
        self.scrapes += 1
        page, facts = web.scrape(self.r.providers, url)
        if facts.get("stopped"):  # the price guard: the page was paid for and is kept; paid calls stop
            self.r.stopped = facts["stopped"]
        self.charge(facts.get("cost_usd"))
        self.fetch_log(stage, "firecrawl", facts=facts)
        if facts.get("bot_wall"):
            self.http.blocked[web.host_of(url)] = "bot-check page (Firecrawl)"
            page = None
        self.step(stage, "scrape (Firecrawl)", url, "ok" if page else (facts.get("error") or f"status {facts.get('status')}"),
                  {"links": len(page.links) if page else 0, "proxy": facts.get("proxy")}, cost=facts.get("cost_usd") or 0.0,
                  elapsed_ms=facts.get("elapsed_ms"))
        self.scraped[url] = page
        return page

    def locate(self, card: dict, home: web.Page | None, website: str | None) -> list[dict]:
        name, state, kind = card.get("name", ""), card.get("state", ""), card.get("kind", "")
        home_site = web.site_of(home.url) if home else None
        found: dict[str, dict] = {}

        def add(source, rank, url, title):
            if not url or not url.startswith("http"):
                return
            url = url.split("#")[0]
            score, reasons = score_candidate(url, title or "", home_site)
            if url not in found or score > found[url]["score"]:
                found[url] = {"source": source, "rank": rank, "url": url, "title": (title or "")[:300], "score": score,
                              "reasons": reasons, "platform": platform_of(url)}

        for i, link in enumerate(home.links if home else [], start=1):
            if text.keep_for_locate(link.href, link.text):
                add("homepage_link", i, link.href, link.text)
        query = f"{name.title()} {state} {BOARD_WORD.get(kind, 'board')} meeting agendas minutes"
        objective = (f"The page where {name.title()}, a {KIND_WORD.get(kind, 'public body')} in {state}, publishes its governing "
                     f"board's meeting agendas, packets and minutes")
        for rank, x in enumerate(self.search("parallel", "search_site", {"objective": objective, "search_queries": [query]}, query,
                                             objective=objective), 1):
            add("search_open", rank, x.get("url"), x.get("title"))
        if not home_site:  # the registry's site is dead: restrict to the best open result that names the body
            named = [c for c in found.values() if c["source"] == "search_open" and not c["platform"]
                     and names_body(name, c["title"])]
            home_site = web.site_of(named[0]["url"]) if named else None
            if home_site:  # the body's real homepage, found by search: its links are candidates too
                real = self.reach(card, f"https://www.{home_site}")
                for i, link in enumerate(real.links if real else [], start=1):
                    if text.keep_for_locate(link.href, link.text):
                        add("homepage_link", i, link.href, link.text)
        if home_site:
            site_query = f"{BOARD_WORD.get(kind, 'board')} meeting agendas minutes"
            for rank, x in enumerate(self.search("exa", "search_open", {"query": site_query, "numResults": 10,
                                                                         "includeDomains": [home_site]}, site_query,
                                                 domain=home_site, n=10), 1):
                add("search_site", rank, x.get("url"), x.get("title"))
            for c in found.values():  # rescore against the site now known
                c["score"], c["reasons"] = score_candidate(c["url"], c["title"], home_site)

        ranked = sorted(found.values(), key=lambda c: (-c["score"], c["source"] != "search_site", c["rank"]))
        for c in ranked:
            c |= {"checked": False, "is_listing": False, "dated_doc_links": 0, "picked": False}
        checked, queue = 0, [c for c in ranked if c["score"] >= 2 and not is_file(c["url"])]
        while queue and checked < MAX_CHECKED and len(self.sources) < MAX_SOURCES:
            c = queue.pop(0)
            if c["platform"] == "diligent":
                c |= {"checked": True, "is_listing": True, "picked": True}
                if not any(s["platform"] == "diligent" for s in self.sources):
                    self.sources.append(c)
                continue
            checked += 1
            page = self.scrape(c["url"], "locate")
            c["checked"] = True
            if not page:
                continue
            dated = sum(1 for l in page.links if text.keep_for_documents(l.href, l.text, l.row)
                        and (text.find_date(l.text) or text.find_date(unquote(l.href)) or text.find_date(l.row)))
            c["dated_doc_links"], c["is_listing"] = dated, dated >= 3
            if c["is_listing"]:
                if not any(s["url"].lower() == c["url"].lower() for s in self.sources):
                    c["picked"] = True
                    self.sources.append(c)
                continue
            # a board page that is not itself a list: one hop to its agenda, minutes and packet links
            hop = []
            for link in page.links:
                if is_file(link.href) or link.href.split("#")[0] in found or not re.search(r"agenda|minute|packet", f"{link.text} {link.href}", re.I):
                    continue
                add("page_link", len(found) + 1, link.href, link.text)
                hop.append(found[link.href.split("#")[0]])
            for h in hop:
                h |= {"checked": False, "is_listing": False, "dated_doc_links": 0, "picked": False}
            ranked += hop
            queue = sorted(queue + [h for h in hop if h["score"] >= 2], key=lambda x: -x["score"])
        for c in ranked:
            self.r.log.add(store.CANDIDATES, {"body_id": self.body_id, **c})
        self.step("locate", "pick sources", None, f"{len(self.sources)} of {len(ranked)} candidates",
                  {"sources": [s["url"] for s in self.sources]})
        return self.sources

    # ----------------------------------------------------------------------------------- list
    def meeting(self, d: date | None, governing: bool, title: str, source_url: str, platform: str, key=None, n=0):
        if not d or d < LIST_SINCE:
            return
        m = self.meetings.setdefault((d, governing), {"title": title, "source_url": source_url, "platform": platform,
                                                      "platform_key": key, "n": 0})
        m["n"] += n

    def http_page(self, url: str, stage: str) -> web.Page | None:
        """Our own GET of a portal page (free, our User-Agent), read like a scraped page."""
        resp = self.http.get(url, use_cache=False)
        self.fetch_log(stage, "http", resp)
        self.step(stage, "GET (adapter)", url, "ok" if resp.ok else (resp.error or f"HTTP {resp.status}"), elapsed_ms=resp.elapsed_ms)
        return web.read_html(resp.text, resp.final_url or url) if resp.ok and "html" in (resp.content_type or "") else None

    def list_firecrawl(self, source: dict) -> None:
        self.list_pages(source, [source["url"]], lambda u: self.scrape(u, "list"), via="firecrawl")

    def list_destiny(self, source: dict) -> None:
        """Destiny Hosted shows one month per page: walk the months of the window with our own GET (an adapter)."""
        m = re.search(r"[?&]id=(\d+)", source["url"])
        if not m:
            return self.list_firecrawl(source)
        base = f"https://public.destinyhosted.com/agenda_publish.cfm?id={m.group(1)}&mt=ALL"
        months, d = [], date(LIST_SINCE.year, LIST_SINCE.month, 1)
        while d <= date.today() + timedelta(days=31):
            months.append(f"{base}&get_month={d.month}&get_year={d.year}")
            d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
        self.step("list", "Destiny month views", base, f"{len(months)} months")
        self.list_pages(source, months, lambda u: self.http_page(u, "list"), via="http", more_views=False)

    def list_pages(self, source: dict, start: list[str], read, via: str, more_views: bool = True) -> None:
        """Read meetings lists, then the 2026 meeting pages they link to. Lists go first, so a cap on pages never
        costs the meeting count."""
        lists, pages, seen, listed = list(start), [], set(), 0
        platform = source["platform"] or "own_site"
        while lists or pages:
            is_list = bool(lists)
            url, inherit = (lists.pop(0), None) if is_list else pages.pop(0)
            if url in seen:
                continue
            seen.add(url)
            page = read(url)
            if not page:
                continue
            doc_links = [(l.href, l.text) for l in page.links if text.keep_for_documents(l.href, l.text, l.row)]
            if is_list:
                listed += 1
                years = sorted({d.year for l in page.links for d in [text.find_date(l.text) or text.find_date(unquote(l.href))
                                                                     or text.find_date(l.row)] if d})
                self.r.log.add(store.LISTINGS, {"body_id": self.body_id, "url": url, "via": via, "platform": platform,
                                                "fingerprint": text.listing_hash(doc_links), "n_links": len(page.links),
                                                "n_doc_links": len(doc_links), "years": years, "fetched_at": now()})
            page_year = text.find_year(url)
            for l in page.links:
                if not text.keep_for_documents(l.href, l.text, l.row):
                    continue
                default_year = text.find_year(l.heading) or page_year or (inherit.year if inherit else None)
                if is_list:
                    found = text.find_date(l.text) or text.find_date(unquote(l.href)) or text.find_date(l.row, default_year)
                else:  # a meeting's own page: its documents are that meeting's
                    found = inherit
                kind, reason = classify_link(l, found, url)
                governing = is_governing(l)
                cancelled = bool(re.search(r"cancel", f"{l.text} {l.row}", re.I))
                follow = False
                if kind in ("agenda", "packet", "minutes", "document") and found and found >= LIST_SINCE:
                    if is_list and not cancelled:
                        self.meeting(found, governing, l.row or l.text, url, platform, n=1)
                    if l.href not in self.docs and not cancelled:
                        self.docs[l.href] = Doc(l.href, kind, found, l.text, url, governing)
                    # an HTML agenda in the window is a meeting page too: its packet and minutes hang off it
                    follow = kind == "agenda" and not PDFISH.search(l.href) and found >= DOCS_SINCE and governing and not cancelled
                elif kind == "meeting_page" and is_list and found and found >= LIST_SINCE and not cancelled:
                    self.meeting(found, governing, l.row or l.text, url, platform)
                    follow = governing and found >= DOCS_SINCE
                elif kind == "listing_page" and is_list and more_views and listed + len(lists) < 12:
                    y = text.find_year(f"{l.text} {unquote(l.href)}")
                    if (y is None or y >= LIST_SINCE.year) and l.href not in seen and l.href not in lists:
                        lists.append(l.href)
                        follow = "queued"
                if follow is True and l.href not in seen:
                    pages.append((l.href, found))
                self.r.log.add(store.LINKS, {"body_id": self.body_id, "page_url": url, "href": l.href, "text": l.text, "row": l.row,
                                             "heading": l.heading, "found_date": found.isoformat() if found else None, "kind": kind,
                                             "governing": governing, "reason": reason + (" (cancelled)" if cancelled else ""),
                                             "followed": bool(follow)})
            if not is_list and url in self.docs and self.docs[url].text is None:
                self.docs[url].text = page.text  # the HTML agenda we just read

    def list_diligent(self, source: dict) -> None:
        portal = source["url"]
        until = date.today() + timedelta(days=60)
        resp = self.http.get(diligent.meetings_url(portal, LIST_SINCE, until), use_cache=False)
        self.fetch_log("list", "diligent_api", resp)
        if not resp.ok:
            self.step("list", "Diligent meetings", portal, resp.error or f"HTTP {resp.status}")
            return
        meetings = diligent.read_meetings(resp.body)
        self.r.log.add(store.LISTINGS, {"body_id": self.body_id, "url": resp.url, "via": "diligent_api", "platform": "diligent",
                                        "fingerprint": text.text_hash(json.dumps([[m.get("Id"), m.get("MeetingDate")] for m in meetings])),
                                        "n_links": len(meetings), "n_doc_links": len(meetings),
                                        "years": sorted({int(str(m.get("MeetingDate"))[:4]) for m in meetings if m.get("MeetingDate")}),
                                        "fetched_at": now()})
        self.step("list", "Diligent meetings", portal, f"{len(meetings)} meetings", elapsed_ms=resp.elapsed_ms)
        for m in meetings:
            d = date.fromisoformat(str(m.get("MeetingDate"))[:10])
            title = m.get("Name") or m.get("MeetingTypeName") or ""
            governing = not GOVERNING_NOT.search(title)
            self.meeting(d, governing, title, portal, "diligent", str(m.get("Id")))
            if d < DOCS_SINCE or not governing:
                continue
            r2 = self.http.get(diligent.documents_url(portal, m["Id"]), use_cache=d < date.today() - timedelta(days=45))
            self.fetch_log("list", "diligent_api", r2)
            if not r2.ok:
                continue
            documents, items, attachments = diligent.read_documents(r2.body, portal)
            self.meetings[(d, governing)]["n"] += len(documents)
            self.portal_counts[d.isoformat()] = (len(items), len(attachments))
            self.step("list", "Diligent meeting documents", r2.url, f"{len(documents)} documents, {len(items)} items, {len(attachments)} attachments",
                      {"meeting_date": d.isoformat(), "items": len(items), "attachments": len(attachments)}, elapsed_ms=r2.elapsed_ms)
            for doc in documents:
                agenda = diligent.agenda_text(items) if doc["kind"] == "agenda" and items else None
                self.docs[doc["url"]] = Doc(doc["url"], doc["kind"], d, doc["title"], portal, governing, agenda)
            for a in attachments:
                self.r.log.add(store.LINKS, {"body_id": self.body_id, "page_url": r2.url, "href": a["url"], "text": a["title"],
                                             "row": None, "heading": None, "found_date": d.isoformat(), "kind": "attachment",
                                             "governing": governing, "reason": f"agenda item {a['item']}", "followed": False})

    # ----------------------------------------------------------------------------------- fetch and read
    def fetch_docs(self) -> list[tuple[Doc, str]]:
        import pdf_inspector
        wanted = sorted((d for d in self.docs.values() if d.governing and d.meeting_date and d.meeting_date >= DOCS_SINCE
                         and d.kind in ("agenda", "packet", "minutes", "document")),
                        key=lambda d: (d.meeting_date, DOC_ORDER.get(d.kind, 9)), reverse=True)[:MAX_DOCS]
        out = []
        for d in wanted:
            row = {"body_id": self.body_id, "meeting_date": d.meeting_date.isoformat(), "kind": d.kind, "url": d.url, "title": d.title,
                   "listing_url": d.listing_url, "governing": d.governing, "downloaded": False}
            if d.text:  # already read (an HTML agenda page, a Diligent agenda)
                out.append((d, d.text))
                self.r.log.add(store.DOCUMENTS, row | {"content_type": "text/html", "text_chars": len(d.text)})
                continue
            if d.kind == "packet" and diligent.is_portal(d.url):
                self.r.log.add(store.DOCUMENTS, row | {"error": "Diligent packet not downloaded (the agenda gives the items)"})
                continue
            resp = self.http.get(d.url)
            if not resp.ok:
                self.fetch_log("fetch", "http", resp)
                self.r.log.add(store.DOCUMENTS, row | {"error": resp.error or f"HTTP {resp.status}"})
                continue
            is_pdf = resp.body[:5] == b"%PDF-"
            name = f"{d.meeting_date.isoformat()}-{d.kind}-{resp.sha256[:8]}.{'pdf' if is_pdf else 'html'}"
            rel, sha = web.save_download(self.short, name, resp.body)
            self.fetch_log("fetch", "http", resp, path=rel)
            row |= {"downloaded": True, "path": rel, "bytes": len(resp.body), "sha256": sha, "content_type": resp.content_type}
            started = time.time()
            try:
                if is_pdf:
                    res = pdf_inspector.process_pdf(str(paths.stored(rel)))
                    body = res.markdown or ""
                    row |= {"pdf_type": res.pdf_type, "pages": res.page_count, "pages_needing_ocr": len(res.pages_needing_ocr or [])}
                else:
                    body = web.read_html(resp.text, resp.final_url or d.url).text
                row |= {"text_chars": len(body), "read_ms": int((time.time() - started) * 1000)}
                out.append((d, body))
            except Exception as error:
                row |= {"error": f"{type(error).__name__}: {str(error)[:200]}"}
            self.r.log.add(store.DOCUMENTS, row)
        self.step("fetch", "documents read", None, f"{len(out)} of {len(wanted)}",
                  {"pdfs": sum(1 for d, _ in out if d.url and not d.text), "kinds": sorted({d.kind for d, _ in out})})
        return out

    def read_items(self, docs: list[tuple[Doc, str]]) -> None:
        for d, body in docs:
            for seq, passage in enumerate(text.segments(body), start=1):
                money = text.amounts(passage)
                vendors = text.vendor_candidates(passage)
                term_text, end_year = text.term(passage)
                amount_max = max(money) if money else None
                vendor = vendors[0] if vendors else None
                key = f"{d.meeting_date}|{int(amount_max) if amount_max else ''}|{(vendor or '').lower()[:30]}" \
                    if (amount_max or vendor) else f"{d.meeting_date}|{text.text_hash(passage)[:16]}"
                self.items.append({"body_id": self.body_id, "meeting_date": d.meeting_date.isoformat(), "doc_kind": d.kind,
                                   "doc_url": d.url, "seq": seq, "text": passage, "text_hash": text.text_hash(passage),
                                   "amounts": money, "amount_max": amount_max, "vendor_candidates": vendors, "vendor": vendor,
                                   "term_text": term_text, "term_end_year": end_year, "event_type": event_type(passage),
                                   "action": action(passage, d.kind), "event_key": key})
        firsts = {}
        for it in sorted(self.items, key=lambda i: (DOC_ORDER.get(i["doc_kind"], 9), i["seq"])):
            firsts.setdefault(it["event_key"], id(it))
        for it in self.items:
            it["first_in_meeting"] = firsts[it["event_key"]] == id(it)
            self.r.log.add(store.ITEMS, it)
        self.step("items", "passages read by rule", None, f"{len(self.items)} items, {len(firsts)} events")

    # ----------------------------------------------------------------------------------- answers
    def answer(self) -> None:
        as_of = date.today().isoformat()

        def rows(types, need_amount):
            started = time.time()
            out = [{"meeting_date": i["meeting_date"], "type": i["event_type"], "action": i["action"], "vendor": i["vendor"],
                    "amount": i["amount_max"], "term": i["term_text"], "quote": i["text"][:300], "source": i["doc_url"],
                    "doc_kind": i["doc_kind"]}
                   for i in self.items if i["first_in_meeting"] and i["event_type"] in types and (i["amount_max"] or not need_amount)]
            out.sort(key=lambda x: (-(x["amount"] or 0), x["meeting_date"]))
            return out, int((time.time() - started) * 1000)

        for qid, question, types, need in (
                ("Q1", "What has the board bought since 2026-01-01: contracts, renewals and purchases, with vendor and amount?",
                 ("contract", "renewal", "purchase"), True),
                ("Q2", "What is open: bids, RFPs and grants authorized or awarded since 2026-01-01?", ("bid", "grant"), False)):
            out, ms = rows(types, need)
            self.r.log.add(store.ANSWERS, {"body_id": self.body_id, "question_id": qid, "question": question, "as_of": as_of,
                                           "n_rows": len(out), "rows": out[:200], "answer_ms": ms})
        started = time.time()
        governing = sorted(d for (d, g) in self.meetings if g)
        past = [d for d in governing if d <= date.today()]
        future = [d for d in governing if d > date.today()]
        latest = past[-1] if past else None
        latest_docs = [{"kind": d.kind, "url": d.url} for d in self.docs.values() if d.meeting_date == latest and d.governing]
        latest_items = [i for i in self.items if i["meeting_date"] == (latest.isoformat() if latest else None) and i["first_in_meeting"]]
        out = [{"latest_meeting": latest.isoformat() if latest else None, "next_meeting": future[0].isoformat() if future else None,
                "documents": latest_docs, "buying_items": len([i for i in latest_items if i["event_type"] != "none"]),
                "days_since_latest": (date.today() - latest).days if latest else None}]
        self.r.log.add(store.ANSWERS, {"body_id": self.body_id, "question_id": "Q3", "question": "What is next: the latest or "
                                       "upcoming meeting and what is on its agenda?", "as_of": as_of, "n_rows": 1, "rows": out,
                                       "answer_ms": int((time.time() - started) * 1000)})
        self.step("answer", "three questions", None, "done")

    # ----------------------------------------------------------------------------------- scorecard
    def check(self) -> None:
        def add(check, expected, got, passed, note):
            self.r.log.add(store.CHECKS, {"body_id": self.body_id, "check": check, "expected": str(expected), "got": str(got),
                                          "passed": passed, "note": note})
        counts = {}
        for (d, g) in self.meetings:
            if g:
                counts[d.year] = counts.get(d.year, 0) + 1
        if self.spec.get("discovery"):
            known = json.loads((DISCOVERY / self.spec["discovery"]).read_text())
            note = f"work/deep-discovery/{self.spec['discovery']}"
            urls = [s["url"].split(" ")[0] for s in known["meetings_sources"]]
            hosts = {web.host_of(u) for u in urls}
            got = [s["url"] for s in self.sources]
            add("found a known meetings list (same host)", sorted(hosts), got, any(web.host_of(u) in hosts for u in got), note)
            board = next((b for b in known["boards"] if b.get("role") == "governing"), {})
            for year, n in (board.get("meetings_per_year") or {}).items():
                if int(year) >= LIST_SINCE.year and isinstance(n, int):
                    g = counts.get(int(year), 0)
                    add(f"governing meetings in {year}", n, g, abs(g - n) <= max(2, round(n * 0.1)), note + " (within 10% or 2)")
        hand = self.spec.get("hand_map")
        if hand:
            d = date.fromisoformat(hand["date"])
            listed = any(m == d for (m, _) in self.meetings)
            add("hand-mapped meeting found", hand["date"], listed, listed, hand["note"])
            items, attachments = self.portal_counts.get(hand["date"], (None, None))
            note = hand["note"] if items is not None else hand["note"] + "; not comparable: the chain used the district's own site, not the portal"
            add("agenda items on the hand-mapped meeting", hand["items"], items, None if items is None else items == hand["items"], note)
            add("attachments on the hand-mapped meeting", hand["attachments"], attachments,
                None if attachments is None else attachments == hand["attachments"], note)
        self.step("check", "scorecard", None, "done")


# ------------------------------------------------------------------------------------------- run
def run_body(short: str, r: Runner, http: web.Http, paid: bool) -> None:
    import duckdb
    b = BodyRun(short, BODIES[short], r, http, paid)
    con = duckdb.connect()  # one connection per body: DuckDB connections are not shared across threads
    card = body_card(con, b.body_id)
    website, record_id = con.execute(f"SELECT website, anchor_record_id FROM {tables.latest('entities', 'bodies')} WHERE body_id = ?",
                                     [b.body_id]).fetchone()
    with r.log.lock:
        r.item({"item_id": f"deep:{short}", "sample": "deep-4", "stratum": card.get("kind"), "body_id": b.body_id,
                "record_id": record_id, "card": {"body": card, "website": website}})
    home = b.reach(card, website)
    sources = b.locate(card, home, website)
    for s in sources:
        lister = {"diligent": b.list_diligent, "destinyhosted": b.list_destiny}.get(s["platform"], b.list_firecrawl)
        b.step("route", "adapter" if lister != b.list_firecrawl else "firecrawl", s["url"], s["platform"] or "own_site")
        lister(s)
    for (d, g), m in sorted(b.meetings.items()):
        r.log.add(store.MEETINGS, {"body_id": b.body_id, "meeting_date": d.isoformat(), "title": (m["title"] or "")[:300], "governing": g,
                                   "source_url": m["source_url"], "platform": m["platform"], "platform_key": m.get("platform_key"),
                                   "n_documents": m["n"]})
    b.step("list", "meetings found", None, f"{sum(1 for (_, g) in b.meetings if g)} governing, {sum(1 for (_, g) in b.meetings if not g)} other")
    b.read_items(b.fetch_docs())
    b.answer()
    b.check()
    print(f"{short}: {len(sources)} sources, {len(b.meetings)} meetings, {len(b.docs)} documents, {len(b.items)} items, "
          f"{b.scrapes} scrapes", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["run"])
    parser.add_argument("--bodies", default=",".join(BODIES))
    parser.add_argument("--no-paid", action="store_true", help="no searches or Firecrawl: homepage links and portals only")
    parser.add_argument("--budget", type=float, default=1.0, help="this run's cap, USD")
    parser.add_argument("--approved-by", default="", help="who approved the run (free text, recorded with it)")
    args = parser.parse_args()
    shorts = [s for s in args.bodies.split(",") if s]
    r = Runner("deep-P1-free" if args.no_paid else "deep-P1", "Deep chain Phase 1: plain plumbing, no Jev, on four bodies",
               0.0 if args.no_paid else args.budget, args.approved_by, extra_specs=store.ALL)
    http = web.Http()
    with ThreadPoolExecutor(max_workers=len(shorts)) as pool:
        for f in [pool.submit(run_body, s, r, http, not args.no_paid) for s in shorts]:
            try:
                f.result()
            except Exception as error:  # one body failing does not stop the others; the run records it
                print(f"error: {type(error).__name__}: {error}", flush=True)
    r.finish()


if __name__ == "__main__":
    main()
