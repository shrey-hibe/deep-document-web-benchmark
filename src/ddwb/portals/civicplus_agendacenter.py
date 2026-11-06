"""CivicPlus AgendaCenter: the agenda page of a city or county's own CivicPlus site, at <site>/AgendaCenter.

It lives on thousands of bodies' own hosts, so the client id is the site's host (without "www."), and `detect` knows it
by the path (/AgendaCenter) or by the page (its rows, its file links, or a link to another site's AgendaCenter).
Two plain requests give everything:
  GET  /AgendaCenter                        every category (board, committee) with the years it has, and the rows of
                                            the year each category shows: date, posting time, title, agenda, minutes,
                                            media, and the downloads list (packet, HTML agenda, ...)
  POST /AgendaCenter/UpdateCategoryList     the rows of one category for one year. The site answers 400 unless it gets
                                            every field its own script sends (year, catID, startDate, endDate, term,
                                            prevVersionScreen), so all are sent, empty as the script sends them.
The site's cross-category search returns only the current year, so each category is read year by year.
Files are /AgendaCenter/ViewFile/Agenda/_MMDDYYYY-<id> and /AgendaCenter/ViewFile/Minutes/_MMDDYYYY-<id>: the
documents themselves.

Cannot: agenda items (the agenda is a file; [] here), which board governs (read from the category's name), posting
times (on the page, not in the shapes). A category's years come from its own year links: a year the page does not link
is not read. Checked on Vernon Hills, IL (village), Williamson County, TN (county) and Beverly, MA (city), 2026-11-05.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from html import unescape
from urllib.parse import urljoin, urlsplit

from . import civic_common as cc
from .base import Document, Fetch, Item, Listing, Meeting, PortalError
from .registry import register

_PATH = re.compile(r"/AgendaCenter(?:[/?#]|$)", re.I)
_MARKERS = re.compile(r'class="catAgendaRow"|/AgendaCenter/ViewFile/|id="AgendaCenterContent"|/AgendaCenter/UpdateCategoryList', re.I)
_LINK = re.compile(r'(?:href|src|action)\s*=\s*["\']([^"\']*/AgendaCenter\b[^"\']*)["\']', re.I)
_CATEGORY = re.compile(r'<div[^>]*\bid="cat(\d+)"[^>]*>\s*<h2[^>]*>(.*?)</h2>', re.S)
_SECTION = re.compile(r'<span id="section(\d+)">')
_YEAR = re.compile(r"<li(?P<current>\s+class=\"?current\"?)?\s*>\s*<a[^>]*changeYear\((\d{4}),\s*(\d+)\s*,")
_ANY_YEAR = re.compile(r"changeYear\((\d{4}),\s*(\d+)\s*,")
_ROW = re.compile(r'<tr[^>]*class="catAgendaRow"[^>]*>')
_DATE = re.compile(r'aria-label="Agenda for ([A-Z][a-z]+ \d{1,2}, \d{4})"')
_AGENDA = re.compile(r'<a[^>]*\bhref="(?P<href>[^"]*/ViewFile/Agenda/(?P<file>_(?P<mmddyyyy>\d{8})-(?P<id>\d+))[^"]*)"[^>]*>(?P<text>.*?)</a>', re.S)
_CELL = re.compile(r'<td class="(?P<cell>minutes|media)"[^>]*>(?P<body>.*?)</td>', re.S)
_HREF = re.compile(r'href="([^"]+)"')
_DOWNLOAD = re.compile(r'<li>\s*<a(?P<attrs>[^>]*)\bhref="(?P<href>[^"]+)"[^>]*>(?P<label>.*?)</a>\s*</li>', re.S)
_DEEP = re.compile(r"-(\d+)/\?#_?(\d{8})-(\d+)$")


def _text(fragment: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", fragment or "")).split())


def host_of(url: str) -> str | None:
    host = (urlsplit(url if "//" in url else f"https://{url}").hostname or "").lower()
    return host.removeprefix("www.") or None


@dataclass
class Row:
    id: str  # the agenda id, e.g. "1460"
    file_key: str  # "_10062026-1460"
    category: str
    date: date
    title: str
    agenda_url: str
    minutes_url: str | None = None
    media: list[str] = field(default_factory=list)
    downloads: list[tuple[str, str, str]] = field(default_factory=list)  # (label, url, css class)


@dataclass
class Page:
    categories: dict[str, str] = field(default_factory=dict)  # id -> name
    years: dict[str, list[int]] = field(default_factory=dict)  # id -> the years the page links to
    shown: dict[str, int] = field(default_factory=dict)  # id -> the year whose rows are on the page
    rows: list[Row] = field(default_factory=list)


def parse_page(html: str, origin: str, category: str | None = None) -> Page:
    """The main page or an UpdateCategoryList fragment -> categories, their years, and the rows shown. A row belongs to
    the category whose section precedes it (or to `category`, for a fragment without one)."""
    page = Page(categories={c: _text(n) for c, n in _CATEGORY.findall(html)})
    for m in _ANY_YEAR.finditer(html):
        years = page.years.setdefault(m.group(2), [])
        if int(m.group(1)) not in years:
            years.append(int(m.group(1)))
    for m in _YEAR.finditer(html):
        if m.group("current"):
            page.shown[m.group(3)] = int(m.group(2))
    marks = sorted([(m.start(), "section", m.group(1)) for m in _SECTION.finditer(html)]
                   + [(m.start(), "row", m.end()) for m in _ROW.finditer(html)])
    starts = [m.start() for m in _ROW.finditer(html)] + [len(html)]
    current = category
    for position, what, value in marks:
        if what == "section":
            current = value
            continue
        end = next(s for s in starts if s > position)
        row = _row(html[value:end], current or "", origin)
        if row is not None:
            page.rows.append(row)
    return page


def _row(chunk: str, category: str, origin: str) -> Row | None:
    chunk = chunk.split("</tbody>", 1)[0]
    agenda = _AGENDA.search(chunk)
    if not agenda:
        return None
    when = _DATE.search(chunk)
    if when:
        day = datetime.strptime(when.group(1), "%B %d, %Y").date()
    else:  # the file key carries the date too
        d = agenda.group("mmddyyyy")
        day = date(int(d[4:]), int(d[:2]), int(d[2:4]))
    cells = {m.group("cell"): m.group("body") for m in _CELL.finditer(chunk)}
    minutes = _HREF.search(cells.get("minutes", ""))
    absolute = lambda href: urljoin(origin + "/", unescape(href))  # noqa: E731
    title = re.sub(r"\s*\((?:PDF|DOCX?|HTML)\)\s*$", "", _text(agenda.group("text")), flags=re.I)
    return Row(id=agenda.group("id"), file_key=agenda.group("file"), category=category, date=day, title=title or "Agenda",
               agenda_url=absolute(agenda.group("href")),
               minutes_url=absolute(minutes.group(1)) if minutes else None,
               media=list(dict.fromkeys(absolute(h) for h in _HREF.findall(cells.get("media", "")) if h.strip() not in ("", "#"))),
               downloads=[(_text(m.group("label")), absolute(m.group("href")),
                           (re.search(r'class="([^"]*)"', m.group("attrs")) or [None, ""])[1])
                          for m in _DOWNLOAD.finditer(chunk)])


def governing_of(title: str, category: str) -> bool | None:
    """A category can hold two boards ("Village Board & Committee of the Whole"), and a council's category its
    committees: a title naming the governing board says True, a title naming a committee says False; else the
    category's name says, else whatever the title says."""
    by_title = cc.governing_of(title)
    if by_title or re.search(r"committee", title or "", re.I):
        return by_title
    by_category = cc.governing_of(category)
    return by_category if by_category is not None else by_title


