"""Getting pages and files for the deep chain, and reading their links.

- Http: our own requests (homepages, PDFs, portal data): our User-Agent, one request at a time per host, a few seconds
  apart, nothing retried around a bot wall: a 403, 429 or bot-check page stops that host for the run (notes/RULES.md).
  Responses are kept in a local cache (data/cache/deep/), so a re-run reads them again for free.
- scrape, through the provider layer (ddwb.providers; Firecrawl by default): a page as Markdown with its links, for pages
  built by script and not on a known portal. A host stopped for a bot wall is never scraped; a bot-wall page is dropped.
- Link readers keep, for each link, the text of its table row or list item and the heading above it, because
  meetings lists often say only "Agenda" in the link and put the date in the row.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx

from .. import paths
from ..fetch.live_check import BOT_WALL, USER_AGENT

CACHE = paths.CACHE / "deep"
MIN_GAP = 3.0
HOST_GAP = {"pylusd.org": 5.0}  # the site's robots.txt asks for 5 seconds
MAX_BYTES = 500_000_000  # a guard against runaway responses, not a reading limit: hitting it is an error (60 MB cut 5 board packets)
OLD_CAP = 60_000_000  # the cap before 2026-11-03, which cut files silently


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def site_of(url: str) -> str:
    """The registrable part of a host, roughly: council.summitoh.net -> summitoh.net, co.summit.oh.us -> summit.oh.us,
    www.pylusd.k12.ca.us -> pylusd.k12.ca.us."""
    parts = host_of(url).split(".")
    if len(parts) >= 3 and parts[-1] == "us" and len(parts[-2]) == 2:
        n = 4 if len(parts) >= 4 and parts[-3] in ("k12", "ci", "co", "lib", "state", "cc", "dst", "pvt") else 3
    elif len(parts) > 2 and len(parts[-1]) == 2 and parts[-2] in ("co", "com", "org", "gov", "ac", "net", "edu"):
        n = 3
    else:
        n = 2
    return ".".join(parts[-n:])


@dataclass
class Response:
    url: str
    final_url: str | None = None
    status: int | None = None
    content_type: str | None = None
    body: bytes = b""
    error: str | None = None
    elapsed_ms: int = 0
    from_cache: bool = False
    bot_wall: bool = False
    fetched_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))

    @property
    def sha256(self) -> str | None:
        return hashlib.sha256(self.body).hexdigest() if self.body else None

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 400 and not self.bot_wall and not self.error


class Http:
    """Polite GETs with a cache. Thread-safe: each host has its own lock and clock."""

    def __init__(self, cache: Path | None = None) -> None:
        self.cache = cache or CACHE  # read when made, so a test can point it elsewhere
        self.cache.mkdir(parents=True, exist_ok=True)
        self.client = httpx.Client(headers={"User-Agent": USER_AGENT}, follow_redirects=True, max_redirects=8,
                                   timeout=httpx.Timeout(40.0, connect=10.0))
        self.locks: dict[str, threading.Lock] = {}
        self.last: dict[str, float] = {}
        self.blocked: dict[str, str] = {}
        self.guard = threading.Lock()

    def _key(self, url: str) -> Path:
        return self.cache / hashlib.sha1(url.encode()).hexdigest()

    def get(self, url: str, use_cache: bool = True) -> Response:
        return self.request("GET", url, use_cache=use_cache)

    def post(self, url: str, data: dict | None = None, json_body: dict | None = None, headers: dict | None = None,
             use_cache: bool = True) -> Response:
        """A form or JSON POST, paced and cached like a GET (a portal's list of meetings behind a form)."""
        return self.request("POST", url, data=data, json_body=json_body, headers=headers, use_cache=use_cache)

    def request(self, method: str, url: str, data: dict | None = None, json_body: dict | None = None,
                headers: dict | None = None, use_cache: bool = True) -> Response:
        payload = json.dumps({"data": data, "json": json_body, "headers": headers}, sort_keys=True) if method != "GET" else ""
        key = self._key(url if method == "GET" else f"{method} {url} {payload}")
        if use_cache and key.with_suffix(".json").exists():
            meta = json.loads(key.with_suffix(".json").read_text())
            body = key.with_suffix(".bin").read_bytes()
            if not (OLD_CAP <= len(body) < OLD_CAP + 100_000):  # files the old 60 MB cap cut short are fetched again
                return Response(**meta, body=body, from_cache=True)
        host = host_of(url)
        if host in self.blocked:
            return Response(url, error=f"host stopped earlier this run: {self.blocked[host]}", bot_wall=True)
        with self.guard:
            lock = self.locks.setdefault(host, threading.Lock())
        with lock:
            gap = HOST_GAP.get(site_of(url), MIN_GAP) - (time.monotonic() - self.last.get(host, 0.0))
            if gap > 0:
                time.sleep(gap)
            started = time.monotonic()
            out = Response(url)
            try:
                with self.client.stream(method, url, data=data, json=json_body, headers=headers) as r:
                    chunks, size = [], 0
                    for chunk in r.iter_bytes():
                        chunks.append(chunk)
                        size += len(chunk)
                        if size >= MAX_BYTES:
                            out.error = f"stopped at the {MAX_BYTES // 1_000_000} MB guard: the file is larger"
                            break
                    body = b"".join(chunks)
                out.final_url, out.status, out.content_type, out.body = str(r.url), r.status_code, r.headers.get("content-type"), body
            except Exception as error:  # recorded, not retried (but see S3 below)
                out.error = f"{type(error).__name__}: {str(error)[:200]}"
                failed = str(getattr(getattr(error, "request", None), "url", "") or "")
                path_style = s3_path_style(failed)
                if path_style and path_style != url:
                    self.last[host] = time.monotonic()
                    return self.request(method, path_style, data=data, json_body=json_body, headers=headers, use_cache=use_cache)
            self.last[host] = time.monotonic()
            out.elapsed_ms = int((time.monotonic() - started) * 1000)
        head = re.sub(r"(?is)<script\b.*?</script>|<!--.*?-->", " ", out.body[:40000].decode("utf-8", errors="ignore"))[:4000] \
            if "html" in (out.content_type or "") else ""
        out.bot_wall = out.status in (403, 429) or bool(BOT_WALL.search(head))
        if out.bot_wall:
            self.blocked[host] = f"HTTP {out.status}" if out.status in (403, 429) else "bot-check page"
        if out.status is not None and 200 <= out.status < 400 and not out.bot_wall and not out.error:  # errors never cached
            key.with_suffix(".bin").write_bytes(out.body)
            meta = {k: getattr(out, k) for k in ("url", "final_url", "status", "content_type", "error", "elapsed_ms", "bot_wall", "fetched_at")}
            key.with_suffix(".json").write_text(json.dumps(meta))
        return out


# ------------------------------------------------------------------------------------------- reading links
@dataclass
class Link:
    href: str
    text: str
    row: str = ""
    heading: str = ""


@dataclass
class Page:
    url: str
    title: str = ""
    text: str = ""
    links: list[Link] = field(default_factory=list)
    forms: list[dict] = field(default_factory=list)  # {action, method, fields: [{name, type, value, options}]}: views behind a form


_ROW_TAGS = ("tr", "li", "dt", "dd", "article")
_BLOCK_TAGS = ("p", "div", "td", "section")
_HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6", "caption", "legend", "summary")


class _Reader(HTMLParser):
    """Text, title and every link with its row (nearest tr/li, else nearest block) and the heading before it."""

    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base, self.title, self.text, self.links = base, "", [], []
        self.stack: list[tuple[str, list[str]]] = []  # open blocks: (tag, text parts)
        self.pending: list[tuple[Link, list[str], int, bool]] = []  # link, its row's text parts (filled as we go), where
        # the link starts in them, and whether the row is a table row or list item (else a block, split at <br>)
        self.heading, self._head, self._skip, self._title, self._link = "", None, 0, False, None
        self.forms, self._form, self._select, self._option = [], None, None, None

    def _form_tag(self, tag, a):
        """Forms: a year or month dropdown is often the only way to other views of a meetings list."""
        if tag == "form":
            self._form = {"action": urljoin(self.base, a.get("action") or self.base), "method": (a.get("method") or "get").lower(), "fields": []}
        elif self._form is None:
            return
        elif tag == "select" and a.get("name"):
            self._select = {"name": a["name"], "type": "select", "options": []}
        elif tag == "option" and self._select is not None:
            self._option = [a.get("value"), ""]
        elif tag == "input" and a.get("name") and (a.get("type") or "text").lower() in ("hidden", "text", "search", "number", "date", "radio", "checkbox"):
            self._form["fields"].append({"name": a["name"], "type": (a.get("type") or "text").lower(), "value": a.get("value", "")})

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in ("form", "select", "option", "input"):
            self._form_tag(tag, a)
        if tag in ("script", "style", "noscript", "svg", "template"):
            self._skip += 1
        elif tag == "title":
            self._title = True
        elif tag in _HEADINGS:
            self._head = []
        if tag in _ROW_TAGS or tag in _BLOCK_TAGS:
            self.stack.append((tag, []))
        if tag == "base" and a.get("href"):
            self.base = urljoin(self.base, a["href"])
        if tag in ("br", "hr"):
            for _, parts in self.stack:
                parts.append(LINE)
        if tag == "a" and a.get("href"):
            link = Link(urljoin(self.base, a["href"].strip()), "", heading=self.heading)
            is_row = next((parts for t, parts in reversed(self.stack) if t in _ROW_TAGS), None)
            row = is_row if is_row is not None else next((parts for t, parts in reversed(self.stack)), [])
            self._link = link
            self.pending.append((link, row, len(row), is_row is not None))

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg", "template") and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._title = False
        elif tag in _HEADINGS and self._head is not None:
            self.heading = re.sub(r"\s+", " ", "".join(self._head)).strip()[:200]
            self._head = None
        elif tag == "a":
            self._link = None
        elif tag == "option" and self._option is not None and self._select is not None:
            value, label = self._option
            self._select["options"].append([label.strip() if value is None else value, re.sub(r"\s+", " ", label).strip()])
            self._option = None
        elif tag == "select" and self._select is not None and self._form is not None:
            self._form["fields"].append(self._select)
            self._select = None
        elif tag == "form" and self._form is not None:
            if self._form["fields"]:
                self.forms.append(self._form)
            self._form = None
        if (tag in _ROW_TAGS or tag in _BLOCK_TAGS) and any(t == tag for t, _ in self.stack):
            while self.stack:
                t, _ = self.stack.pop()
                if t == tag:
                    break

    def handle_data(self, data):
        if self._skip:
            return
        if self._title:
            self.title += data
        if self._head is not None:
            self._head.append(data)
        if self._link is not None:
            self._link.text += data
        if self._option is not None:
            self._option[1] += data
        for _, parts in self.stack:
            parts.append(data)
        self.text.append(data)


GOOGLE_DOC = re.compile(r"^https?://docs\.google\.com/(document|spreadsheets|presentation)/d/([\w-]{20,})")
GOOGLE_FILE = re.compile(r"^https?://drive\.google\.com/(?:file/d/([\w-]{20,})|open\?id=([\w-]{20,}))")


def direct_url(url: str) -> str:
    """The address that returns a shared Google document's content, not its viewer page: a Doc as plain text, a Sheet as
    CSV, Slides as plain text, a Drive file as its download. Any other address is returned as it is."""
    m = GOOGLE_DOC.match(url or "")
    if m:
        kind, doc = m.groups()
        return f"https://docs.google.com/{kind}/d/{doc}/export?format={'csv' if kind == 'spreadsheets' else 'txt'}"
    m = GOOGLE_FILE.match(url or "")
    if m:
        return f"https://drive.google.com/uc?export=download&id={m.group(1) or m.group(2)}"
    return url


S3_VHOST = re.compile(r"^https?://([a-z0-9._-]*_[a-z0-9._-]*)\.s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com(/.*)?$", re.I)


def s3_path_style(url: str) -> str | None:
    """A virtual-host S3 address whose bucket name holds an underscore (no valid TLS name) as its path-style twin:
    https://granicus_production_attachments.s3.amazonaws.com/x -> https://s3.amazonaws.com/granicus_production_attachments/x."""
    m = S3_VHOST.match(url or "")
    return f"https://s3.amazonaws.com/{m.group(1)}{m.group(2) or '/'}" if m else None


LINE = "\x00"  # a <br> inside a block


def row_text(parts: list[str], start: int, is_row: bool) -> str:
    """A link's row: the whole table row or list item; in a block split by <br> (a list of meetings as lines), the
    link's own line, with the line before it when the link's line has no digit (a date on the line above)."""
    full = "".join(parts)
    if is_row or LINE not in full:
        return full.replace(LINE, " ")
    pos, acc, lines = len("".join(parts[:start])), 0, full.split(LINE)
    at = len(lines) - 1
    for i, line in enumerate(lines):
        if acc + len(line) >= pos:
            at = i
            break
        acc += len(line) + 1
    seg = [lines[at]]
    while at > 0 and len(seg) < 3 and not re.search(r"\d", " ".join(seg)):
        at -= 1
        seg.insert(0, lines[at])
    return " ".join(seg)


def _clean(s: str, n: int) -> str:
    return re.sub(r"\s+", " ", s or "").strip()[:n]


def read_html(html: str, base: str) -> Page:
    reader = _Reader(base)
    try:
        reader.feed(html)
        reader.close()
    except Exception:
        pass
    links = [Link(l.href, _clean(l.text, 200), _clean(row_text(row, start, is_row), 400), l.heading)
             for l, row, start, is_row in reader.pending]
    return Page(base, _clean(reader.title, 300), _clean(" ".join(reader.text), 2_000_000), links, reader.forms)


MD_LINK = re.compile(r"(?<!!)\[([^\]]{0,300})\]\((<[^>]+>|[^()\s]+(?:\s+[^()\s\"]+)*)(?:\s+\"[^\"]*\")?\)")
MD_HEADING = re.compile(r"^\s*#{1,6}\s+(.*)")


def read_markdown(md: str, base: str, title: str = "") -> Page:
    """Links in Firecrawl's Markdown: the line a link sits on is its row (a table row or list item)."""
    links, heading = [], ""
    for line in (md or "").splitlines():
        h = MD_HEADING.match(line)
        if h:
            heading = _clean(MD_LINK.sub(r"\1", h.group(1)), 200)
        for m in MD_LINK.finditer(line):
            href = quote(m.group(2).strip("<>").strip(), safe=":/?&=#%+@;,~")  # file names with spaces
            if href.startswith("#"):
                continue
            links.append(Link(urljoin(base, href), _clean(m.group(1).replace("**", ""), 200),
                              _clean(MD_LINK.sub(r"\1", line).replace("|", " "), 400), heading))
    return Page(base, title, md or "", links)


def scrape(providers, url: str) -> tuple[Page | None, dict]:
    """One page through the configured scrape provider, as Markdown with links. Returns the page (None when there is
    none: an error, a bot wall, scrape switched off) and the call's facts for logging. A price-guard stop keeps the
    paid page and says so in facts["stopped"]."""
    from ..providers import BotWall, PriceDrift, ProviderError

    started = time.time()
    facts = {"via": "firecrawl", "url": url}
    res, page = None, None
    try:
        res = providers.scrape(url)
    except PriceDrift as drift:
        res, facts["stopped"] = drift.result, str(drift)[:300]
    except BotWall as wall:
        paid = wall.result
        facts |= {"error": str(wall)[:300], "bot_wall": True, "cost_usd": paid.cost if paid else 0.0,
                  "cache_hit": bool(paid and paid.cached), "status": (paid.response or {}).get("status") if paid else None}
    except ProviderError as error:
        facts |= {"error": f"{type(error).__name__}: {str(error)[:300]}", "cost_usd": 0.0}
    if res is not None:
        d = res.response or {}
        md = d.get("markdown") or ""
        facts |= {"via": res.vendor or res.provider, "provider": res.provider, "cost_usd": res.cost, "cache_hit": res.cached,
                  "status": d.get("status"), "final_url": d.get("final_url"), "content_type": d.get("content_type"),
                  "proxy": d.get("proxy"), "bytes": len(md.encode()), "sha256": hashlib.sha256(md.encode()).hexdigest(),
                  "error": d.get("error"), "bot_wall": False}
        page = read_markdown(md, d.get("final_url") or url, d.get("title") or "")
        if not page.links:  # the provider's bare link list has no text: used only when the Markdown has no links
            page.links = [Link(h, "") for h in d.get("links") or [] if isinstance(h, str)]
    facts["elapsed_ms"] = int((time.time() - started) * 1000)
    return page, facts


# ------------------------------------------------------------------------------------------- downloads
_manifest_lock = threading.Lock()


def save_download(short: str, name: str, body: bytes) -> tuple[str, str]:
    """Keep a downloaded file under the work root's data/deep/<body>/ and record it in its data-manifest.tsv
    (ddwb.paths). Returns (path relative to the work root, sha256)."""
    sha = hashlib.sha256(body).hexdigest()
    folder = paths.DOCS / short
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(body)
    rel = paths.to_stored(path)
    manifest = paths.MANIFEST
    with _manifest_lock:
        known = manifest.read_text().splitlines() if manifest.exists() else ["path\tbytes\tsha256"]
        if not any(line.startswith(rel + "\t") for line in known):
            new = not manifest.exists()
            manifest.parent.mkdir(parents=True, exist_ok=True)
            with manifest.open("a") as handle:
                handle.write(("path\tbytes\tsha256\n" if new else "") + f"{rel}\t{len(body)}\t{sha}\n")
    return rel, sha
