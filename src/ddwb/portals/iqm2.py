"""IQM2 (<client>.iqm2.com, Granicus's "Meeting Portal", also branded MinuteTraq): plain GETs, no browser.

Pages read
  /Citizens/Calendar.aspx?From=M/D/YYYY&To=M/D/YYYY&View=List
                              every meeting of every group (board) in the range, one row each: the Detail_Meeting link
                              whose tooltip carries the date, "Board:", "Type:" and "Status:", and the row's download
                              links. Read one calendar year at a time, so a failed year is a note, not a gap in silence
  /Citizens/Detail_Meeting.aspx?ID=N
                              one meeting: the Downloads panel (data-type agenda, agenda_packet, minutes, minutes_package)
                              and the MeetingDetail outline: section rows, numbered items (Detail_LegiFile links) and
                              their attachments one level below
  /Citizens/FileOpen.aspx?Type=T&ID=N&Inline=True
                              the files themselves (agenda, packet, minutes, summary, item attachments): returned as is,
                              they answer with the file. Type numbers vary by client (14 agenda, 1 packet, 12 minutes,
                              15 summary on the clients seen), so kinds come from the link text and data-type
  /Citizens/SplitView.aspx?Mode=Video&MeetingID=N   the video page

What it cannot do
  - IQM2 hosts complete the TLS handshake and then never answer this client: read timeouts on every client tried,
    on robots.txt and the RSS feed too, from an Indian network (2026-11-05) and through the US VPN (2026-11-06), while
    the same pages open in a normal browser. Over plain HTTP alone meetings() raises PortalError naming the timeout
    rather than returning nothing; nav hands the adapter ddwb.portals.browser.Fallback, which moves such a host to the
    browser tier (#123). Checked that way on 2026-11-06 through OpenClaw's Chrome: Jefferson County, AL's calendar
    (141 meetings 2025-26), a meeting's 46 items with 157 files, and a 113-page minutes PDF.
  - Many clients have left IQM2: Olmsted County's calendar ends in 2021, Coweta County's in July 2024 (seen in a
    browser, 2026-11-06). meetings() reads the calendar's year links and says where the listing ends.
  - Votes and item outcomes sit on each Detail_LegiFile page, which items() does not open; items carry no outcome.
  - Meetings whose detail page has no outline give no items.

Checked on: the parsers were built and tested on Wayback Machine captures of olmstedcountymn (MN county, 2018 list and
detail pages) and pleasanthillca (CA city, 2024 capture of a detail page with items and attachments); in a browser
(2026-11-06) cowetacountyga's 2024 list has the same rows (32 meetings, 27 with minutes) and olmstedcountymn's 2026
list is empty with years up to 2021.
"""

from __future__ import annotations

import html as htmllib
import re
from datetime import date
from urllib.parse import urljoin, urlsplit

from .base import Document, Fetch, Item, Listing, Meeting, PortalError
from .meeting_words import clean, doc_kind, is_governing, meeting_kind, parse_date
from .registry import register

HOST = re.compile(r"^([a-z0-9][a-z0-9-]*)\.iqm2\.com$", re.I)
VENDOR = {"www", "prod-app", "app", "api", "help", "support", "static", "cdn", "assets", "login", "admin", "demo"}
EMBED = re.compile(r"(?:https?:)?//([a-z0-9][a-z0-9-]*)\.iqm2\.com(?::\d+)?/(?:Citizens|Services|Web)/[^\s\"'<>]*", re.I)
ANY_LINK = re.compile(r"(?:https?:)?//([a-z0-9][a-z0-9-]*)\.iqm2\.com\b", re.I)
DATA_TYPE = {"agenda": "agenda", "agenda_packet": "packet", "minutes": "minutes", "minutes_package": "minutes"}


def client_of(url: str) -> str | None:
    m = HOST.match((urlsplit(url or "").hostname or "").lower())
    if not m or m.group(1) in VENDOR or re.fullmatch(r"mt\d+", m.group(1)):
        return None
    return m.group(1)