def deep_link(origin: str, category_name: str, category: str, row: Row) -> str:
    """The page's own address for one row: /AgendaCenter/<category>-<catID>/?#_<MMDDYYYY>-<agenda id>, which the site's
    script opens on that row (it reads the category from before "/?#" and the agenda id from the end)."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", category_name).strip("-") or "Category"
    return f"{origin}/AgendaCenter/{slug}-{category}/?#{row.file_key}"


def _format(css: str) -> str | None:
    """The download link's class names the file's format ("pdf", "html")."""
    first = (css or "").split()[:1]
    return first[0].lower() if first and first[0].lower() in ("pdf", "html", "doc", "docx") else None


def _base_title(title: str) -> str:
    """A row's title without the words that tell its files apart: "Village Board Meeting Full Packet" and "Village Board
    Meeting" are one meeting posted as two rows."""
    t = re.sub(r"\((?:pdf|docx?|html)\)|\b(?:full\s+)?packet\b|\bagenda\b|\bmeeting\b|\bamended\b|\brevised\b|\bupdated\b",
               " ", (title or "").lower())
    return re.sub(r"[^a-z0-9]+", "", t)


def group_rows(rows: list[Row]) -> list[list[Row]]:
    """Rows of one meeting together: same category, same day, same title once the file words are taken out. The row
    that is not a packet comes first and gives the meeting its id and title."""
    groups: dict[tuple, list[Row]] = {}
    for r in sorted(rows, key=lambda r: (r.date, int(r.id))):
        groups.setdefault((r.category, r.date, _base_title(r.title) or r.id), []).append(r)
    return [sorted(g, key=lambda r: ("packet" in r.title.lower(), int(r.id))) for g in groups.values()]


