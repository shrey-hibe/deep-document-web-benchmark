"""CivicClerk (<tenant>.portal.civicclerk.com): the tenant's public JSON service at <tenant>.api.civicclerk.com.

The portal is a script page filled from the tenant's own service, which answers plain GETs:
  /v1/Events/GetEventsList(fromDate='Y-M-D',toDate='Y-M-D',categoryIds=[],skip=N,sortDescending=false,cursorId=0)
        events in the window, 15 a time with `hasMore`; `skip` pages on. Each has its board (categoryName), start (local
        wall-clock time written with a "Z"), agenda id, and its published files (agenda, packet, minutes, notices).
  /v1/Events/<id>          one event, with its published files (used when a meeting comes without the listing)
  /v1/Meetings/<agendaId>  the agenda as a tree of items (outline numbers per level, titles, descriptions) with each
                           item's attachments
  /v1/Meetings/GetMeetingFileStream(fileId=<id>,plainText=false)  a published file itself (the PDF)
  /v1/Meetings/GetAttachmentFile(fileId=<id>)                      an item attachment itself
The listing is read a calendar year at a time, every page of it. Dates are read as written (local); the service
does not say which board governs, so `governing` comes from the board's name (civic_common).

Cannot: item outcomes (the agenda JSON carries none; the minutes are a file), staff reports (`reportsList`, no public
file route known), the video file itself (the media page on the portal is given instead).
Checked on Ukiah, CA
(city), Indian River County, FL and Cass County, IN (counties), 2026-11-05.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, datetime, timedelta
from html import unescape
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from . import civic_common as cc
from .base import Document, Fetch, Item, Listing, Meeting, PortalError
from .registry import register

_HOST = re.compile(r"\b([a-z0-9][a-z0-9-]*)\.(?:(?:portal|api)\.)?civicclerk\.com\b", re.I)
_NOT_TENANTS = {"www", "portal", "api", "help", "support", "cdn", "static", "docs", "status", "login", "auth", "mail",
                "info", "blog", "go", "app", "media", "assets", "public"}
MAX_PAGES = 400  # 6,000 events in a year: far beyond any tenant; a guard against a service that never says "no more"


def tenant_of(url: str) -> str | None:
    host = (urlsplit(url if "//" in url else f"https://{url}").hostname or "").lower()
    m = re.fullmatch(r"([a-z0-9][a-z0-9-]*)\.(?:(?:portal|api)\.)?civicclerk\.com", host)
    return m.group(1) if m and m.group(1) not in _NOT_TENANTS else None


def api(tenant: str) -> str:
    return f"https://{tenant}.api.civicclerk.com"


def events_url(tenant: str, since: date, until: date, skip: int = 0) -> str:
    """Events dated since..until: the service's toDate is the day after the last day."""
    return (f"{api(tenant)}/v1/Events/GetEventsList(fromDate='{since.isoformat()}',"
            f"toDate='{(until + timedelta(days=1)).isoformat()}',categoryIds=[],skip={int(skip)},sortDescending=false,cursorId=0)")


def file_url(tenant: str, file_id) -> str:
    return f"{api(tenant)}/v1/Meetings/GetMeetingFileStream(fileId={int(file_id)},plainText=false)"


def attachment_url(tenant: str, attachment_id) -> str:
    return f"{api(tenant)}/v1/Meetings/GetAttachmentFile(fileId={int(attachment_id)})"


def portal_url(tenant: str, event_id) -> str:
    return f"https://{tenant}.portal.civicclerk.com/event/{int(event_id)}"


def _text(fragment) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", str(fragment or ""))).split())


def _content(text: str) -> str | None:
    """A description that is only a form's empty labels ("Description: Justification:") says nothing."""
    rest = re.sub(r"\b[A-Z][A-Za-z /&-]{0,40}:", " ", text)
    return text if rest.strip(" .-") else None


