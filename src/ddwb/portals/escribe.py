"""eScribe meeting portals (pub-<client>.escribemeetings.com, also bm-public-<client>.): the portal's calendar method and
its HTML agendas.

Reads:
  POST /MeetingsCalendarView.aspx/GetCalendarMeetings   {"calendarStartDate": "YYYY-MM-DD", "calendarEndDate": ...}
        -> {"d": [meeting]}: ID (GUID), MeetingName, MeetingType, StartDate ("2026/09/15 18:00:00", local), HasAgenda,
        MeetingDocumentLink [{Type, Format, Title, Url}]: every meeting of every board in the range, upcoming ones too,
        documents attached. Asked one calendar year at a time.
  GET  /Meeting.aspx?Id={ID}&Agenda=Agenda|Merged|PostAgenda&lang=English   the agenda as HTML, item by item
        (div.AgendaItem<n>: AgendaItemCounter, AgendaItemTitle, AgendaItemDescription, AgendaItemMotions; attachments
        in div.AgendaItemAttachment<n>, links FileStream.ashx?DocumentId=N).
Document types: Agenda, Merged (revised agenda), Addendum, AgendaCover, PostAgenda (post-meeting package), PostMinutes
(the minutes), Video, AdditionalDocuments (anything: its title says). Format ".pdf" is a FileStream.ashx file, "HTML"
the Meeting.aspx rendering of the same document.

Cannot: get past eScribe's Cloudflare browser check. Every tenant tried on 2026-11-05 (pub-fairfield, pub-slocity,
pub-salinecounty, pub-sunlinetransit, pub-mrwmd, pub-covinaca, pub-morenovalley, pub-upland, pub-nationalcity,
pub-portofsandiego, pub-vernon, pub-raleighnc, pub-cityofboise, pub-lanecounty, pub-woolwich, bm-public-auroraco)
answered HTTP 403 "Verifying your browser" to plain HTTP from this machine (an Indian exit IP): meetings() raises a
PortalError that says so, for the browser tier (or a US exit) to take over. Nothing here tries to get around it.
Since 2026-11-06 nav hands the adapter ddwb.portals.browser.Fallback, which moves a walled host to the browser tier:
through OpenClaw's Chrome, National City's portal opened with no challenge shown and its calendar POST answered (11
meetings, Sep to Nov 2026); from a US exit the same calendar answers plain HTTP (114 meetings 2025-26).
So the shapes above were not read live here: they come from eScribe responses published in open-source scrapers' test
files (City Bureau's city-scrapers-san-diego: National City and Chula Vista calendars) and those scrapers' code; the
tests use those calendar rows. Outcomes and votes (in the PostMinutes HTML) are not read; legacy v1 agendas
(SelectableItem markup) give no items.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from urllib.parse import urljoin, urlsplit

from . import _tree
from ._kinds import Documents, board_name, document_kind, governing, meeting_kind, missing_note
from .base import Document, Fetch, Item, Listing, Meeting, PortalError
from .registry import register

HOST = re.compile(r"^((?:pub-|bm-public-)?[a-z0-9][a-z0-9-]*)\.escribemeetings\.com$", re.I)
IN_PAGE = re.compile(r"https?://((?:pub-|bm-public-)[a-z0-9-]+)\.escribemeetings\.com", re.I)
VENDOR = {"www", "help", "support", "docs", "status", "api", "app", "login", "cdn", "static", "assets", "media", "pub", "bm"}
CALENDAR = "/MeetingsCalendarView.aspx/GetCalendarMeetings"
CHECK = re.compile(r"verifying your browser|just a moment|cf-chl|checking your browser", re.I)
_DOC_TYPES = {"Agenda": "agenda", "Merged": "agenda", "Addendum": "agenda", "AgendaCover": "agenda", "PostAgenda": "packet",
              "PostMinutes": "minutes", "Minutes": "minutes", "Video": "video"}
_ITEM_CLASS = re.compile(r"^AgendaItem(\d+)$")
_ATTACH_CLASS = re.compile(r"^AgendaItemAttachment(\d+)$")


def base_url(client: str) -> str:
    return f"https://{client.lower()}.escribemeetings.com"


def wall_error(resp, what: str) -> PortalError:
    stopped = resp.bot_wall and "HTTP 403" in (resp.error or "")  # the client stopped the host after an earlier 403
    if resp.status == 403 or stopped or CHECK.search(resp.text[:20000] if resp.body else ""):
        return PortalError(f"{what}: eScribe's Cloudflare browser check (HTTP 403 'Verifying your browser') "
                           f"refuses plain HTTP; this tenant needs the browser tier")
    if resp.bot_wall:
        return PortalError(f"{what}: bot wall ({resp.error or f'HTTP {resp.status}'})")
    return PortalError(f"{what}: {resp.error or f'HTTP {resp.status}'}")


def start(row: dict) -> datetime | None:
    try:
        return datetime.strptime(str(row.get("StartDate")), "%Y/%m/%d %H:%M:%S")
    except ValueError:
        return None


def to_meeting(client: str, row: dict) -> Meeting:
    title = re.sub(r"\s+", " ", str(row.get("MeetingName") or row.get("MeetingType") or "")).strip()
    board = board_name(re.sub(r"\s+", " ", str(row.get("MeetingType") or title)).strip())
    when = start(row)
    return Meeting(id=str(row.get("ID")), date=when.date() if when else None, title=title, board=board,
                   kind=meeting_kind(title, board), governing=governing(board),
                   url=f"{base_url(client)}/Meeting.aspx?Id={row.get('ID')}&Agenda=Agenda&lang=English")


def link_document(client: str, link: dict, meeting_id: str) -> Document:
    kind = _DOC_TYPES.get(str(link.get("Type")))
    title = str(link.get("Title") or link.get("Type") or "").strip()
    if kind is None:  # AdditionalDocuments and anything new: the title says
        kind = document_kind(title)
    fmt = str(link.get("Format") or "").lower().lstrip(".")
    return Document(url=urljoin(base_url(client) + "/", str(link.get("Url") or "")), kind=kind, title=title,
                    meeting_id=meeting_id, format={"video": None}.get(fmt, fmt or None))


@register
class EScribe:
    name = "escribe"
    transport = "http"

    def __init__(self, http: Fetch) -> None:
        self.http = http
        self._rows: dict[tuple[str, str], dict] = {}

    def detect(self, url: str, html: str | None = None) -> str | None:
        m = HOST.match((urlsplit(url or "").hostname or "").lower())
        if m and m.group(1) not in VENDOR:
            return m.group(1)
        m = IN_PAGE.search(html or "")
        return m.group(1).lower() if m else None

    # ------------------------------------------------------------------ meetings
    def _calendar(self, client: str, since: date, until: date) -> list[dict]:
        resp = self.http.post(base_url(client) + CALENDAR,
                              json_body={"calendarStartDate": since.isoformat(), "calendarEndDate": until.isoformat()},
                              headers={"Content-Type": "application/json; charset=utf-8"})
        if resp.bot_wall or not resp.ok:
            raise wall_error(resp, f"calendar {since}..{until}")
        try:
            rows = json.loads(resp.body or b"{}").get("d")
        except (ValueError, AttributeError):
            rows = None
        if not isinstance(rows, list):
            raise PortalError(f"calendar {since}..{until}: not a list of meetings ({resp.content_type})")
        return rows

    def meetings(self, client: str, since: date, until: date) -> Listing:
        notes, rows, failed = [], {}, 0
        years = list(range(since.year, until.year + 1))
        for year in years:
            lo, hi = max(since, date(year, 1, 1)), min(until, date(year, 12, 31))
            try:
                for row in self._calendar(client, lo, hi):
                    rows[str(row.get("ID"))] = row
            except PortalError as error:
                if "browser check" in str(error) or year == years[0] and len(years) == 1:
                    raise
                failed += 1
                notes.append(f"{error}: {year} not read")
        if failed == len(years):
            raise PortalError(f"no calendar year could be read: {'; '.join(notes)}")
        meetings = []
        for key, row in rows.items():
            m = to_meeting(client, row)
            if m.date and since <= m.date <= until:
                self._rows[(client, key)] = row
                meetings.append(m)
        meetings.sort(key=lambda m: (m.date, m.title))
        with_minutes = sum(1 for m in meetings if any(link_document(client, l, m.id).kind == "minutes"
                                                      for l in self._rows[(client, m.id)].get("MeetingDocumentLink") or []))
        if meetings:
            notes.append(f"minutes listed for {with_minutes} of {len(meetings)} meetings")
        return Listing(meetings, notes)

    def _row(self, client: str, meeting: Meeting) -> dict | None:
        if (client, meeting.id) not in self._rows and meeting.date:
            self.meetings(client, meeting.date, meeting.date)
        return self._rows.get((client, meeting.id))

    # ------------------------------------------------------------------ documents
    def documents(self, client: str, meeting: Meeting) -> Documents:
        row = self._row(client, meeting)
        if row is None:
            raise PortalError(f"meeting {meeting.id} not on the calendar for {meeting.date}")
        docs, seen = Documents(), set()
        for link in row.get("MeetingDocumentLink") or []:
            if not link.get("Url") or str(link.get("Url")).startswith("<"):
                continue
            doc = link_document(client, link, meeting.id)
            if doc.url not in seen:
                seen.add(doc.url)
                docs.append(doc)
        if row.get("HasVideo") and row.get("VideoUrl") and urljoin(base_url(client) + "/", row["VideoUrl"]) not in seen:
            docs.append(Document(url=urljoin(base_url(client) + "/", row["VideoUrl"]), kind="video", title="Video",
                                 meeting_id=meeting.id))
        if not row.get("HasAgenda") and not docs:
            docs.notes.append("the portal lists this meeting with no agenda posted yet")
        missing_note(docs)
        return docs

    # ------------------------------------------------------------------ items
    def items(self, client: str, meeting: Meeting) -> list[Item]:
        row = self._row(client, meeting) or {}
        html = {str(l.get("Type")): l for l in row.get("MeetingDocumentLink") or [] if str(l.get("Format")).upper() == "HTML"}
        chosen = next((html[t] for t in ("Merged", "Agenda", "PostAgenda") if t in html), None)
        if chosen is None:
            return []
        url = urljoin(base_url(client) + "/", chosen["Url"])
        resp = self.http.get(url)
        if resp.bot_wall or not resp.ok:
            raise wall_error(resp, f"HTML agenda {url}")
        return parse_agenda(resp.text, base_url(client), meeting.id)


def parse_agenda(html: str, base: str, meeting_id: str) -> list[Item]:
    """An eScribe HTML agenda's items in order. A heading whose numbered sub-items follow (7 and 7.1) becomes their
    section rather than an item; a heading with nothing under it stays an item."""
    root = _tree.parse(html)
    attachments: dict[str, dict[str, Document]] = {}
    for div in root.find_all(pred=lambda n: any(_ATTACH_CLASS.match(c) for c in n.classes)):
        item_id = next(_ATTACH_CLASS.match(c).group(1) for c in div.classes if _ATTACH_CLASS.match(c))
        for a in div.find_all("a", pred=lambda n: "documentid=" in n.attrs.get("href", "").lower()):
            url = urljoin(base + "/", a.attrs["href"])
            title = a.attrs.get("data-original-title") or a.text()
            attachments.setdefault(item_id, {}).setdefault(url, Document(url=url, kind="item", title=title.strip(),
                                                                         meeting_id=meeting_id))
    rows = []
    for div in root.find_all("div", pred=lambda n: "AgendaItem" in n.classes and any(_ITEM_CLASS.match(c) for c in n.classes)):
        item_id = next(_ITEM_CLASS.match(c).group(1) for c in div.classes if _ITEM_CLASS.match(c))
        counter, title = div.find(cls="AgendaItemCounter"), div.find(cls="AgendaItemTitle")
        number = counter.text().rstrip(".") if counter and counter.text() else None
        parts = [d.text() for d in div.find_all(cls="AgendaItemDescription")]
        for motion in div.find_all(cls="AgendaItemMotion"):
            label, text = motion.find(cls="MotionLabel"), motion.find(cls="MotionText")
            if text and text.text():
                parts.append(f"{label.text()}: {text.text()}" if label and label.text() else text.text())
        rows.append((item_id, number, title.text() if title else "", " ".join(p for p in parts if p) or None))
    items, section = [], {}
    for i, (item_id, number, title, text) in enumerate(rows):
        nxt = rows[i + 1][1] if i + 1 < len(rows) else None
        if number and nxt and nxt.startswith(number + "."):
            section[number] = f"{number}. {title}"
            continue
        parent = number.rsplit(".", 1)[0] if number and "." in number else None
        docs = list(attachments.get(item_id, {}).values())
        for d in docs:
            d.item_no = number
        items.append(Item(meeting_id=meeting_id, number=number, title=title, section=section.get(parent),
                          attachments=docs, text=text))
    return items