def documents_of(rows: list[Row], meeting_id: str) -> list[Document]:
    """The documents of one meeting's rows: agenda (a row titled as a packet gives the packet), minutes, the downloads
    list (packet, HTML agenda), media. "Previous versions" is a page of older files, not a document: left out."""
    docs: list[Document] = []
    seen: set[str] = set()

    def add(url: str, kind: str, title: str, fmt: str | None = None) -> None:
        if url and url not in seen and "/PreviousVersions/" not in url:
            seen.add(url)
            docs.append(Document(url=url, kind=kind, title=title, meeting_id=meeting_id, format=fmt))

    for row in rows:
        fmt = {url: ("html" if "html=true" in url.lower() else _format(css)) for _, url, css in row.downloads}
        main = row.agenda_url.split("?", 1)[0]  # the file itself; the title link is sometimes its HTML view
        add(main, "packet" if "packet" in row.title.lower() else "agenda", row.title, fmt.get(main, "pdf" if main != row.agenda_url else None))
        if row.minutes_url:
            add(row.minutes_url, "minutes", f"{row.title} (minutes)")
        for label, url, _ in [("", row.agenda_url, "")] + row.downloads:
            low = url.lower()
            kind = ("packet" if "packet=true" in low else "minutes" if "/viewfile/minutes/" in low
                    else "agenda" if "/viewfile/agenda/" in low else cc.document_kind(label))
            add(url, kind, f"{row.title} ({label or ('HTML' if 'html=true' in low else kind)})", fmt.get(url))
        for url in row.media:
            add(url, "video", f"{row.title} (media)")
    return docs


