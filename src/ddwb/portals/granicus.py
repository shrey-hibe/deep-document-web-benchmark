"""Granicus (<client>.granicus.com): the MediaManager "ViewPublisher" meeting lists, and the agenda-management output
they link to. Plain GETs through the Fetch, no browser.

Pages read
  /ViewPublisher.php?view_id=N    a "view": every meeting the client publishes in it, all years on one page, grouped by
                                  category (the board) under year tabs; upcoming events (event_id) and archived clips
                                  (clip_id), each row with its Agenda, Minutes, Video and packet links. A client has
                                  several views (council, planning, an "all meetings" view); view ids are probed from 1
                                  until five in a row are missing (eight when none is found), plus any view id seen on
                                  the body's own page. "Training" views are skipped. A meeting in two views counts once.
  /AgendaViewer.php?view_id=&clip_id= (or &event_id=)
                                  the agenda: a redirect to the file (often on <client>.legistar1.com), or to
                                  /GeneratedAgendaViewer.php, the HTML agenda that Granicus agenda management writes:
                                  numbered items, notes with motions and votes, MetaViewer.php attachments (items())
  /MinutesViewer.php?...          the minutes: a redirect to the file, sometimes through a Google Docs viewer whose `url`
                                  parameter is the file (documents() returns the file)
  /services/minutes/reports/<id>/attachment
                                  minutes from the newer Granicus minutes product (it redirects to a signed S3 link that
                                  expires in minutes, so this address is the one returned)
  d3n9y02raazwpg.cloudfront.net/<client>/<uuid>.pdf
                                  agenda packets from the newer agenda product, linked from the view's rows
  archive-video.granicus.com/<client>/<uuid>.mp4|mp3, /MediaPlayer.php, /player/clip/<id>
                                  the recording and the player

What it cannot do
  - A client whose host shows only the MediaManager login and no public view, or whose views sit above the probe range
    and are not linked from the body's page, gives PortalError (no public view). An unknown client (Granicus answers
    /core/error/NotFound.aspx) gives PortalError.
  - Some event agendas redirect to granicus_production_attachments.s3.amazonaws.com, whose certificate does not match
    the bucket name (an underscore): the client refuses it, and the AgendaViewer link is returned unresolved.
  - Items come only from Granicus-generated HTML agendas; a meeting whose agenda is a PDF has no items here. Sub-items
    without their own number row and template-specific notes may be missed.
  - documents() opens the Agenda and Minutes viewer links (one GET each, files included) to return the file's own
    address; construct with resolve=False to skip that and get the viewer links.

Checked on (2026-11-05, India egress): matanuska (AK borough), buttecounty (CA county: its views stop in August 2024),
cityofnsb (FL city), comstock-mi (MI township, events only, the newer minutes product), cityofnewport (RI city);
views surveyed on 15 free-pool clients (fairbanksalaska shows only the MediaManager login; tuscaloosa and juneau are
unknown to Granicus).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlsplit

from .base import Document, Fetch, Item, Listing, Meeting, PortalError
from .meeting_words import board_from, clean, doc_kind, is_governing, meeting_kind, parse_date
from .registry import register

HOST = re.compile(r"^([a-z0-9][a-z0-9-]*)\.granicus\.com$", re.I)
VENDOR = {"www", "support", "webcontent", "archive-video", "archive-stream", "design", "cluster-1", "media", "static",
          "cdn", "help", "learn", "api", "admin", "app", "login", "s3", "video", "content", "resources", "info", "go",
          "community", "status", "docs", "assets", "i", "granicus", "public", "events", "player"}
PAGES = r"(?:ViewPublisher|MediaPlayer|AgendaViewer|MinutesViewer|GeneratedAgendaViewer|ViewSearchResults|ViewEvent|" \
        r"ViewPublisherRSS|MetaViewer|ASX|Podcast)\.php|player/(?:clip|event)/|services/minutes/"
EMBED = re.compile(rf"(?:https?:)?//([a-z0-9][a-z0-9-]*)\.granicus\.com/({PAGES})[^\s\"'<>]*", re.I)
ANY_LINK = re.compile(r"(?:https?:)?//([a-z0-9][a-z0-9-]*)\.granicus\.com\b[^\s\"'<>]*", re.I)
MAX_VIEW, MISS_AFTER_FOUND, MISS_NONE_FOUND = 40, 5, 8
SKIP_VIEW = re.compile(r"\btraining\b", re.I)
SKIP_LINK = re.compile(r"/(ASX|Podcast|VPodcast|ViewPublisherRSS|ViewSearchResults)\.php|^javascript:|^#|^mailto:", re.I)
SIGNED = re.compile(r"[?&](X-Amz-Signature|Signature|Expires|se|sig)=", re.I)


def client_of(url: str) -> str | None:
    m = HOST.match((urlsplit(url or "").hostname or "").lower())
    if not m or m.group(1) in VENDOR or m.group(1).startswith("legistar"):
        return None
    return m.group(1)


def origin(client: str) -> str:
    return f"https://{client}.granicus.com"


def view_url(client: str, view_id: int) -> str:
    return f"{origin(client)}/ViewPublisher.php?view_id={int(view_id)}"


def absolute(url: str, base: str) -> str:
    url = (url or "").strip()
    return "https:" + url if url.startswith("//") else urljoin(base, url)


@dataclass
class Row:
    """One meeting row of a view."""
    id: str  # clip-<n> or event-<n>
    view_id: int
    name: str
    when: date | None
    category: str = ""
    links: list[tuple[str, str]] = field(default_factory=list)  # (absolute url, link text)


class _View(HTMLParser):
    """A ViewPublisher page: its title, category headings (CollapsiblePanelTab) and every table row with its cells
    and links. Text in a hidden span (the sort key Granicus puts before a date) is left out."""

    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base, self.title, self.rows, self.category = base, "", [], ""
        self._title = False
        self._skip = 0
        self._cat: list | None = None  # [tag, depth, parts] while inside a category heading
        self._row: dict | None = None
        self._cell: dict | None = None
        self._link: list | None = None
        self._hidden = 0

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in ("script", "style"):
            self._skip += 1
            return
        if tag == "title":
            self._title = True
        if self._cat is not None and tag == self._cat[0]:
            self._cat[1] += 1
        elif self._cat is None and "CollapsiblePanelTab" in a.get("class", ""):
            self._cat = [tag, 1, []]
        if tag == "tr":
            self._row = {"cells": [], "links": [], "category": self.category}
        elif tag in ("td", "th") and self._row is not None:
            self._cell = {"attrs": a, "parts": []}
            self._row["cells"].append(self._cell)
        elif tag == "span" and self._cell is not None and re.search(r"display\s*:\s*none", a.get("style", ""), re.I):
            self._hidden += 1
        elif tag == "span" and self._hidden:
            self._hidden += 1
        elif tag == "a" and self._row is not None:
            urls = []
            href = a.get("href", "")
            if href and not SKIP_LINK.search(href):
                urls.append(absolute(href, self.base))
            for u in re.findall(r"""(?:https?:)?//[^\s'"()]+""", a.get("onclick", "")):
                urls.append(absolute(u, self.base))
            self._link = [urls, []]

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
            return
        if tag == "title":
            self._title = False
        if self._cat is not None and tag == self._cat[0]:
            self._cat[1] -= 1
            if self._cat[1] == 0:
                self.category = clean("".join(self._cat[2]))
                self._cat = None
        if tag == "span" and self._hidden:
            self._hidden -= 1
        elif tag == "a" and self._link is not None and self._row is not None:
            text = clean("".join(self._link[1]))
            for u in self._link[0]:
                self._row["links"].append((u, text))
            self._link = None
        elif tag in ("td", "th"):
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row, self._cell = None, None

    def handle_data(self, data):
        if self._skip:
            return
        if self._title:
            self.title += data
        if self._cat is not None:
            self._cat[2].append(data)
        if self._link is not None:
            self._link[1].append(data)
        if self._cell is not None and not self._hidden:
            self._cell["parts"].append(data)


