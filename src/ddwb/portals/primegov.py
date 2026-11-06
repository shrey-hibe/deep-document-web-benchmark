"""PrimeGov public portals (<client>.primegov.com/public/portal): the portal's own public JSON API, plain GETs.

Reads:
  /api/v2/PublicPortal/GetArchivedMeetingYears              the years the archive holds, e.g. [2026, 2025, ...]
  /api/v2/PublicPortal/ListArchivedMeetings?year=Y          every past meeting of that year, all boards, each with its
                                                            documentList (templateName, templateId, compileOutputType)
  /api/v2/PublicPortal/ListUpcomingMeetings                 meetings still ahead
  /api/committee/GetCommitteeesListByShowInPublicPortal     committee id -> name (the board of each meeting)
  /Portal/Meeting?meetingTemplateId=T                       an HTML agenda (compileOutputType 3): items, sections,
                                                            attachments (/api/compilemeetingattachmenthistory/...)
Documents are compiled files: /Public/CompiledDocument?meetingTemplateId=T&compileOutputType=1 (PDF) or 2 (Word),
the URL the portal's own page builds (getHref in its _Upcoming.js); a document with its own `link` keeps that link.
A templateName says the kind (Agenda, HTML Agenda, Packet, Minutes, FC Minutes, Notice of Cancellation, Results...).

Cannot: read outcomes (a "Results" or vote-summary document is listed, not parsed); read items of a meeting that has
no HTML agenda (only PDFs); tell a committee from the governing board except by the committee's name.
Checked (2026-11-05) on Peoria AZ (peoriaaz), Foster City CA (fostercity), East Bay Regional Park District CA
(ebparks), Patterson CA (pattersonca). Two agenda layouts are read: the template layout (sections with data-sectionid,
items div.meeting-item) and the layout built from a Word file (table.item-table-fromdocx, no sections).
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

HOST = re.compile(r"^([a-z0-9][a-z0-9-]*)\.primegov\.com$", re.I)
IN_PAGE = re.compile(r"https?://([a-z0-9][a-z0-9-]*)\.primegov\.com", re.I)
VENDOR = {"www", "help", "support", "docs", "status", "api", "app", "login", "cdn", "static", "assets", "media", "learn"}
FORMATS = {1: "pdf", 2: "docx", 3: "html"}
_NUMBER = re.compile(r"^[\w]{1,4}(?:[.\-][\w]{1,4}){0,3}[.)]?$")


def base_url(client: str) -> str:
    return f"https://{client.lower()}.primegov.com"


def document_url(client: str, doc: dict) -> str:
    """The file itself, as the portal's own getHref builds it."""
    if doc.get("link"):
        return urljoin(base_url(client) + "/", doc["link"])
    template = doc.get("templateId")
    param = f"compiledMeetingDocumentFileId={doc.get('id')}" if not template or template <= 0 else f"meetingTemplateId={template}"
    if doc.get("compileOutputType") == 3:
        return f"{base_url(client)}/Portal/Meeting?{param}"
    return f"{base_url(client)}/Public/CompiledDocument?{param}&compileOutputType={doc.get('compileOutputType')}"


def _date(row: dict) -> date | None:
    try:
        return datetime.fromisoformat(str(row.get("dateTime"))[:19]).date()
    except ValueError:
        try:
            return datetime.strptime(str(row.get("date")), "%b %d, %Y").date()
        except ValueError:
            return None


def _json(resp, what: str):
    if resp.bot_wall:
        raise PortalError(f"{what}: bot wall ({resp.error or f'HTTP {resp.status}'})")
    if not resp.ok:
        raise PortalError(f"{what}: {resp.error or f'HTTP {resp.status}'}")
    try:
        return json.loads(resp.body or b"null")
    except ValueError as error:
        raise PortalError(f"{what}: not JSON ({error})") from None


