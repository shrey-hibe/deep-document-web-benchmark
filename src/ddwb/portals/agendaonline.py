"""AgendaOnline (agendaonline.net): the California School Boards Association's agenda service for school districts,
retired when CSBA moved districts to GAMUT Meetings (on Simbli). Tenants are archives now: the newest meetings seen
were 2019 to 2022 (Chico USD 2019-12, Hayward USD 2020-08, San Jacinto USD 2021-06, Washington USD 2022-01, Desert
Sands USD 2022-06), each going back to 2007-2015. The registry has no platform for it (the free pool lists these
districts under simbli or own_site), so it is worth a body's history, not its recent meetings.

Not Hyland OnBase "Agenda Online" (hylandcloud.com, /OnBaseAgendaOnline/): a different product with the same name.

Pages read (ASP.NET WebForms, no browser needed). A tenant's listing is at one of two addresses, and the client id is
the part that names it:
  https://agendaonline.net/public/<slug>          client "<slug>" (chicounified, washington, desertsands)
  https://<sub>.agendaonline.net/public/          client "<sub>.agendaonline.net" (sanjacinto, palmsprings, hayward)
The listing is one page holding every meeting (date and time, title, location, an "Archived Meeting." lock), no pager.
A meeting opens only through the grid's form post (__doPostBack 'Select$<row>', with the page's __VIEWSTATE): the post
redirects to /public/Meeting.aspx?AgencyID=..&MeetingID=..&AgencyTypeID=1&IsArchived=.. . meetings() makes no post;
Meeting.id is the listing row ("row-<n>", stable because the archives no longer change) and documents() and items() make
the one post per meeting (kept for the adapter's life). The server keeps the tenant in its session, so the first
meeting opened per tenant costs one fresh GET of the listing; and the post goes to the grid name that opens archived
meetings cleanly (_targets: found by trial, the one fragile part).
The meeting page is the agenda and, for a past meeting, its minutes: per item the abstract, "Minutes:" text, motions
with their outcome and votes, and attachments (Meeting/Attachments/DisplayAttachment.aspx?AttachmentID=<n>).
documents() gives that page as the minutes (format html) when it records minutes or motions, else as the agenda, and
each attachment (kind item). There is no separate minutes file; the page's "Print" view is another post and not read.

It needs neither a browser nor a model to pick the meeting and minutes links. Checked on San Jacinto USD (sanjacinto.agendaonline.net) and Chico USD
(agendaonline.net/public/chicounified), found by web search, on 2026-11-05.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from html import unescape
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from .base import Document, Fetch, Item, Listing, Meeting, PortalError
from .registry import register

_HOST = re.compile(r"(?:^|\.)agendaonline\.net$", re.I)
_SLUG = re.compile(r"^[A-Za-z0-9_-]+$")
_RESERVED = {"meeting.aspx", "agency.aspx", "agencies.aspx", "default.aspx", "meeting", "secure", "public", "images"}
_URL_IN_HTML = re.compile(r"https?://(?:[\w-]+\.)?agendaonline\.net/[^\s\"'<>)]*", re.I)
_ROW = re.compile(r"<tr title=\"Click to select this row\.\"[^>]*__doPostBack\(&#39;([^&]+)&#39;,&#39;(Select\$\d+)&#39;\)[^>]*>(.*?)</tr>", re.S)
_HIDDEN = re.compile(r'<input type="hidden" name="([^"]+)" id="[^"]*" value="([^"]*)"')


def client_of(url: str) -> str | None:
    """The tenant from an AgendaOnline URL: the path slug on agendaonline.net, else the tenant's own sub-domain."""
    parts = urlsplit((url or "").strip() if "//" in (url or "") else f"https://{(url or '').strip()}")
    host = (parts.hostname or "").lower()
    if not _HOST.search(host):
        return None
    if host not in ("agendaonline.net", "www.agendaonline.net"):
        return host
    segments = [s for s in parts.path.split("/") if s]
    if len(segments) >= 2 and segments[0].lower() == "public" and _SLUG.match(segments[1]) \
            and segments[1].lower() not in _RESERVED:
        return segments[1]
    return None


def listing_url(client: str) -> str:
    return f"https://{client}/public/" if "." in client else f"https://agendaonline.net/public/{client}"


def _text(fragment: str) -> str:
    return " ".join(unescape(re.sub(r"<br\s*/?>", " ", re.sub(r"<[^>]+>", " ", fragment or ""))).split())


@dataclass
class Row:
    index: int
    target: str  # the grid's postback target
    argument: str  # Select$<index>
    when: date | None
    starts_at: str | None
    title: str
    location: str
    archived: bool