@register
class AgendaCenter:
    name, transport = "civicplus_agendacenter", "http"

    def __init__(self, http: Fetch) -> None:
        self.http = http
        self._origin: dict[str, str] = {}  # client -> https://<host> that answered (after redirects)
        self._groups: dict[tuple[str, str], list[Row]] = {}  # (client, meeting id) -> the meeting's rows

    # ------------------------------------------------------------------ detect
    def detect(self, url: str, html: str | None = None) -> str | None:
        url = url or ""
        if _PATH.search(urlsplit(url if "//" in url else f"https://{url}").path or ""):
            return host_of(url)
        if not html:
            return None
        own = host_of(url) if url else None
        if own and _MARKERS.search(html):
            return own
        hosts = Counter(host_of(urljoin(url or "https://example.invalid/", unescape(m.group(1)))) for m in _LINK.finditer(html))
        hosts.pop(None, None)
        hosts.pop("example.invalid", None)
        if not hosts:
            return None
        return own if own in hosts else hosts.most_common(1)[0][0]

    # ------------------------------------------------------------------ requests
    def _main(self, client: str) -> tuple[str, str]:
        """The AgendaCenter main page and the origin that served it (the bare host, else www.)."""
        tries = [client] if client.startswith("www.") else [client, f"www.{client}"]
        last = None
        for host in tries:
            resp = cc.fetch(self.http, f"https://{host}/AgendaCenter")
            if resp.ok:
                final = urlsplit(resp.final_url or resp.url)
                origin = f"{final.scheme}://{final.hostname}"
                self._origin[client] = origin
                return resp.text, origin
            last = resp
            if resp.bot_wall:
                break
        raise PortalError(f"civicplus_agendacenter {client}: /AgendaCenter refused: {cc.refusal(last)}")

    def _year(self, origin: str, category: str, year: int):
        return cc.fetch(self.http, f"{origin}/AgendaCenter/UpdateCategoryList", post=True,
                        data={"year": str(year), "catID": str(category), "startDate": "", "endDate": "", "term": "",
                              "prevVersionScreen": "false"},
                        headers={"X-Requested-With": "XMLHttpRequest"})

    # ------------------------------------------------------------------ meetings
    def meetings(self, client: str, since: date, until: date) -> Listing:
        html, origin = self._main(client)
        main = parse_page(html, origin)
        notes: list[str] = []
        if not main.categories:
            if not _MARKERS.search(html) and "AgendaCenter" not in html:
                raise PortalError(f"civicplus_agendacenter {client}: {origin}/AgendaCenter is not an AgendaCenter page")
            notes.append("the AgendaCenter page lists no categories (empty, or the site moved its agendas)")
        rows: dict[str, Row] = {r.id: r for r in main.rows}
        wall = None
        for category, name in main.categories.items():
            for year in sorted(main.years.get(category, [])):
                if not since.year <= year <= until.year or year == main.shown.get(category):
                    continue
                if wall:
                    notes.append(f"{name} {year}: not read: the site stopped us earlier ({wall})")
                    continue
                resp = self._year(origin, category, year)
                if not resp.ok:
                    notes.append(f"{name} {year}: not read: {cc.refusal(resp)}")
                    wall = cc.refusal(resp) if resp.bot_wall else None
                    continue
                for r in parse_page(resp.text, origin, category).rows:
                    rows.setdefault(r.id, r)
        meetings = []
        for group in group_rows([r for r in rows.values() if since <= r.date <= until]):
            first = group[0]
            board = main.categories.get(first.category, f"Category {first.category}")
            governing = governing_of(first.title, board)
            self._groups[(client, first.id)] = group
            meetings.append(Meeting(id=first.id, date=first.date, title=first.title, board=board,
                                    kind=cc.kind_of(first.title, board, governing=governing), governing=governing,
                                    url=deep_link(origin, board, first.category, first)))
        meetings.sort(key=lambda m: (m.date, int(m.id)))
        return Listing(meetings, notes)

    # ------------------------------------------------------------------ one meeting
    def _rows(self, client: str, meeting: Meeting) -> list[Row]:
        known = self._groups.get((client, meeting.id))
        if known:
            return known
        m = _DEEP.search(meeting.url or "")
        if not m:
            raise PortalError(f"civicplus_agendacenter {client}: meeting {meeting.id} has no AgendaCenter address to read it from")
        category, mmddyyyy = m.group(1), m.group(2)
        origin = self._origin.get(client) or self._main(client)[1]
        resp = self._year(origin, category, int(mmddyyyy[4:]))
        if not resp.ok:
            raise PortalError(f"civicplus_agendacenter {client}: category {category} refused: {cc.refusal(resp)}")
        for group in group_rows(parse_page(resp.text, origin, category).rows):
            if any(r.id == meeting.id for r in group):
                self._groups[(client, meeting.id)] = group
                return group
        raise PortalError(f"civicplus_agendacenter {client}: agenda {meeting.id} is no longer listed in category {category}")

    def documents(self, client: str, meeting: Meeting) -> list[Document]:
        return documents_of(self._rows(client, meeting), meeting.id)

    def items(self, client: str, meeting: Meeting) -> list[Item]:
        return []  # AgendaCenter publishes the agenda as a file, not as items