def to_meeting(client: str, row: dict, committees: dict[int, str]) -> Meeting:
    title = (row.get("title") or "").strip()
    board = committees.get(row.get("committeeId")) or board_name(title)
    html = [d for d in row.get("documentList") or [] if d.get("compileOutputType") == 3 and not d.get("link")]
    page = document_url(client, html[0]) if html else f"{base_url(client)}/public/portal"  # no page of its own
    return Meeting(id=str(row.get("id")), date=_date(row), title=title, board=board, kind=meeting_kind(title, board),
                   governing=governing(board), url=page)


@register
class PrimeGov:
    name = "primegov"
    transport = "http"

    def __init__(self, http: Fetch) -> None:
        self.http = http
        self._rows: dict[tuple[str, str], dict] = {}  # (client, meeting id) -> the meeting's row, documents included

    # ------------------------------------------------------------------ detect
    def detect(self, url: str, html: str | None = None) -> str | None:
        m = HOST.match((urlsplit(url or "").hostname or "").lower())
        if m and m.group(1) not in VENDOR:
            return m.group(1)
        for m in IN_PAGE.finditer(html or ""):
            if m.group(1).lower() not in VENDOR:
                return m.group(1).lower()
        return None

    # ------------------------------------------------------------------ meetings
    def _committees(self, client: str, notes: list[str]) -> dict[int, str]:
        resp = self.http.get(f"{base_url(client)}/api/committee/GetCommitteeesListByShowInPublicPortal")
        try:
            data = _json(resp, "committee list")
            return {c["id"]: (c.get("name") or "").strip() for c in data or [] if isinstance(c, dict) and "id" in c}
        except PortalError as error:
            notes.append(f"{error}: boards named from meeting titles")
            return {}

    def meetings(self, client: str, since: date, until: date) -> Listing:
        base = base_url(client)
        notes: list[str] = []
        years = _json(self.http.get(f"{base}/api/v2/PublicPortal/GetArchivedMeetingYears"), "archive years")
        if not isinstance(years, list):
            raise PortalError(f"archive years: unexpected answer {str(years)[:80]!r}")
        committees = self._committees(client, notes)
        rows: dict[str, dict] = {}
        for year in sorted(y for y in years if isinstance(y, int) and since.year <= y <= until.year):
            try:
                data = _json(self.http.get(f"{base}/api/v2/PublicPortal/ListArchivedMeetings?year={year}"), f"archived meetings {year}")
            except PortalError as error:
                notes.append(f"{error}: year {year} not read")
                continue
            for row in data or []:
                rows[str(row.get("id"))] = row
        if until >= date.today().replace(day=1) or not rows:
            try:
                for row in _json(self.http.get(f"{base}/api/v2/PublicPortal/ListUpcomingMeetings"), "upcoming meetings") or []:
                    rows.setdefault(str(row.get("id")), row)
            except PortalError as error:
                notes.append(f"{error}: upcoming meetings not read")
        meetings = []
        for key, row in rows.items():
            m = to_meeting(client, row, committees)
            if m.date and since <= m.date <= until:
                self._rows[(client, key)] = row
                meetings.append(m)
        meetings.sort(key=lambda m: (m.date, m.id))
        with_minutes = sum(1 for m in meetings if any(document_kind(d.get("templateName", "")) == "minutes"
                                                      for d in self._rows[(client, m.id)].get("documentList") or []))
        if meetings:
            notes.append(f"minutes listed for {with_minutes} of {len(meetings)} meetings")
        return Listing(meetings, notes)

    # ------------------------------------------------------------------ documents
    def _row(self, client: str, meeting: Meeting) -> dict | None:
        row = self._rows.get((client, meeting.id))
        if row is None and meeting.date:
            self.meetings(client, meeting.date, meeting.date)
            row = self._rows.get((client, meeting.id))
        return row

    def documents(self, client: str, meeting: Meeting) -> Documents:
        row = self._row(client, meeting)
        if row is None:
            raise PortalError(f"meeting {meeting.id} not found in the portal's lists for {meeting.date}")
        docs = Documents()
        for d in row.get("documentList") or []:
            name = (d.get("templateName") or "").strip()
            docs.append(Document(url=document_url(client, d), kind=document_kind(name), title=name, meeting_id=meeting.id,
                                 format=None if d.get("link") else FORMATS.get(d.get("compileOutputType"))))
        if row.get("videoUrl"):
            docs.append(Document(url=row["videoUrl"], kind="video", title="Video", meeting_id=meeting.id))
        missing_note(docs)
        return docs

    # ------------------------------------------------------------------ items
    def items(self, client: str, meeting: Meeting) -> list[Item]:
        row = self._row(client, meeting)
        html_agendas = [d for d in (row or {}).get("documentList") or []
                        if d.get("compileOutputType") == 3 and not d.get("link") and document_kind(d.get("templateName", "")) == "agenda"]
        if not html_agendas:
            return []
        url = document_url(client, html_agendas[0])
        resp = self.http.get(url)
        if resp.bot_wall or not resp.ok:
            raise PortalError(f"HTML agenda {url}: {resp.error or f'HTTP {resp.status}'}")
        return parse_agenda(resp.text, base_url(client), meeting.id)