def _cell(row: dict, *names: str) -> str | None:
    for cell in row["cells"]:
        headers = cell["attrs"].get("headers", "").split()
        if headers and headers[0] in names:
            return clean("".join(cell["parts"]))
    return None


def parse_view(html: str, client: str, view_id: int) -> tuple[str, list[Row]]:
    """(the view's title, its meeting rows). A row is a meeting when one of its links carries a clip_id or event_id."""
    reader = _View(view_url(client, view_id))
    reader.feed(html or "")
    reader.close()
    out = []
    for r in reader.rows:
        ids = {"clip": None, "event": None}
        for u, _ in r["links"]:
            for kind in ids:
                m = re.search(rf"[?&]{kind}_id=(\d+)|/player/{kind}/(\d+)", u)
                if m and not ids[kind]:
                    ids[kind] = m.group(1) or m.group(2)
        if not (ids["clip"] or ids["event"]):
            continue
        texts = [clean("".join(c["parts"])) for c in r["cells"]]
        name = _cell(r, "Name", "EventName") or next(
            (clean("".join(c["parts"])) for c in r["cells"] if c["attrs"].get("scope") == "row"), None) \
            or next((t for t in texts if t), "")
        when = parse_date(_cell(r, "Date", "EventDate")) or parse_date(" ".join(texts)) or parse_date(name)
        rid = f"clip-{ids['clip']}" if ids["clip"] else f"event-{ids['event']}"
        out.append(Row(rid, view_id, name, when, r["category"], list(dict.fromkeys(r["links"]))))
    return clean(reader.title), out


