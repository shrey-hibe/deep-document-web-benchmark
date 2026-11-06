"""The one interface every meeting-portal adapter implements, and the shapes it returns.

An adapter knows one portal product (Legistar, BoardDocs, CivicClerk, ...), never one body: what varies between bodies on
the same portal is the client id and what the portal returns, not the code. It answers four questions:

    detect(url, html)          is this URL, or this page (a body's own site embedding or linking the portal), on my
                               portal? -> the client id (e.g. "wake" for wake.legistar.com), or None
    meetings(client, since, until)
                               the meetings the portal lists in that window -> Listing (meetings, notes, coverage)
    documents(client, meeting) a meeting's documents: agenda, packet, minutes, video, ... -> [Document]
    items(client, meeting)     a meeting's agenda items with their attachments, where the portal publishes items; [] where
                               it does not

Everything comes back in the same shapes as the structure layer (meeting, item, document), so nothing downstream cares
which portal a body uses. An adapter reports what it could not do (notes) rather than returning less in silence, and
counts what it found per year (coverage), so a thin result flags itself.

Transport: `http` adapters take a Fetch (ddwb.deep.web.Http: paced per host, cached, the honest research User-Agent,
bot walls recorded). `browser` adapters drive a browser through the browse provider with the person-like settings of
decision #123 (no automation flags, a normal browser user agent, a person's pace) and stop at any CAPTCHA or challenge
that asks for an answer: they never solve one.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol, runtime_checkable

from ..deep.web import Response

KINDS = ("agenda", "packet", "minutes", "item", "video", "notice", "other")  # what a document is
MEETING_KINDS = ("regular", "special", "work_session", "committee", "other")


class Fetch(Protocol):
    """What an http adapter needs from the client (ddwb.deep.web.Http has both)."""

    def get(self, url: str, use_cache: bool = True) -> Response: ...

    def post(self, url: str, data: dict | None = None, json_body: dict | None = None, headers: dict | None = None,
             use_cache: bool = True) -> Response: ...


@dataclass
class Document:
    url: str
    kind: str  # one of KINDS
    title: str = ""
    meeting_id: str | None = None
    item_no: str | None = None  # the agenda item it is attached to, when the portal says
    format: str | None = None  # pdf, html, docx, ... when the portal says


@dataclass
class Item:
    meeting_id: str
    number: str | None  # as the portal prints it ("7.a", "22.10", "2025-108")
    title: str
    section: str | None = None  # the heading or parent item it sits under
    attachments: list[Document] = field(default_factory=list)
    outcome: str | None = None  # when the portal publishes the action taken
    text: str | None = None  # the item's own text, when the portal publishes it


@dataclass
class Meeting:
    id: str  # the portal's own id for the meeting
    date: date | None
    title: str
    board: str = ""  # the board or committee whose meeting it is, as the portal names it
    kind: str = "other"  # one of MEETING_KINDS
    governing: bool | None = None  # the body's governing board (council, commissioners, board of education), when knowable
    url: str | None = None  # the meeting's page on the portal


@dataclass
class Listing:
    meetings: list[Meeting]
    notes: list[str] = field(default_factory=list)  # what could not be read, and why

    @property
    def per_year(self) -> dict[int, int]:
        return dict(sorted(Counter(m.date.year for m in self.meetings if m.date).items()))


@runtime_checkable
class Portal(Protocol):
    name: str  # the platform's name as the registry spells it (legistar, boarddocs, civicclerk, ...)
    transport: str  # "http" or "browser"

    def detect(self, url: str, html: str | None = None) -> str | None: ...

    def meetings(self, client: str, since: date, until: date) -> Listing: ...

    def documents(self, client: str, meeting: Meeting) -> list[Document]: ...

    def items(self, client: str, meeting: Meeting) -> list[Item]: ...


class PortalError(RuntimeError):
    """The portal refused or failed (bot wall, error page, changed format): said, never an empty answer."""