def _attachments(node: _tree.Node, base: str, meeting_id: str, number: str | None) -> list[Document]:
    out, seen = [], set()
    for a in node.find_all("a", pred=lambda n: "historyattachment" in n.attrs.get("href", "").lower()):
        url = urljoin(base + "/", a.attrs["href"])
        if url in seen:
            continue
        seen.add(url)
        title = a.text() or re.sub(r"^Download\s+", "", a.attrs.get("title", ""))
        out.append(Document(url=url, kind="item", title=title, meeting_id=meeting_id, item_no=number))
    return out


def parse_agenda(html: str, base: str, meeting_id: str) -> list[Item]:
    """Items of a PrimeGov HTML agenda, both layouts: every outermost element with data-itemid is an item."""
    root = _tree.parse(html)
    skip = lambda n: "item_contents" in n.classes or "optionalButtonsCell" in n.classes or "attachmentCell" in n.classes
    items, seen, section = [], set(), None
    for node in root.iter():  # document order: a section (data-sectionid) heads the items after it
        if "data-sectionid" in node.attrs and "data-itemid" not in node.attrs:
            num, head = node.find(cls="number-cell-section"), node.find(cls="section-heading")
            if head is not None:  # the template layout: the section's own number and heading cells
                text = " ".join(x for x in ((num.text() if num else ""), head.text()) if x)
            else:  # the layout built from a Word file: a one-row table whose first paragraph is the heading
                text = next((q.text(skip) for q in node.find_all("p") if q.text(skip)), node.text(skip))[:200]
            section = text or section
            continue
        item_id = node.attrs.get("data-itemid")
        if not item_id or item_id in seen or any(a.attrs.get("data-itemid") == item_id for a in node.ancestors()):
            continue
        seen.add(item_id)
        number = None
        cell = node.find(cls="number-cell")
        if cell is not None:
            number = cell.text() or None
        else:  # the layout built from a Word file: the number sits in an unclassed cell before the item's cell
            for td in node.find_all("td"):
                if td.classes or td.find(cls="agenda-item"):
                    continue
                t = td.text()
                if t and _NUMBER.match(t):
                    number = t
                    break
        number = number.rstrip(".") if number else None
        # the paperclip cell repeats class="agenda-item" in some layouts: the item's own cell is the one with text
        body = next((n for n in node.find_all(cls="agenda-item") if n.text(skip)), node)
        rows = [r for r in body.find_all("tr") if r.text(skip)]
        if rows:
            title, text = rows[0].text(skip), " ".join(r.text(skip) for r in rows[1:]) or None
        else:
            title, text = body.text(skip), None
        items.append(Item(meeting_id=meeting_id, number=number, title=title, section=section,
                          attachments=_attachments(node, base, meeting_id, number), text=text))
    return items
