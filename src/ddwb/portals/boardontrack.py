"""BoardOnTrack (app2.boardontrack.com): charter school boards. The public portal looks like a script-built app but is
rendered on the server (Grails): plain GETs with our research User-Agent read it all; no browser.

Pages and endpoints read (client = the tenant's slug, the second path segment of every public URL: K8WqfA; the portal
answers the slug in any case, so the registry's lower-cased ids work, and URLs are built with the case the portal uses):
  /public/<slug>/year?syid=<YYYY-MM>&et=b|c     one school year of meetings: et=b the board's, et=c committees' (et=a
                                                both). Each line item: the date in three spans (Aug / 05 / 2026), the
                                                title, the time, and links to the agenda (/public/<slug>/agenda/<n>)
                                                and minutes (/public/<slug>/minutes/<n>) pages. The page links the
                                                previous and next school years; syid is the year's first month
  /public/<slug>/meeting/<id>                   one meeting's links (used when documents() is asked about a meeting it
                                                did not list)
  /org/<slug>/agenda/downloadAgendaPdf/<id>     the agenda PDF, by meeting id
  /org/<slug>/minutes/downloadPdf/<id>          the minutes PDF, by meeting id
  /public/<slug>/agenda/<n>                     the agenda as HTML: sections ("I. Opening Items"), items ("A. Approve
                                                Minutes") with purpose, presenter, description and any links
  /public/<slug>/minutes/<n>                    the minutes as HTML, item by item: items() takes each item's minutes text
                                                (motions, votes, discussion) as its outcome

What it cannot do: attachments the board pack holds are not published item by item on the public agenda (only links
written into an item's description are); minutes may be drafts (the minutes page says "Draft" or "Approved"; the PDF
link does not). meetings() reads two pages per school year and stops going back after two school years with no
meetings at all (the tenant's start on BoardOnTrack), saying so in its notes.

Reads the slug, the three-span dates and the previous-school-year pager, and fetches the minutes PDF by meeting id. Checked on Vertus Charter School NY (0wwusX), Golden Charter Academy
CA (K8WqfA) and The Exploris School NC (XwlyFd) on 2026-11-05.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from html import unescape
from urllib.parse import urljoin, urlsplit

from .base import Document, Fetch, Item, Listing, Meeting, PortalError
from .registry import register

ORIGIN = "https://app2.boardontrack.com"
_HOST = re.compile(r"(?:^|\.)boardontrack\.com$", re.I)
_SLUG = re.compile(r"^[A-Za-z0-9_-]+$")
_RESERVED = {"agenda", "board", "documents", "home", "meeting", "meetingslist", "minutes", "org", "public",
             "publicportal", "year", "assets"}
_URL_IN_HTML = re.compile(r"https?://(?:[\w-]+\.)?boardontrack\.com/(?:public|org)/[A-Za-z0-9_-]+", re.I)
_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_DATE = re.compile(r"<span[^>]*>\s*([A-Za-z]{3,9})\s*</span>\s*<span[^>]*>\s*(\d{1,2})\s*</span>\s*<span[^>]*>\s*(\d{4})\s*</span>")
_SYID = re.compile(r"syid=(\d{4})-(\d{2})")


def slug_of(url: str) -> str | None:
    """The tenant slug from any boardontrack.com /public/<slug>/... or /org/<slug>/... URL, or None."""
    parts = urlsplit((url or "").strip() if "//" in (url or "") else f"https://{(url or '').strip()}")
    if not _HOST.search(parts.hostname or ""):
        return None
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) < 2 or segments[0].lower() not in ("public", "org"):
        return None
    slug = segments[1]
    return slug if _SLUG.match(slug) and slug.lower() not in _RESERVED else None


def _text(fragment: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", fragment or "")).split())


@dataclass
class Row:
    meeting_id: str
    when: date | None
    title: str
    starts_at: str | None
    agenda_id: str | None
    minutes_id: str | None
    committee: bool


def parse_year_page(html: str, committee: bool) -> list[Row]:
    """One school-year page -> its meetings (line items) in page order."""
    rows = []
    for chunk in re.split(r'class="twp-py-3 line-item\b', html or "")[1:]:
        meeting = re.search(r"/meeting/(\d+)", chunk)
        if not meeting:
            continue
        when = None
        d = _DATE.search(chunk)
        if d and d.group(1)[:3].lower() in _MONTHS:
            try:
                when = date(int(d.group(3)), _MONTHS[d.group(1)[:3].lower()], int(d.group(2)))
            except ValueError:
                when = None
        titles = [_text(t) for t in re.findall(r'/meeting/\d+"[^>]*link-body[^>]*>(.*?)</a>', chunk, re.S)]
        time = re.search(r'<span class="twp-text-base twp-text-subtle">(.*?)</span>', chunk, re.S)
        agenda, minutes = re.search(r"/agenda/(\d+)", chunk), re.search(r"/minutes/(\d+)", chunk)
        title = next((t for t in titles if t), "Meeting")
        if re.search(r"\bcancel(?:l)?ed\b", _text(chunk), re.I) and "cancel" not in title.lower():
            title += " (cancelled)"
        rows.append(Row(meeting.group(1), when, title, _text(time.group(1)) if time else None,
                        agenda.group(1) if agenda else None, minutes.group(1) if minutes else None, committee))
    return rows


def school_year(html: str) -> tuple[int, int] | None:
    """The page's school year (first year, first month), from its "Current School Year" link or, failing that, the
    middle of its previous and next links."""
    current = re.search(r'href="([^"]*)"[^>]*title="Current School Year"', html or "")
    if current:
        m = _SYID.search(unescape(current.group(1)))
        if m:
            return int(m.group(1)), int(m.group(2))
    prev = re.search(r'href="([^"]*)"[^>]*title="Previous School Year"', html or "")
    if prev:
        m = _SYID.search(unescape(prev.group(1)))
        if m:
            return int(m.group(1)) + 1, int(m.group(2))
    return None


def canonical_slug(html: str, slug: str) -> str:
    """The slug as the portal spells it in its own links (the registry lower-cases ids)."""
    m = re.search(rf"/public/({re.escape(slug)})/", html or "", re.I)
    return m.group(1) if m else slug


def organization_name(html: str) -> str:
    m = re.search(r'<meta name="description" content="View the public meetings, agendas and minutes for ([^"]+)"', html or "")
    if m:
        return unescape(m.group(1)).strip()
    m = re.search(r'<h2 class="twp-truncate">(.*?)</h2>', html or "", re.S)
    return _text(m.group(1)) if m else ""


def meeting_kind(row: Row) -> tuple[str, bool]:
    t = row.title.lower()
    if row.committee or re.search(r"\bcommittee\b", t):
        return "committee", False
    if re.search(r"\bspecial\b|\bemergency\b", t):
        return "special", True
    if re.search(r"retreat|workshop|work session|study session|training", t):
        return "work_session", True
    return "regular", True


def parse_agenda(html: str, meeting_id: str, base: str) -> list[Item]:
    """The agenda page's table: section rows ("I." "Opening Items") and item rows ("A." title, purpose), each item's
    description in the plain row after it."""
    body = (html or "")
    start, end = body.find("<tbody>"), body.rfind("</tbody>")
    body = body[start:end] if start >= 0 and end > start else body
    marks = list(re.finditer(r'<tr class="(b3-section-row|b3-item-row)"[^>]*>', body))
    items: list[Item] = []
    section_no, section = "", None
    for i, m in enumerate(marks):
        chunk = body[m.end(): marks[i + 1].start() if i + 1 < len(marks) else len(body)]
        cut = chunk.find("</tr>")
        row, rest = (chunk[:cut], chunk[cut:]) if cut >= 0 else (chunk, "")
        strongs = [_text(s) for s in re.findall(r"<strong>(.*?)</strong>", row, re.S)]
        if m.group(1) == "b3-section-row":
            section_no = (strongs[0] if strongs else "").rstrip(".")
            name = strongs[1] if len(strongs) > 1 else ""
            section = f"{section_no}. {name}".strip(". ") if section_no else name
            continue
        letter = (strongs[0] if strongs else "").rstrip(".")
        cell = re.search(r'<td class="b3-pl-0"[^>]*>(.*?)</td>', row, re.S)
        title = _text(cell.group(1)) if cell else ""
        description = " ".join(_text(d) for d in re.findall(r'<div class="b3-rte-content"[^>]*>(.*?)</div>\s*</td>', rest, re.S))
        number = ".".join(p for p in (section_no, letter) if p) or None
        attachments = [Document(urljoin(base, unescape(href)), "item", _text(label) or "Attachment", meeting_id, item_no=number)
                       for href, label in re.findall(r'<a [^>]*href="([^"]+)"[^>]*>(.*?)</a>', row + rest, re.S)
                       if not href.startswith(("mailto:", "#", "javascript:"))]
        items.append(Item(meeting_id=meeting_id, number=number, title=title or "(untitled)", section=section,
                          attachments=attachments, text=description or None))
    return items


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def parse_minutes(html: str) -> dict[tuple[str, str], str]:
    """The minutes page -> {(section name, item title): what the minutes record for that item}. Keyed by names, not
    numbers: the minutes leave out empty sections, so "IV. Closing Items" on the agenda can be "III." in the minutes."""
    out: dict[tuple[str, str], str] = {}
    for sec in re.finditer(r'<div class="item">\s*<h3 class="as-title">(.*?)</h3>(.*?)(?=<div class="item">|<div class="footer-notice|\Z)',
                           html or "", re.S):
        section = re.sub(r"^[IVXLC\d]+\.?\s+", "", _text(sec.group(1)))
        for it in re.finditer(r'class="item-number[^"]*"[^>]*>.*?</h3>\s*<h3[^>]*>(.*?)</h3>(.*?)(?=<div class="minutes-item">|\Z)',
                              sec.group(2), re.S):
            said = re.search(r'<div class="item-minutes">(.*)', it.group(2), re.S)
            text = _text(said.group(1)) if said else ""
            if text:
                out[(_key(section), _key(_text(it.group(1))))] = text[:2000]
    return out


def outcome_for(item: Item, said: dict[tuple[str, str], str]) -> str | None:
    section = re.sub(r"^[IVXLC\d]+\.?\s+", "", item.section or "")
    hit = said.get((_key(section), _key(item.title)))
    if hit is None:
        same_title = [text for (_, title), text in said.items() if title == _key(item.title)]
        hit = same_title[0] if len(same_title) == 1 else None
    return hit


@register
class BoardOnTrack:
    name, transport = "boardontrack", "http"
    EMPTY_YEARS_TO_STOP = 2

    def __init__(self, http: Fetch) -> None:
        self.http = http
        self._rows: dict[tuple[str, str], Row] = {}  # (slug lower, meeting id) -> its line item
        self._slugs: dict[str, str] = {}  # slug lower -> the portal's spelling

    def detect(self, url: str, html: str | None = None) -> str | None:
        slug = slug_of(url)
        if slug or not html:
            return slug
        for m in _URL_IN_HTML.finditer(html):
            slug = slug_of(m.group(0))
            if slug:
                return slug
        return None

    def _get(self, url: str) -> str:
        r = self.http.get(url)
        if r.bot_wall:
            raise PortalError(f"BoardOnTrack stopped at a bot wall or block ({url}, HTTP {r.status}): never solved")
        if not r.ok:
            raise PortalError(f"BoardOnTrack {url}: HTTP {r.status} {r.error or ''}".strip())
        return r.text

    def _slug(self, client: str) -> str:
        return self._slugs.get(client.lower(), client)

    def _year(self, slug: str, first: tuple[int, int], et: str) -> list[Row]:
        return parse_year_page(self._get(f"{ORIGIN}/public/{slug}/year?syid={first[0]}-{first[1]:02d}&et={et}"), et == "c")

    def meetings(self, client: str, since: date, until: date) -> Listing:
        page = self._get(f"{ORIGIN}/public/{client}/year?et=b")
        slug = canonical_slug(page, client)
        self._slugs[client.lower()] = slug
        name = organization_name(page)
        current = school_year(page)
        if current is None:
            raise PortalError(f"BoardOnTrack {client}: the meetings page names no school year (changed format?)")
        year0, month = current
        notes: list[str] = []
        rows: list[Row] = []
        # back from the current school year to the one holding `since`, then forward to the one holding `until`
        empty, y = 0, year0
        while date(y + 1, month, 1) > since:
            found = self._year(slug, (y, month), "b") + self._year(slug, (y, month), "c")
            rows += found
            empty = 0 if found else empty + 1
            if empty >= self.EMPTY_YEARS_TO_STOP and y < year0:
                notes.append(f"stopped at school year {y}-{month:02d}: {empty} school years with no meetings before it")
                break
            y -= 1
        y = year0 + 1
        while date(y, month, 1) <= until:
            found = self._year(slug, (y, month), "b") + self._year(slug, (y, month), "c")
            if not found:
                break
            rows += found
            y += 1
        meetings, seen = [], set()
        for row in rows:
            if row.meeting_id in seen:
                continue
            seen.add(row.meeting_id)
            self._rows[(client.lower(), row.meeting_id)] = row
            if row.when is None:
                notes.append(f"meeting {row.meeting_id} ({row.title}): no date read")
                continue
            if not since <= row.when <= until:
                continue
            kind, governing = meeting_kind(row)
            board = (re.sub(r"\s*meeting\s*$", "", row.title, flags=re.I).strip() or row.title) if row.committee else (name or "Board")
            meetings.append(Meeting(row.meeting_id, row.when, row.title, board=board, kind=kind, governing=governing,
                                    url=f"{ORIGIN}/public/{slug}/meeting/{row.meeting_id}"))
        meetings.sort(key=lambda m: (m.date, m.id))
        return Listing(meetings, notes)

    def _row(self, client: str, meeting: Meeting) -> Row:
        row = self._rows.get((client.lower(), str(meeting.id)))
        if row is None:  # not listed in this session: the meeting's own page has its links
            slug = self._slug(client)
            html = self._get(f"{ORIGIN}/public/{slug}/meeting/{meeting.id}")
            self._slugs[client.lower()] = canonical_slug(html, client)
            agenda, minutes = re.search(r"/agenda/(\d+)", html), re.search(r"/minutes/(\d+)", html)
            row = Row(str(meeting.id), meeting.date, meeting.title, None, agenda.group(1) if agenda else None,
                      minutes.group(1) if minutes else None, meeting.kind == "committee")
            self._rows[(client.lower(), str(meeting.id))] = row
        return row

    def documents(self, client: str, meeting: Meeting) -> list[Document]:
        row = self._row(client, meeting)
        slug = self._slug(client)
        docs = []
        if row.agenda_id:
            docs.append(Document(f"{ORIGIN}/org/{slug}/agenda/downloadAgendaPdf/{row.meeting_id}", "agenda", "Agenda",
                                 row.meeting_id, format="pdf"))
        if row.minutes_id:
            docs.append(Document(f"{ORIGIN}/org/{slug}/minutes/downloadPdf/{row.meeting_id}", "minutes", "Minutes",
                                 row.meeting_id, format="pdf"))
        return docs

    def items(self, client: str, meeting: Meeting) -> list[Item]:
        row = self._row(client, meeting)
        slug = self._slug(client)
        if not row.agenda_id:
            return []
        url = f"{ORIGIN}/public/{slug}/agenda/{row.agenda_id}"
        items = parse_agenda(self._get(url), row.meeting_id, url)
        if row.minutes_id:
            said = parse_minutes(self._get(f"{ORIGIN}/public/{slug}/minutes/{row.minutes_id}"))
            for item in items:
                item.outcome = outcome_for(item, said)
        return items