def origin(client: str) -> str:
    return f"https://{client}.iqm2.com"


def calendar_url(client: str, start: date, end: date) -> str:
    return f"{origin(client)}/Citizens/Calendar.aspx?From={start.month}/{start.day}/{start.year}" \
           f"&To={end.month}/{end.day}/{end.year}&View=List"


def detail_url(client: str, meeting_id: str) -> str:
    return f"{origin(client)}/Citizens/Detail_Meeting.aspx?ID={meeting_id}"


def _text(fragment: str) -> str:
    return clean(htmllib.unescape(re.sub(r"(?s)<[^>]+>", " ", fragment or "")))


def _attr(tag: str, name: str) -> str:
    m = re.search(rf"""\b{name}\s*=\s*(?:"([^"]*)"|'([^']*)')""", tag, re.I)
    return htmllib.unescape(m.group(1) if m and m.group(1) is not None else (m.group(2) if m else "") or "")


def _links(fragment: str, base: str) -> list[tuple[str, str]]:
    """(absolute url, text) of a fragment's links; hidden placeholders (empty href) are left out, and a video link's
    window is read from its onclick."""
    out = []
    for m in re.finditer(r"(?is)<a\b([^>]*)>(.*?)</a>", fragment):
        tag, text = m.group(1), _text(m.group(2))
        href = _attr(tag, "href").strip()
        opened = re.search(r"""OpenWindow\(\s*["']([^"']+)""", _attr(tag, "onclick"))
        if opened:
            href = opened.group(1)
        if not href or href.startswith(("#", "javascript:", "mailto:")) or "HiddenDocumentLink" in _attr(tag, "class"):
            continue
        out.append((urljoin(base, href), text))
    return out


def parse_calendar(page: str, client: str) -> list[dict]:
    """The meeting rows of a Calendar.aspx list: id, date, board, type, status, title, links."""
    base = f"{origin(client)}/Citizens/Calendar.aspx"
    rows = []
    for block in re.split(r"""(?i)<div\s+class=["']Row\s+""", page or "")[1:]:
        link = re.search(r"""(?is)<a\b([^>]*href=["'][^"']*Detail_Meeting\.aspx\?ID=(\d+)[^"']*["'][^>]*)>(.*?)</a>""", block)
        if not link:
            continue
        tip = _attr(link.group(1), "title")
        fields = dict((k.strip().lower(), clean(v)) for k, v in re.findall(r"(?m)^\s*(Board|Type|Status)\s*:\s*(.+?)\s*$",
                                                                               tip.replace("\r", "\n")))
        details = re.search(r"""(?is)class=["'][^"']*RowDetails[^"']*["'][^>]*>(.*?)</div>""", block)
        detail_text = _text(details.group(1)) if details else ""
        board = fields.get("board") or detail_text.split(" - ")[0]
        mtype = fields.get("type") or (detail_text.split(" - ", 1)[1] if " - " in detail_text else "")
        when = parse_date(tip) or parse_date(_text(link.group(3)))
        rows.append({"id": link.group(2), "date": when, "board": board, "type": mtype,
                     "status": fields.get("status", ""), "title": detail_text or " - ".join(x for x in (board, mtype) if x),
                     "links": [l for l in _links(block, base) if "Detail_Meeting.aspx" not in l[0]]})
    return rows


def calendar_years(page: str) -> list[int]:
    """The years the calendar offers (its "Date:" links), newest first: the newest is the last year with meetings."""
    span = re.search(r"""(?is)id=["'][^"']*lblCalendarRange["'][^>]*>(.*?)</span>\s*</td>""", page or "")
    years = re.findall(r"From=1/1/(\d{4})&(?:amp;)?To=12/31/\1", span.group(1) if span else page or "")
    return sorted({int(y) for y in years}, reverse=True)