def parse_listing(html: str) -> list[Row]:
    rows = []
    for m in _ROW.finditer(html or ""):
        cells = [c for c in re.findall(r"<td[^>]*>(.*?)</td>", m.group(3), re.S)]
        stamp = _text(cells[0]) if cells else ""
        when, starts = None, None
        d = re.match(r"(\d{1,2}/\d{1,2}/\d{4})\s*(.*)", stamp)
        if d:
            try:
                when = datetime.strptime(d.group(1), "%m/%d/%Y").date()
            except ValueError:
                when = None
            starts = d.group(2) or None
        rows.append(Row(int(m.group(2).split("$")[1]), unescape(m.group(1)), m.group(2), when, starts,
                        _text(cells[1]) if len(cells) > 1 else "", _text(cells[2]) if len(cells) > 2 else "",
                        "Archived Meeting." in m.group(3)))
    return rows


def agency_name(html: str) -> str:
    m = re.search(r"<h2>(.*?)&nbsp;Meetings</h2>", html or "", re.S)
    return _text(m.group(1)) if m else ""


def meeting_kind(title: str) -> tuple[str, bool | None]:
    t = title.lower()
    if re.search(r"\bcommittee\b|\bcorporation\b|\bfinancing\b|\bfoundation\b|\bauthority\b|\bjpa\b", t):
        return "committee", False  # a committee, or another body the board sits as (a financing corporation)
    if re.search(r"\bspecial\b|\bemergency\b", t):
        return "special", True
    if re.search(r"workshop|work session|study session|retreat", t):
        return "work_session", True
    if re.search(r"\bregular\b|\bannual\b|\borganizational\b", t):
        return "regular", True
    return "other", True


def _targets(rendered: str) -> list[str]:
    """The grid targets to post to, in order. Found by trial on two tenants (2026-11-05): a page renders its grid as
    AgencyPublic2 or AgencyPublic3; posting the row to the other name opens the meeting as archived (HTTP 200), while
    the rendered name redirects archived meetings to IsArchived=False, which fails with HTTP 500 and can leave the
    session sending every later post back to the listing (San Jacinto). The rendered name is the fallback."""
    other = re.sub(r"AgencyPublic([23])", lambda m: f"AgencyPublic{5 - int(m.group(1))}", rendered)
    return [other, rendered] if other != rendered else [rendered]


def _meeting_page_url(final_url: str, archived: bool) -> str:
    parts = urlsplit(final_url)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    if archived:
        query["IsArchived"] = "True"
    return f"{parts.scheme}://{parts.netloc}{parts.path}?{urlencode(query)}"


_HEADER = re.compile(r"<span class='second-level-header'>(.*?)</span>", re.S)


def _field(chunk: str, name: str) -> str:
    m = re.search(rf'second-level-agenda-item-header"[^>]*>(?:<span>)?\s*{name}:\s*(?:</span>)?</td>\s*</tr>\s*<tr>\s*'
                  r'<td class="second-level-agenda-item">(.*?)</td>\s*</tr>', chunk, re.S)
    return _text(m.group(1)) if m else ""


def parse_meeting_page(html: str, meeting_id: str, base: str) -> tuple[list[Item], bool]:
    """The meeting page -> its items (number, title, section, abstract, what was done, attachments) and whether it
    records minutes or motions."""
    items: list[Item] = []
    sections: dict[str, str] = {}
    marks = list(_HEADER.finditer(html or ""))
    recorded = False
    for i, m in enumerate(marks):
        chunk = html[m.end(): marks[i + 1].start() if i + 1 < len(marks) else len(html)]
        head = _text(m.group(1))
        n = re.match(r"^(\d+(?:\.\d+)*)\.?\s+(.*)$", head)
        number, title = (n.group(1), n.group(2)) if n else (None, head)
        if number and "." not in number:
            sections[number] = f"{number}. {title}"
        section = sections.get(number.split(".")[0]) if number and "." in number else None
        minutes = _field(chunk, "Minutes")
        motions = [f"{_text(o)}: {_text(t)}" for t, o in
                   re.findall(r"<span class='motionText'>(.*?)</span>\s*<span class='motionOutCome'>(.*?)</span>", chunk, re.S)]
        recorded = recorded or bool(minutes or motions)
        attachments = [Document(urljoin(base, unescape(href)), "item", _text(label) or "Attachment", meeting_id,
                                item_no=number, format="pdf" if "pdf-icon" in cls else None)
                       for cls, href, label in re.findall(r'<a class="([^"]*)" href="([^"]*DisplayAttachment\.aspx[^"]*)"[^>]*>(.*?)</a>', chunk, re.S)]
        outcome = " | ".join(motions) or minutes or None
        items.append(Item(meeting_id=meeting_id, number=number, title=title or "(untitled)", section=section,
                          attachments=attachments, outcome=outcome[:2000] if outcome else None,
                          text=_field(chunk, "Quick Summary / Abstract") or None))
    return items, recorded