def _day(value) -> date | None:
    """A CivicClerk time ("2026-09-16T17:00:00Z") is local wall-clock time: its date is the meeting's date."""
    try:
        return datetime.fromisoformat(str(value)[:19]).date() if value else None
    except ValueError:
        return None


def _suffix(name) -> str | None:
    suffix = PurePosixPath(str(name or "").split("?", 1)[0]).suffix.lower().lstrip(".")
    return suffix or None


def published_files(event: dict) -> list[dict]:
    """The event's published files; the list endpoint has been seen sending them as a string."""
    files = event.get("publishedFiles") or []
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except json.JSONDecodeError:
            import ast
            files = ast.literal_eval(files)
    return [dict(f) for f in files if isinstance(f, dict) and f.get("fileId")]


def meeting_of(event: dict, tenant: str) -> Meeting:
    board = _text(event.get("categoryName") or event.get("eventCategoryName"))
    title = _text(event.get("eventName") or event.get("agendaName")) or board or f"Meeting {event.get('id')}"
    governing = cc.governing_of(board) if board else cc.governing_of(title)
    return Meeting(id=str(event["id"]), date=_day(event.get("startDateTime") or event.get("eventDate")), title=title,
                   board=board, kind=cc.kind_of(title, board, governing=governing), governing=governing,
                   url=portal_url(tenant, event["id"]))


def documents_of(event: dict, tenant: str) -> list[Document]:
    mid = str(event["id"])
    docs = []
    for f in sorted(published_files(event), key=lambda f: (f.get("sort") or 0, f.get("fileId") or 0)):
        label = _text(f.get("type")) or _text(f.get("name"))
        name = _text(f.get("name"))
        title = label if not name or name.lower() == label.lower() else f"{label}: {name}"
        docs.append(Document(url=file_url(tenant, f["fileId"]), kind=cc.document_kind(label), title=title,
                             meeting_id=mid, format=_suffix(f.get("url"))))
    media = (event.get("externalMediaUrl") or "").strip()
    if media:
        docs.append(Document(url=media, kind="video", title="Meeting video", meeting_id=mid))
    elif event.get("hasMedia"):
        docs.append(Document(url=f"{portal_url(tenant, event['id'])}/media", kind="video",
                             title="Meeting video (the portal's player)", meeting_id=mid))
    return docs


def items_of(agenda: dict, tenant: str, meeting_id: str) -> list[Item]:
    """/v1/Meetings/<agendaId> -> items in agenda order, numbered by their outline path ("8.a"). An entry with children
    is a heading: its children carry it as their section."""
    out: list[Item] = []

    def walk(entries, prefix: str, section: str | None) -> None:
        for e in sorted(entries or [], key=lambda e: e.get("sortOrder") or 0):
            own = str(e.get("agendaObjectItemOutlineNumber") or "").strip().rstrip(".")
            number = f"{prefix}.{own}" if prefix and own else own or prefix or None
            title = _text(e.get("agendaObjectItemName")) or "(untitled)"
            children = e.get("childItems") or []
            if children:
                walk(children, number or "", f"{number}. {title}" if number else title)
                continue
            attachments = [Document(url=attachment_url(tenant, a["id"]), kind="item",
                                    title=_text(a.get("fileName")) or f"Attachment {a['id']}", meeting_id=meeting_id,
                                    item_no=number, format=_suffix(a.get("mediaFileName") or a.get("fileName")))
                           for a in e.get("attachmentsList") or [] if a.get("id") and a.get("isPublished", True)]
            out.append(Item(meeting_id=meeting_id, number=number, title=title, section=section, attachments=attachments,
                            text=_content(_text(e.get("agendaObjectItemDescription")))))

    walk(agenda.get("items") or [], "", None)
    return out