def link_kind(url: str, text: str) -> tuple[str, str | None] | None:
    """(kind, format) of a row's link, None for links that are not documents (feeds, Windows Media, search)."""
    path = urlsplit(url).path.lower()
    host = (urlsplit(url).hostname or "").lower()
    if SKIP_LINK.search(url):
        return None
    if path.endswith("/agendaviewer.php"):
        return "agenda", None
    if path.endswith("/generatedagendaviewer.php"):
        return "agenda", "html"
    if path.endswith("/minutesviewer.php") or re.search(r"/services/minutes/reports/[^/]+/attachment", path):
        return "minutes", None
    if path.endswith("/mediaplayer.php") or "/player/clip/" in path or "/player/event/" in path:
        return "video", "html"
    if host.startswith("archive-video") or re.search(r"\.(mp4|mp3|m4a|m3u8)$", path):
        return "video", path.rsplit(".", 1)[-1]
    if path.endswith("/metaviewer.php") or re.search(r"\.(pdf|docx?|xlsx?|pptx?|html?)$", path):
        kind = doc_kind(text, url)
        return ("packet" if kind == "other" and "cloudfront" in host and "packet" in text.lower() else kind), \
            (path.rsplit(".", 1)[-1] if re.search(r"\.(pdf|docx?|xlsx?|pptx?)$", path) else None)
    if client_of(url) and path.endswith(".php"):
        return None  # other Granicus pages (search, event pages) are not documents
    kind = doc_kind(text, url)
    return (kind, None) if kind != "other" else None