@register
class AgendaOnline:
    name, transport = "agendaonline", "http"

    def __init__(self, http: Fetch) -> None:
        self.http = http
        self._pages: dict[str, tuple[str, str, list[Row]]] = {}  # client -> (listing html, final url, rows)
        self._live: dict[str, tuple[str, str]] = {}  # client -> a listing fetched live in this session (html, url)
        self._opened: dict[tuple[str, str], tuple[str, str]] = {}  # (client, meeting id) -> (page url, html)

    def detect(self, url: str, html: str | None = None) -> str | None:
        client = client_of(url)
        if client or not html:
            return client
        for m in _URL_IN_HTML.finditer(html):
            client = client_of(unescape(m.group(0)))
            if client:
                return client
        return None

    def _check(self, r, what: str):
        if r.bot_wall:
            raise PortalError(f"AgendaOnline stopped at a bot wall or block ({what}, HTTP {r.status}): never solved")
        return r

    def _listing(self, client: str) -> tuple[str, str, list[Row]]:
        if client not in self._pages:
            url = listing_url(client)
            r = self._check(self.http.get(url), url)
            if not r.ok:
                raise PortalError(f"AgendaOnline listing {url}: HTTP {r.status} {r.error or ''}".strip())
            rows = parse_listing(r.text)
            if not rows and "gviMeetings" not in r.text:
                raise PortalError(f"AgendaOnline listing {url}: no meetings grid (not a tenant's public page)")
            self._pages[client] = (r.text, r.final_url or url, rows)
        return self._pages[client]

    def meetings(self, client: str, since: date, until: date) -> Listing:
        html, url, rows = self._listing(client)
        name = agency_name(html)
        meetings, notes = [], []
        for row in rows:
            if row.when is None:
                notes.append(f"listing row {row.index} ({row.title}): no date read")
                continue
            if since <= row.when <= until:
                kind, governing = meeting_kind(row.title)
                meetings.append(Meeting(f"row-{row.index}", row.when, row.title, board=name or "Board", kind=kind,
                                        governing=governing, url=url))
        dated = [r.when for r in rows if r.when]
        if dated and max(dated) < until:
            notes.append(f"AgendaOnline is an archive: the newest meeting {client} lists is {max(dated).isoformat()} "
                         "(CSBA retired the service; the district publishes elsewhere since)")
        meetings.sort(key=lambda m: (m.date, m.id))
        return Listing(meetings, notes)

    def _meeting_page(self, client: str, meeting: Meeting) -> tuple[str, str]:
        """(the meeting page's URL, its HTML), through the grid's form post. The server keeps the tenant in its
        session: the post must follow a live GET of the listing in the same session (a cached listing with a new
        session is sent back to the listing), so the first meeting opened per tenant costs one fresh listing GET, and
        posts are never answered from the cache (a cached refusal would be replayed)."""
        key = (client, str(meeting.id))
        if key in self._opened:
            return self._opened[key]
        _, _, rows = self._listing(client)
        index = int(str(meeting.id).removeprefix("row-")) if str(meeting.id).startswith("row-") else -1
        row = next((r for r in rows if r.index == index), None)
        if row is None:
            raise PortalError(f"AgendaOnline {client}: no listing row for meeting {meeting.id}")
        if client not in self._live:
            url = listing_url(client)
            r = self._check(self.http.get(url, use_cache=False), url)
            if not r.ok:
                raise PortalError(f"AgendaOnline listing {url}: HTTP {r.status} {r.error or ''}".strip())
            self._live[client] = (r.text, r.final_url or url)
        html, url = self._live[client]
        r, final = None, ""
        for target_control in _targets(row.target):
            action = re.search(r'<form method="post" action="([^"]+)"', html)
            fields = {name: unescape(value) for name, value in _HIDDEN.findall(html)}
            fields |= {"__EVENTTARGET": target_control, "__EVENTARGUMENT": row.argument}
            target = urljoin(url, unescape(action.group(1)) if action else "")
            r = self._check(self.http.post(target, data=fields, use_cache=False), target)
            final = r.final_url or ""
            if "MeetingID=" in final:
                break
        if "MeetingID=" not in final:
            raise PortalError(f"AgendaOnline {client}: selecting row {row.index} did not open a meeting "
                              f"(HTTP {r.status if r else None}, {final or 'no redirect'})")
        page_url = _meeting_page_url(final, row.archived)
        page = r if (page_url == final and r.ok) else self._check(self.http.get(page_url), page_url)
        if not page.ok:
            raise PortalError(f"AgendaOnline meeting page {page_url}: HTTP {page.status} {page.error or ''}".strip())
        self._opened[key] = (page_url, page.text)
        return self._opened[key]

    def documents(self, client: str, meeting: Meeting) -> list[Document]:
        page_url, html = self._meeting_page(client, meeting)
        items, recorded = parse_meeting_page(html, str(meeting.id), page_url)
        own = Document(page_url, "minutes" if recorded else "agenda",
                       "Agenda with minutes and motions recorded item by item" if recorded else "Agenda",
                       str(meeting.id), format="html")
        return [own] + [doc for item in items for doc in item.attachments]

    def items(self, client: str, meeting: Meeting) -> list[Item]:
        page_url, html = self._meeting_page(client, meeting)
        return parse_meeting_page(html, str(meeting.id), page_url)[0]