@register
class CivicClerk:
    name, transport = "civicclerk", "http"

    def __init__(self, http: Fetch) -> None:
        self.http = http
        self._events: dict[tuple[str, str], dict] = {}  # (tenant, event id) -> the event as the listing gave it

    # ------------------------------------------------------------------ detect
    def detect(self, url: str, html: str | None = None) -> str | None:
        tenant = tenant_of(url or "")
        if tenant or not html:
            return tenant
        found = Counter(m.group(1).lower() for m in _HOST.finditer(html) if m.group(1).lower() not in _NOT_TENANTS)
        return found.most_common(1)[0][0] if found else None

    # ------------------------------------------------------------------ meetings
    def meetings(self, client: str, since: date, until: date) -> Listing:
        notes: list[str] = []
        events: dict[str, dict] = {}
        read_any = False
        for year in range(since.year, until.year + 1):
            start, end = max(since, date(year, 1, 1)), min(until, date(year, 12, 31))
            skip, pages = 0, 0
            while True:
                resp = cc.fetch(self.http, events_url(client, start, end, skip))
                if not resp.ok:
                    if resp.bot_wall or not read_any:
                        raise PortalError(f"civicclerk {client}: event list refused: {cc.refusal(resp)}")
                    notes.append(f"{year}: events from number {skip + 1} on not read: {cc.refusal(resp)}")
                    break
                try:
                    data = json.loads(resp.body)
                except ValueError:
                    data = None
                batch = data.get("items") if isinstance(data, dict) else None
                if not isinstance(batch, list):
                    if not read_any:
                        raise PortalError(f"civicclerk {client}: event list is not the service's JSON ({cc.refusal(resp)})")
                    notes.append(f"{year}: events from number {skip + 1} on not read: the answer is not the service's JSON")
                    break
                read_any = True
                for e in batch:
                    if isinstance(e, dict) and e.get("id") is not None:
                        events.setdefault(str(e["id"]), e)
                skip += len(batch)
                pages += 1
                if not data.get("hasMore") or not batch:
                    break
                if pages >= MAX_PAGES:
                    notes.append(f"{year}: stopped after {pages} pages ({skip} events) though the service said there were more")
                    break
        meetings = []
        for e in events.values():
            m = meeting_of(e, client)
            if m.date is None or since <= m.date <= until:
                if m.date is None:
                    notes.append(f"event {m.id} ({m.title}) has no readable date")
                self._events[(client, m.id)] = e
                meetings.append(m)
        meetings.sort(key=lambda m: (m.date or date.min, m.id))
        return Listing(meetings, notes)

    # ------------------------------------------------------------------ one meeting
    def _event(self, client: str, meeting: Meeting) -> dict:
        event = self._events.get((client, meeting.id))
        if event is not None:
            return event
        if not meeting.id.isdigit():
            raise PortalError(f"civicclerk {client}: {meeting.id!r} is not a CivicClerk event id")
        resp = cc.fetch(self.http, f"{api(client)}/v1/Events/{meeting.id}")
        if not resp.ok:
            raise PortalError(f"civicclerk {client}: event {meeting.id} refused: {cc.refusal(resp)}")
        try:
            event = json.loads(resp.body)
        except ValueError:
            event = None
        if not isinstance(event, dict) or event.get("id") is None:
            raise PortalError(f"civicclerk {client}: event {meeting.id} is not the service's JSON")
        self._events[(client, meeting.id)] = event
        return event

    def documents(self, client: str, meeting: Meeting) -> list[Document]:
        return documents_of(self._event(client, meeting), client)

    def items(self, client: str, meeting: Meeting) -> list[Item]:
        agenda_id = self._event(client, meeting).get("agendaId")
        if not isinstance(agenda_id, int) or agenda_id <= 0:
            return []  # no agenda published for this event
        resp = cc.fetch(self.http, f"{api(client)}/v1/Meetings/{agenda_id}")
        if not resp.ok:
            raise PortalError(f"civicclerk {client}: agenda {agenda_id} of event {meeting.id} refused: {cc.refusal(resp)}")
        try:
            agenda = json.loads(resp.body)
        except ValueError:
            agenda = None
        if not isinstance(agenda, dict):
            raise PortalError(f"civicclerk {client}: agenda {agenda_id} is not the service's JSON")
        return items_of(agenda, client, meeting.id)