def link_kind(url: str, text: str, data_type: str = "") -> tuple[str, str | None] | None:
    """(kind, format) of a download link; None for links that are not the meeting's documents."""
    path = urlsplit(url).path.lower()
    if path.endswith("/splitview.aspx") or "video" in text.lower() or "audio" in text.lower():
        return "video", "html"
    if not path.endswith("/fileopen.aspx"):
        return None
    if data_type in DATA_TYPE:
        return DATA_TYPE[data_type], None
    if text.strip().lower() == "summary":
        return "minutes", None  # IQM2's public minutes summary
    kind = doc_kind(text, url)
    return (kind if kind != "other" else "other"), None


def parse_detail(page: str, client: str, meeting_id: str) -> tuple[dict, list[Document], list[Item]]:
    """(heading: board, type, date, file format), the Downloads panel's documents, and the outline's items."""
    base = f"{origin(client)}/Citizens/Detail_Meeting.aspx"
    head = {}
    for key in ("MeetingGroup", "MeetingType", "MeetingDate", "MeetingStatus"):
        m = re.search(rf"""(?is)id=["'][^"']*lbl{key}["'][^>]*>(.*?)</span>""", page or "")
        head[key] = _text(m.group(1)) if m else ""
    fmt = re.search(r"""(?is)txtFileFormat["'][^>]*value=["']([a-z0-9]+)""", page or "")
    fmt = fmt.group(1).lower() if fmt else None
    docs = []
    panel = re.search(r"""(?is)id=["'][^"']*pnlDownloads["'][^>]*>(.*?)</div>""", page or "")
    if panel:
        for m in re.finditer(r"(?is)<a\b([^>]*)>(.*?)</a>", panel.group(1)):
            for url, text in _links(m.group(0), base):
                kind_fmt = link_kind(url, text, _attr(m.group(1), "data-type"))
                if kind_fmt and not any(d.url == url for d in docs):
                    docs.append(Document(url, kind_fmt[0], text or kind_fmt[0].title(), meeting_id, None,
                                         kind_fmt[1] or (fmt if "FileOpen" in url else None)))
    items: list[Item] = []
    table = re.search(r"""(?is)<table[^>]*id=["']MeetingDetail["'][^>]*>(.*?)</table>""", page or "")
    section = None
    for row in re.findall(r"(?is)<tr\b[^>]*>(.*?)</tr>", table.group(1) if table else ""):
        cells = re.findall(r"(?is)<td\b([^>]*)>(.*?)</td>", row)
        depth = 0
        for attrs, inner in cells:
            if re.search(r"""class=["']?Num""", attrs) or re.search(r"""class=["']?Title""", attrs):
                break
            depth += 1
        num = next((_text(inner) for attrs, inner in cells if re.search(r"""class=["']?Num""", attrs)), "")
        title_cell = next((inner for attrs, inner in cells if re.search(r"""class=["']?Title""", attrs)), "")
        title = _text(title_cell)
        links = _links(title_cell, base)
        number = num.rstrip(". ") or None
        if depth == 0:  # a section heading: an agenda entry of its own, and the section of the items below it
            items.append(Item(meeting_id, number, title, None, []))
            section = title or section
        elif depth == 1:
            atts = [Document(u, "item", t, meeting_id, number, fmt) for u, t in links if "FileOpen.aspx" in u]
            items.append(Item(meeting_id, number, title, section, atts))
        elif items:  # an attachment row under the item above
            for u, t in links:
                items[-1].attachments.append(Document(u, "item", t, meeting_id, items[-1].number,
                                                      fmt if "FileOpen.aspx" in u else None))
    return head, docs, items