class _Agenda(HTMLParser):
    """A GeneratedAgendaViewer page: numbered rows (td.numberspace + the next cell) and anchors named agendaN are items;
    anchors named documentN (MetaViewer links) are attachments of the item above them, or of the meeting before the
    first item; text between items is the item's text (notes with motions and votes)."""

    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base, self.items, self.meeting_docs = base, [], []
        self._skip = 0
        self._row: dict | None = None  # the open table row: {"num": parts|None, "title": parts|None, "stage"}
        self._td = None  # "num", "title" or None
        self._anchor: dict | None = None  # {"name", "href", "parts"}
        self._text: list[str] = []  # free text since the last item

    def _flush_text(self):
        if self.items and self._text:
            text = clean(" ".join(self._text))
            if text:
                self.items[-1]["text"] = clean((self.items[-1].get("text") or "") + " " + text)
        self._text = []

    def _new_item(self, number: str | None, title: str, meta: str | None):
        self._flush_text()
        self.items.append({"number": number, "title": title, "meta": meta, "attachments": [], "text": ""})

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "tr":
            self._row = {"num": None, "title": None, "meta": None, "seen_num": False}
        elif tag == "td" and self._row is not None:
            if "numberspace" in a.get("class", ""):
                self._td, self._row["num"], self._row["seen_num"] = "num", [], True
            elif self._row["seen_num"] and self._row["title"] is None:
                self._td, self._row["title"] = "title", []
            else:
                self._td = None
        elif tag == "a":
            name = a.get("name", "")
            self._anchor = {"name": name, "href": absolute(a.get("href", ""), self.base) if a.get("href") else "",
                            "parts": []}
        elif tag == "br":
            self._text.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag == "a" and self._anchor is not None:
            anchor, self._anchor = self._anchor, None
            text = clean("".join(anchor["parts"]))
            if anchor["name"].startswith("document") or "MetaViewer.php" in anchor["href"]:
                doc = {"title": text, "url": anchor["href"]}
                (self.items[-1]["attachments"] if self.items else self.meeting_docs).append(doc)
            elif anchor["name"].startswith("agenda"):
                if self._row is not None and self._row["seen_num"]:
                    self._row["meta"] = anchor["name"][6:]
                else:
                    self._new_item(None, text, anchor["name"][6:])
        elif tag == "td":
            self._td = None
        elif tag == "tr" and self._row is not None:
            row, self._row = self._row, None
            number = clean("".join(row["num"] or [])).rstrip(".") or None
            title = clean("".join(row["title"] or []))
            if row["seen_num"] and (number or title):
                self._new_item(number, title, row["meta"])

    def handle_data(self, data):
        if self._skip:
            return
        if self._anchor is not None:
            self._anchor["parts"].append(data)
            if not self._anchor["name"].startswith("agenda"):
                return
        if self._row is not None and self._td == "num":
            self._row["num"].append(data)
        elif self._row is not None and self._td == "title":
            self._row["title"].append(data)
        elif self._row is None and self._anchor is None:
            self._text.append(data)

    def close(self):
        super().close()
        self._flush_text()


ACTION = re.compile(r"\b(action|motion|moved by|roll call|vote)\b", re.I)
RESULT = re.compile(r"\b(passed|carried|failed|approved|adopted|denied|tabled|ayes?|unanimous(ly)?)\b", re.I)


def outcome_of(text: str | None) -> str | None:
    """The action taken, from an item's notes: the text from the first Action/Motion/Vote marker, when a result
    (passed, carried, approved, ayes, ...) follows it. Background text that only mentions an earlier adoption is not
    an outcome."""
    m = ACTION.search(text or "")
    if not m or not RESULT.search(text[m.start():]):
        return None
    return text[m.start():][:400]
TOP = re.compile(r"^(\d+[A-Z]?|[A-Z]|[IVX]+)$")


def parse_agenda(html: str, base: str, meeting_id: str) -> tuple[list[Item], list[dict]]:
    """(items, the meeting's own documents listed before the first item) of a generated agenda."""
    reader = _Agenda(base)
    reader.feed(html or "")
    reader.close()
    items, section = [], None
    for raw in reader.items:
        number = raw["number"]
        top = number is None or bool(TOP.match(number))
        text = raw.get("text") or None
        atts = [Document(d["url"], "item", d["title"], meeting_id, number) for d in raw["attachments"] if d["url"]]
        items.append(Item(meeting_id, number, raw["title"], None if top else section, atts,
                          outcome=outcome_of(text), text=text[:4000] if text else None))
        if top and raw["title"]:
            section = f"{number}. {raw['title']}" if number else raw["title"]
    return items, reader.meeting_docs


