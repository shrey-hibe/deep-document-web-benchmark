"""Diligent Community portals (<org>.diligent.community, <org>.community.highbond.com): the portal's own JSON service.

Two plain GETs give a meeting's whole structure, no browser:
  /Services/MeetingsService.svc/meetings?from=&to=           meetings (Id, MeetingDate, Name, MeetingTypeName)
  /Services/MeetingsService.svc/meetings/{id}/meetingDocuments the documents (Type 1 agenda or packet, Type 2 minutes),
                                                               with the agenda as HTML: sections, items, attachment links
Checked against a hand-made map of one meeting: 49 of 49 items and 98 of 98 attachments.
"""

from __future__ import annotations

import json
import re
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit

SERVICE = "/Services/MeetingsService.svc/meetings"
HOSTS = re.compile(r"\.diligent\.community$|\.community\.highbond\.com$", re.I)
_NUMBER = re.compile(r"^\d+(?:\.\d+)?\.?$")


def is_portal(url: str) -> bool:
    return bool(HOSTS.search(urlsplit(url).hostname or ""))


def origin(url: str) -> str:
    return f"https://{(urlsplit(url).hostname or '').lower()}"


def meetings_url(portal: str, since: date, until: date) -> str:
    return f"{origin(portal)}{SERVICE}?{urlencode({'from': since.isoformat(), 'to': until.isoformat()})}"


def documents_url(portal: str, meeting_id: int) -> str:
    return f"{origin(portal)}{SERVICE}/{int(meeting_id)}/meetingDocuments"


def document_kind(doc: dict) -> str:
    """Type 2 is minutes; Type 1 is the agenda (HTML or Word) or the packet (PDF)."""
    if doc.get("Type") == 2:
        return "minutes"
    if doc.get("Type") == 1:
        return "packet" if str(doc.get("Format", "")).lower() == "pdf" else "agenda"
    return "other"


class _Agenda(HTMLParser):
    """The agenda HTML in order: section headings (h2), items (h3, with their number) and attachment links."""

    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base, self.items, self.attachments = base, [], []
        self.section = self.number = self.capture = self.link = None
        self.current = None
        self.buffer: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k: v or "" for k, v in attrs}
        if tag == "a":
            if a.get("name", "").startswith("AgendaHeading"):
                self.current = None
            if "/document/" in a.get("href", ""):
                self.link, self.capture, self.buffer = a["href"], "link", []
        elif tag in ("h2", "h3"):
            self.capture, self.buffer = tag, []
        elif tag == "span" and self.capture is None:
            self.capture, self.buffer = "span", []

    def handle_data(self, data):
        if self.capture:
            self.buffer.append(data)

    def handle_endtag(self, tag):
        text = " ".join("".join(self.buffer).split())
        if tag == "a" and self.capture == "link":
            if text and self.link:
                self.attachments.append({"item": self.current, "title": text, "url": urljoin(self.base + "/", self.link)})
            self.capture, self.link = None, None
        elif tag == "h2" and self.capture == "h2":
            self.section = f"{self.number}. {text}" if self.number and "." not in self.number else text
            self.current, self.number, self.capture = None, None, None
        elif tag == "h3" and self.capture == "h3":
            self.items.append({"ordinal": len(self.items) + 1, "number": self.number, "section": self.section, "title": text})
            self.current, self.number, self.capture = len(self.items), None, None
        elif tag == "span" and self.capture == "span":
            if _NUMBER.match(text):
                self.number = text.rstrip(".")
            self.capture = None


def parse_agenda(html: str, base: str) -> tuple[list[dict], list[dict]]:
    parser = _Agenda(base)
    parser.feed(html or "")
    parser.close()
    return parser.items, parser.attachments


def agenda_text(items: list[dict]) -> str:
    """The agenda's items as text, one numbered line each, for the item rules."""
    return "\n".join(f"{i.get('number') or i['ordinal']}. {i['title']}" + (f"  [{i['section']}]" if i.get("section") else "")
                     for i in items)


def read_meetings(body: bytes) -> list[dict]:
    data = json.loads(body or b"[]")
    return sorted(data if isinstance(data, list) else [], key=lambda m: (str(m.get("MeetingDate")), m.get("Id") or 0))


def read_documents(body: bytes, portal: str) -> tuple[list[dict], list[dict], list[dict]]:
    """(documents, agenda items, attachments) of one meeting."""
    data = json.loads(body or b"{}")
    base = origin(portal)
    docs, items, attachments = [], [], []
    for d in data.get("Documents") or []:
        kind = document_kind(d)
        docs.append({"kind": kind, "title": d.get("Name") or "Document", "url": f"{base}/document/{d['Id']}",
                     "format": d.get("Format")})
        if kind == "agenda" and isinstance(d.get("Html"), str) and d["Html"].strip() and not items:
            items, attachments = parse_agenda(d["Html"], base)
    return docs, items, attachments