@register
class IQM2:
    name, transport = "iqm2", "http"

    def __init__(self, http: Fetch, fresh: bool = True) -> None:
        self.http, self.fresh = http, fresh
        self._rows: dict[tuple[str, str], dict] = {}

    def detect(self, url: str, html: str | None = None) -> str | None:
        client = client_of(url)
        if client:
            return client
        for pattern in (EMBED, ANY_LINK):
            for m in pattern.finditer(html or ""):
                client = m.group(1).lower()
                if client not in VENDOR and not re.fullmatch(r"mt\d+", client):
                    return client
        return None

    def meetings(self, client: str, since: date, until: date) -> Listing:
        notes, found, read, newest = [], {}, 0, None
        for year in range(since.year, until.year + 1):
            if newest is not None and year > newest:
                notes.append(f"the calendar offers years up to {newest}: nothing listed after it (the body may have left IQM2)")
                break
            start, end = max(since, date(year, 1, 1)), min(until, date(year, 12, 31))
            r = self.http.get(calendar_url(client, start, end), use_cache=not self.fresh)
            if r.bot_wall:
                raise PortalError(f"iqm2 {client}: bot wall on the calendar ({r.status or r.error})")
            if not r.ok:
                why = r.error or f"HTTP {r.status}"
                if read == 0 and year == since.year and (r.error or (r.status or 0) >= 500):
                    hint = (" (IQM2 hosts complete TLS and then never answer this client, from India and from the US "
                            "alike, while a browser gets the page: the browser tier is needed)") if "Timeout" in why else ""
                    raise PortalError(f"iqm2 {client}: calendar not read: {why}{hint}")
                notes.append(f"{year} not read: {why}")
                continue
            read += 1
            page = r.body.decode("utf-8", errors="replace")
            years = calendar_years(page)
            if years and newest is None:
                newest = years[0]
            if "Detail_Meeting.aspx" not in page and "MeetingRow" not in page and "Calendar" not in page:
                notes.append(f"{year}: the calendar page did not look like an IQM2 list")
            for row in parse_calendar(page, client):
                found.setdefault(row["id"], row)
        if read == 0:
            raise PortalError(f"iqm2 {client}: no calendar year could be read ({'; '.join(notes)})")
        if newest is not None and newest < until.year and not any("years up to" in n for n in notes):
            notes.append(f"the calendar offers years up to {newest}: nothing listed after it (the body may have left IQM2)")
        meetings = []
        for row in found.values():
            if row["date"] is None or not since <= row["date"] <= until:
                continue
            governing = is_governing(row["board"])
            title = row["title"] + (f" ({row['status']})" if re.search(r"cancel|postpon", row["status"], re.I) else "")
            meetings.append(Meeting(row["id"], row["date"], title, row["board"],
                                    meeting_kind(row["type"], row["board"], governing=governing), governing,
                                    detail_url(client, row["id"])))
            self._rows[(client, row["id"])] = row
        undated = sum(1 for r in found.values() if r["date"] is None)
        if undated:
            notes.append(f"{undated} rows without a readable date left out")
        meetings.sort(key=lambda m: (m.date, m.id))
        return Listing(meetings, notes)

    def _detail(self, client: str, meeting: Meeting):
        r = self.http.get(detail_url(client, meeting.id))
        if r.bot_wall:
            raise PortalError(f"iqm2 {client}: bot wall on meeting {meeting.id}")
        if not r.ok:
            raise PortalError(f"iqm2 {client}: meeting {meeting.id} not read: {r.error or r.status}")
        return parse_detail(r.body.decode("utf-8", errors="replace"), client, meeting.id)

    def documents(self, client: str, meeting: Meeting) -> list[Document]:
        row = self._rows.get((client, meeting.id))
        if row is not None and row["links"]:
            docs = []
            for url, text in row["links"]:
                kind_fmt = link_kind(url, text)
                if kind_fmt and not any(d.url == url for d in docs):
                    docs.append(Document(url, kind_fmt[0], text or kind_fmt[0].title(), meeting.id, None, kind_fmt[1]))
            return docs
        return self._detail(client, meeting)[1]

    def items(self, client: str, meeting: Meeting) -> list[Item]:
        return self._detail(client, meeting)[2]