@register
class Granicus:
    name, transport = "granicus", "http"

    def __init__(self, http: Fetch, resolve: bool = True, fresh: bool = True) -> None:
        self.http, self.resolve, self.fresh = http, resolve, fresh
        self.hints: dict[str, set[int]] = {}  # view ids seen on a body's own page
        self._rows: dict[tuple[str, str], Row] = {}

    # ------------------------------------------------------------------ detect
    def detect(self, url: str, html: str | None = None) -> str | None:
        client = client_of(url)
        if client:
            self._hint(client, url)
            return client
        for pattern in (EMBED, ANY_LINK):
            for m in pattern.finditer(html or ""):
                client = m.group(1).lower()
                if client in VENDOR or client.startswith("legistar"):
                    continue
                self._hint(client, m.group(0))
                return client
        return None

    def _hint(self, client: str, url: str) -> None:
        m = re.search(r"[?&]view_id=(\d+)", url)
        if m:
            self.hints.setdefault(client, set()).add(int(m.group(1)))

    # ------------------------------------------------------------------ meetings
    def _get_view(self, client: str, view_id: int, fresh: bool):
        return self.http.get(view_url(client, view_id), use_cache=not fresh)

    def views(self, client: str, fresh: bool | None = None) -> tuple[list[tuple[int, str, str]], list[str]]:
        """The client's public views as (view id, title, page), and notes on what could not be read."""
        fresh = self.fresh if fresh is None else fresh
        found, notes, misses, errors, tried = [], [], 0, 0, set()
        hints = sorted(self.hints.get(client, ()))
        candidates = list(range(1, MAX_VIEW + 1))
        for view_id in candidates + [h for h in hints if h > MAX_VIEW]:
            if view_id in tried:
                continue
            stop = (found and misses >= MISS_AFTER_FOUND) or (not found and misses >= MISS_NONE_FOUND)
            if stop and view_id not in hints:
                continue
            tried.add(view_id)
            r = self._get_view(client, view_id, fresh)
            final = r.final_url or ""
            if r.bot_wall:
                raise PortalError(f"granicus {client}: bot wall at view {view_id} ({r.status or r.error})")
            if "/core/error/NotFound.aspx" in final:
                raise PortalError(f"granicus {client}: not a Granicus client (Granicus answers its not-found page)")
            if r.status == 404 or (r.ok and "/account/login" in final):
                misses += 1
                continue
            if not r.ok:
                errors += 1
                notes.append(f"view {view_id} not read: {r.status or r.error}")
                if errors >= 2:
                    notes.append("stopped probing views after two errors in a row")
                    break
                continue
            errors, misses = 0, 0
            text = r.body.decode("utf-8", errors="replace")
            title = clean(re.sub(r"(?is).*?<title>(.*?)</title>.*", r"\1", text[:20000])) if "<title>" in text[:20000].lower() else ""
            title = re.sub(r"\s*-\s*Granicus Content$", "", title)
            if SKIP_VIEW.search(title):
                notes.append(f"view {view_id} ({title}) skipped: training")
                continue
            found.append((view_id, title, text))
        return found, notes

    def meetings(self, client: str, since: date, until: date) -> Listing:
        views, notes = self.views(client)
        if not views:
            raise PortalError(f"granicus {client}: no public ViewPublisher view found (view ids 1 to {MAX_VIEW} probed"
                              + (f"; {'; '.join(notes)}" if notes else "") + ")")
        rows: dict[str, Row] = {}
        for view_id, title, html in views:
            _, parsed = parse_view(html, client, view_id)
            notes.append(f"view {view_id} ({title or 'untitled'}): {len(parsed)} meetings listed")
            for row in parsed:
                rows.setdefault(row.id, row)
        clips = {(r.when, board_from(r.name).lower()) for r in rows.values() if r.id.startswith("clip-")}
        undated, meetings = 0, []
        for row in rows.values():
            if row.id.startswith("event-") and (row.when, board_from(row.name).lower()) in clips:
                continue  # the event's recording is listed as a clip
            if row.when is None:
                undated += 1
                continue
            if not since <= row.when <= until:
                continue
            board = board_from(row.category) or board_from(row.name)
            governing = is_governing(board)
            if governing is None:
                governing = is_governing(row.name)
            number = row.id.split("-", 1)[1]
            url = (f"{origin(client)}/MediaPlayer.php?view_id={row.view_id}&clip_id={number}" if row.id.startswith("clip-")
                   else f"{origin(client)}/AgendaViewer.php?view_id={row.view_id}&event_id={number}")
            meetings.append(Meeting(row.id, row.when, row.name, board, meeting_kind(row.name, row.category, governing=governing),
                                    governing, url))
            self._rows[(client, row.id)] = row
        if undated:
            notes.append(f"{undated} rows without a readable date left out")
        dated = sorted(r.when for r in rows.values() if r.when)
        if dated:
            notes.append(f"the views list meetings from {dated[0]} to {dated[-1]}")
        meetings.sort(key=lambda m: (m.date, m.id))
        return Listing(meetings, notes)

    # ------------------------------------------------------------------ documents
    def _row(self, client: str, meeting: Meeting) -> Row:
        row = self._rows.get((client, meeting.id))
        if row:
            return row
        q = parse_qs(urlsplit(meeting.url or "").query)
        view_ids = [int(q["view_id"][0])] if q.get("view_id") else [v for v, _, _ in self.views(client, fresh=False)[0]]
        for view_id in view_ids:
            r = self._get_view(client, view_id, fresh=False)
            if not r.ok:
                raise PortalError(f"granicus {client}: view {view_id} not read: {r.status or r.error}")
            for found in parse_view(r.body.decode("utf-8", errors="replace"), client, view_id)[1]:
                self._rows.setdefault((client, found.id), found)
        row = self._rows.get((client, meeting.id))
        if row is None:
            raise PortalError(f"granicus {client}: meeting {meeting.id} is no longer listed")
        return row

    def _open(self, url: str):
        """Follow a viewer link: (the file's own address, format, the response) or (url, None, response) when it
        cannot be resolved."""
        r = self.http.get(url)
        final = r.final_url or url
        if not r.ok:
            return url, None, r
        split = urlsplit(final)
        if split.hostname == "docs.google.com" and parse_qs(split.query).get("url"):
            inner = parse_qs(split.query)["url"][0]
            ext = re.search(r"\.(pdf|docx?|xlsx?|pptx?)$", urlsplit(inner).path, re.I)
            return inner, ext.group(1).lower() if ext else None, r
        ctype = (r.content_type or "").lower()
        if "html" in ctype:
            return final, "html", r
        fmt = "pdf" if "pdf" in ctype else (ctype.split("/")[-1].split(";")[0] or None)
        return (url if SIGNED.search(final) else final), fmt, r

    def documents(self, client: str, meeting: Meeting) -> list[Document]:
        row = self._row(client, meeting)
        docs, seen = [], set()

        def add(url, kind, title, fmt):
            if url and url not in seen:
                seen.add(url)
                docs.append(Document(url, kind, title, meeting.id, None, fmt))

        for url, text in row.links:
            kind_fmt = link_kind(url, text)
            if not kind_fmt:
                continue
            kind, fmt = kind_fmt
            viewer = re.search(r"/(AgendaViewer|MinutesViewer)\.php", url, re.I)
            if self.resolve and viewer:
                final, fmt, r = self._open(url)
                add(final, kind, text or kind.title(), fmt)
                if r.ok and "GeneratedAgendaViewer" in (r.final_url or ""):
                    for d in parse_agenda(r.body.decode("utf-8", errors="replace"), r.final_url, meeting.id)[1]:
                        add(d["url"], doc_kind(d["title"], d["url"]), d["title"], None)
            else:
                add(url, kind, text or kind.title(), fmt)
        return docs

    # ------------------------------------------------------------------ items
    def items(self, client: str, meeting: Meeting) -> list[Item]:
        row = self._row(client, meeting)
        agenda = next((u for u, _ in row.links if re.search(r"/(Generated)?AgendaViewer\.php", u, re.I)), None)
        if not agenda:
            return []
        r = self.http.get(agenda)
        if r.bot_wall:
            raise PortalError(f"granicus {client}: bot wall at the agenda of {meeting.id}")
        if not r.ok or "GeneratedAgendaViewer" not in (r.final_url or agenda) or "html" not in (r.content_type or ""):
            return []  # a PDF agenda (or an unreachable one): the portal publishes no items for it
        return parse_agenda(r.body.decode("utf-8", errors="replace"), r.final_url or agenda, meeting.id)[0]
