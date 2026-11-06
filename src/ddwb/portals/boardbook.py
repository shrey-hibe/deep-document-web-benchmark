"""BoardBook Premier (meetings.boardbook.org): Texas school districts above all, also Illinois, Oregon, Minnesota and a
few cities. The public view is rendered on the server and answers plain GETs with our research User-Agent; no browser.

Pages and endpoints read (client = the organisation id in every public URL, e.g. 644 for Rains ISD):
  /Public/Organization/<org>                    every meeting the organisation shows: board rows ("row-for-board") and
                                                committee rows ("row-for-unit-<id>", named in the unit filter), each
                                                with date, time, title, "Meeting Type:" and links to its Agenda,
                                                Minutes, Public Notice and "Supplementals" (approved resolutions etc.)
  /Public/Agenda/<org>?meeting=<id>             the agenda as a tree of items ("III.A.1.", "10.5.", "7.A)") with their
                                                descriptions and attachments (data-documentid, file name, type icon)
  /Public/DownloadAgenda/<org>?meeting=<id>     the agenda as a PDF
  /Public/Minutes/<org>?meeting=<id>            redirects to /Documents/CustomMinutesForMeeting/...: a document viewer.
  /Public/PublicNotice/<org>?meeting=<id>       Its script loads /Documents/DownloadPDF/TBR_AAAA_?org=<org> with the
                                                id from the hidden input NewDocumentViewerDocumentID, which a plain GET
                                                of the viewer page already carries: one GET finds the PDF
                                                (/Documents/DownloadPDF/<document id>?org=<org>), no script run needed
  /Meeting/Supplementals/<org>?meeting=<id>     a list of files in the same form as agenda attachments
  /Documents/FileViewerOrPublic/<org>?file=<n>  an attachment's viewer; its PDF is /Documents/DownloadPDF/<n>?org=<org>

Minutes: many tenants link a meeting's own minutes from the organisation page. Others (Northwest ISD) never do: the
minutes of a meeting are attachments of an "Approval of minutes" item on a later agenda ("Minutes - September 14 2026 -
Special Meeting.pdf" on the October 12 agenda). For a meeting with no Minutes link, documents() reads the next few
agendas of the same board and keeps the attachments of minutes items whose file name (or item title) states this
meeting's date, telling a special from a regular meeting on the same day by the words in the file name.
In a meeting's own documents, the minutes of earlier meetings attached to its agenda stay kind "item".

What it cannot do: how far back the public list reaches is the tenant's setting. BoardBook is reported to drop meetings
from the public list after about 13 months on some tenants; on others the list goes back years (Rains ISD to 2008,
Northwest ISD to 2004), while Minutes links may cover only the recent part (Grayslake CHSD 127: from November 2024).
meetings() says so in its notes when the window starts before the oldest meeting listed. The public agenda carries no
votes or outcomes. Attachments come as PDFs (DownloadPDF converts Word files); only "download original" links (an email) are
given as the page links them.

Reads the organisation and agenda pages (with item numbering) and resolves each minutes PDF (NewDocumentViewerDocumentID and the FileViewer to DownloadPDF rewrite). Checked on Rains ISD TX
(644), Northwest ISD TX (1405), Grayslake CHSD 127 IL (1736) and Ely MN (1352) on 2026-11-05.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html import unescape
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

from ..deep.text import loose_date
from .base import Document, Fetch, Item, Listing, Meeting, PortalError
from .registry import register

ORIGIN = "https://meetings.boardbook.org"
_HOST = re.compile(r"(?:^|\.)boardbook\.org$", re.I)
_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_RESERVED = {"public", "organization", "agenda", "minutes", "meetings", "meeting", "documents", "downloadpdf"}
_PAGES = {"organization", "agenda", "minutes", "publicnotice", "projector", "downloadagenda", "supplementals",
          "fileviewerorpublic", "fileviewer", "webviewer", "customminutesformeeting", "custompublicnoticeformeeting"}
_URL_IN_HTML = re.compile(r"https?://(?:[\w-]+\.)?boardbook\.org/[^\s\"'<>)]+", re.I)

# "September 24, 2026 at 6:30 PM - Business Meeting"; the time can be free text ("6:30 pm", "6:30 PM Closed Session;
# 7:00pm Open Session"), so everything up to the first " - " is kept as stated.
_HEADING = re.compile(r"^(?P<date>[A-Z][a-z]+ \d{1,2}, \d{4})(?:\s+(?:at\s+)?(?P<time>.*?))?\s+-\s+(?P<title>.*)$")
_ROW = re.compile(r'<tr class="(?P<classes>[^"]*\brow-for-(?:board|unit)\b[^"]*)"[^>]*>', re.I)
_ROW_LINK = re.compile(r"""href=["'](?P<href>/(?:Public|Meeting)/(?P<kind>Agenda|Minutes|PublicNotice|Supplementals)/"""
                       r"""[^?"']+\?meeting=(?P<meeting>\d+))["']""")
_UNIT_OPTION = re.compile(r"<option value='(\d+)'\s*>([^<]+)</option>")
_DOC_ID = re.compile(r'id="NewDocumentViewerDocumentID"\s+value="(\d+)"')
# Printed item numbers are a per-district setting: "10.5." (Hinsdale), "7.A)" and "12.D)4)a)(1)" (Evanston/Skokie),
# "III.A.1." (Rains ISD). The shape they share: digits, a Roman numeral or a letter, then segments of 1-4 letters or
# digits each closed by "." or ")" and optionally opened by "(", the last one possibly unclosed ("10.5 Approve").
# All normalise to dotted form: "10.5", "7.A", "12.D.4.a.1", "III.A.1".
_ITEM_NUMBER = re.compile(r"^((?:\d+|[IVXLC]+|[A-Za-z])[.)](?:\(?[0-9A-Za-z]{1,4}[.)])*(?:[0-9A-Za-z]{1,4}(?=\s))?)\s+")
_FILE_LINK = re.compile(r"/Documents/[^?]+\?(?:.*&)?file=(\d+)")
_MINUTES = re.compile(r"minute", re.I)
_TYPE_WORDS = ("special", "regular", "workshop", "work session", "hearing", "emergency", "closed", "committee")


def org_of(url: str) -> str | None:
    """The organisation id in any public BoardBook link, or None: /Public/<page>/<org>, /Meeting/Supplementals/<org>,
    /Documents/<viewer>/<org>?..., and /Documents/DownloadPDF/<document>?org=<org>."""
    parts = urlsplit((url or "").strip() if "//" in (url or "") else f"https://{(url or '').strip()}")
    if not _HOST.search(parts.hostname or ""):
        return None
    segments = [s for s in parts.path.split("/") if s]
    query = parse_qs(parts.query)
    if len(segments) >= 2 and segments[0].lower() == "documents" and segments[1].lower() == "downloadpdf":
        org = (query.get("org") or [""])[0]
        return org if _ID.match(org) else None
    if len(segments) >= 3 and segments[0].lower() in ("public", "meeting", "documents") and segments[1].lower() in _PAGES:
        org = segments[2].strip()
        if _ID.match(org) and org.lower() not in _RESERVED:
            return org
    return None


def _text(fragment: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", fragment or "")).split())


@dataclass
class Row:
    """One meeting on the organisation page."""
    key: str
    when: date
    title: str
    starts_at: str | None
    meeting_type: str | None
    unit: str | None  # the committee's name, for a committee row
    unit_id: str | None
    board_row: bool  # the row is marked as the board's (row-for-board)
    links: dict[str, str]  # Agenda, Minutes, PublicNotice, Supplementals -> absolute URL


def organization_name(page_html: str) -> str:
    m = re.search(r"<title>(.*?)</title>", page_html or "", re.S | re.I)
    name = _text(m.group(1)) if m else ""
    return re.sub(r"\s*Public View\b.*$", "", name).strip()


def parse_organization_page(page_html: str) -> list[Row]:
    """The organisation page -> its meetings by date (board and committee rows alike), first link of each kind kept."""
    units = {unit_id: _text(name) for unit_id, name in _UNIT_OPTION.findall(page_html)}
    starts = list(_ROW.finditer(page_html))
    rows: dict[str, Row] = {}
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(page_html)
        row = page_html[start.end():end]
        heading = re.search(r"<div>(.*?)</div>", row, re.S)
        cancelled = bool(heading) and "meeting-is-cancelled" in heading.group(1)
        if cancelled:  # the heading is wrapped in "Cancelled" labels
            heading_text = _text(re.sub(r'<span class="meeting-is-cancelled">.*?</span>', " ", heading.group(1), flags=re.S))
        else:
            heading_text = _text(heading.group(1)) if heading else ""
        match = _HEADING.match(heading_text)
        links: dict[str, Any] = {}
        for link in _ROW_LINK.finditer(row):
            links.setdefault(link["kind"], link)
        if not match or not links:
            continue
        key = next(iter(links.values()))["meeting"]
        meeting_type = re.search(r"Meeting Type:</b>\s*([^<]*)", row)
        classes = start.group("classes")
        unit = re.search(r"\brow-for-unit-(\d+)\b", classes)
        try:
            when = datetime.strptime(match["date"], "%B %d, %Y").date()
        except ValueError:
            continue
        rows.setdefault(key, Row(
            key=key, when=when, title=match["title"].strip() + (" (cancelled)" if cancelled else ""),
            starts_at=(match["time"] or "").strip() or None,
            meeting_type=_text(meeting_type.group(1)) or None if meeting_type else None,
            unit=(units.get(unit.group(1)) or f"Committee {unit.group(1)}") if unit else None,
            unit_id=unit.group(1) if unit else None,
            board_row=bool(re.search(r"\brow-for-board\b", classes)),
            links={kind: urljoin(ORIGIN + "/", link["href"]) for kind, link in links.items()},
        ))
    return sorted(rows.values(), key=lambda r: (r.when, r.key))


def meeting_kind(row: Row) -> tuple[str, bool]:
    """(kind, governing): the portal's Meeting Type first, the title where the type says nothing. A committee row, or
    a board row titled as a committee's meeting (old rows put committees under the board), is not the governing board."""
    title, kind_type = row.title.lower(), (row.meeting_type or "").lower()
    committee = (row.unit_id is not None and not row.board_row) or kind_type.startswith("committee") \
        or bool(re.search(r"\bcommittee\b", title))
    if committee:
        return "committee", False
    # A title that names a special meeting or a workshop outranks the type (Rains ISD types its special meetings "Regular")
    if kind_type.startswith(("special", "emergency", "called")) or re.search(r"\bspecial\b|\bemergency\b|\bcalled\b", title):
        kind = "special"
    elif "work" in kind_type or (re.search(r"workshop|work session|study session|retreat", title) and "regular" not in title):
        kind = "work_session"
    elif kind_type.startswith("regular") or "regular" in title:
        kind = "regular"
    else:
        kind = "other"
    return kind, True


class _AgendaParser(HTMLParser):
    """Walk the agenda rows in order; each row is one item with a parent id."""

    _BLOCKS = {"form-check": "title", "AgendaItemType": "type", "Description": "description"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, Any]] = []
        self.files: list[dict[str, str]] = []
        self._row: dict[str, Any] | None = None
        self._depth = 0  # open <div>s
        self._inner_rows = 0  # <tr>s opened inside an item, e.g. a table in its description
        self._capture: tuple[str, int] | None = None  # (field, depth where it started)
        self._buffer: list[str] = []
        self._link: dict[str, str] | None = None
        self._file_name: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        classes = values.get("class", "").split()
        if tag == "tr" and "agenda-item-information" in classes:
            parent = next((c.rsplit("-", 1)[1] for c in classes if c.startswith("agenda-item-children-of-")), "0")
            self._row = {"id": values.get("data-agendaitemid"), "parent": parent, "count": None}
            self.rows.append(self._row)
            return
        if tag == "a" and (values.get("data-documentid") or _FILE_LINK.search(values.get("href", ""))):
            # Viewable files carry data-documentid; "download original" links (e.g. an email) only file=<id>.
            document = values.get("data-documentid") or _FILE_LINK.search(values["href"]).group(1)
            self._link = {"href": values.get("href", ""), "document": document, "icon": "", "label": "",
                          "viewable": "1" if values.get("data-documentid") else "",
                          "item": (self._row or {}).get("id") or values.get("data-foragendaitem") or "0"}
            return
        if self._link is not None:
            if tag == "img":
                self._link["icon"], self._link["label"] = values.get("src", ""), values.get("alt", "")
            elif tag == "span" and "fileNameValue" in classes:
                self._file_name = []
            return
        if self._row is None:
            return
        if tag == "tr":
            self._inner_rows += 1
        elif tag == "div":
            self._depth += 1
            field = next((self._BLOCKS[c] for c in classes if c in self._BLOCKS), None)
            if field and self._capture is None:
                self._capture, self._buffer = (field, self._depth), []
        elif tag == "span" and "attachment-count" in classes:
            self._capture, self._buffer = ("count", self._depth), []
        if self._capture is not None and self._capture[0] in ("title", "description") and tag in ("li", "p", "br"):
            self._buffer.append(" ")

    def handle_data(self, data: str) -> None:
        if self._file_name is not None:
            self._file_name.append(data)
        if self._capture is not None:
            self._buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "span" and self._file_name is not None and self._link is not None:
            self._link["name"] = " ".join("".join(self._file_name).split())
            self._file_name = None
            return
        if tag == "a" and self._link is not None:
            self.files.append(self._link)
            self._link = None
            return
        if self._row is None:
            return
        if tag == "span" and self._capture is not None and self._capture[0] == "count":
            text = "".join(self._buffer).strip()
            self._row["count"] = int(text) if text.isdigit() else None
            self._capture = None
        elif tag == "div":
            if self._capture is not None and self._capture[1] == self._depth:
                field, text = self._capture[0], " ".join("".join(self._buffer).split())
                if field == "type":
                    text = text.removeprefix("Agenda Item Type:").strip()
                elif field == "description":
                    text = text.removeprefix("Description:").strip()
                self._row[field] = text
                self._capture = None
            self._depth -= 1
        elif tag == "tr" and self._inner_rows:
            self._inner_rows -= 1
        elif tag == "tr":
            self._row, self._depth, self._capture = None, 0, None


def file_url(listed: dict[str, str], org: str) -> tuple[str, str | None]:
    """An attachment's document URL and format. A viewable file (data-documentid) comes as a PDF from DownloadPDF,
    which converts Word files too (checked on a .doc minutes file, Northwest ISD); a "download original" link (an
    email, say) is kept as the page gives it, its format from the type icon."""
    if listed.get("viewable"):
        return f"{ORIGIN}/Documents/DownloadPDF/{listed['document']}?org={org}", "pdf"
    label = listed.get("label", "").strip()
    icon = urlsplit(listed.get("icon", "")).path.rsplit("/", 1)[-1]
    extension = (label[: -len(" Icon")] if label.endswith(" Icon") else (icon.rsplit(".", 1)[0] if "." in icon else "")).lower()
    return urljoin(ORIGIN + "/", listed.get("href") or ""), extension or None


def parse_agenda_page(page_html: str, org: str, meeting_id: str) -> tuple[list[Item], list[tuple[Document, str]]]:
    """An agenda page -> items in agenda order, numbered as printed ("III.A.1"), with their attachments; and every
    attachment with the title of the item it sits under (for finding minutes)."""
    parser = _AgendaParser()
    parser.feed(page_html or "")
    parser.close()
    heading_by_id: dict[str, str] = {}
    items: list[Item] = []
    by_id: dict[str, Item] = {}
    for row in parser.rows:
        title = row.get("title") or ""
        number = _ITEM_NUMBER.match(title)
        item_no = number.group(1).replace("(", "").replace(")", ".").rstrip(".") if number else None
        title = title[number.end():].strip() if number else title
        item_id, parent = row.get("id") or "", row.get("parent", "0")
        # The top-level ancestor names the section: "III. ITEMS FOR DISCUSSION AND/OR ACTION" for III.A.1.
        heading_by_id[item_id] = f"{item_no}. {title}" if parent == "0" and item_no else heading_by_id.get(parent, title)
        item = Item(meeting_id=meeting_id, number=item_no, title=title or "(untitled)",
                    section=heading_by_id.get(parent) if parent != "0" else None, text=row.get("description") or None)
        items.append(item)
        by_id[item_id] = item
    files = []
    for listed in parser.files:
        item = by_id.get(listed.get("item", ""))
        url, fmt = file_url(listed, org)
        doc = Document(url=url, kind="item", title=listed.get("name") or f"Document {listed['document']}",
                       meeting_id=meeting_id, item_no=item.number if item else None, format=fmt)
        if item:
            item.attachments.append(doc)
        files.append((doc, item.title if item else ""))
    return items, files


def _type_words(text: str) -> set[str]:
    t = text.lower().replace("work-session", "work session")
    return {w for w in _TYPE_WORDS if w in t}


def pick_minutes(meeting_title: str, candidates: list[tuple[Document, str]]) -> list[tuple[Document, str]]:
    """Of the minutes files dated on the meeting's day, those for this meeting: a special meeting's minutes say
    "special", a regular one's say "regular" or nothing."""
    words = _type_words(meeting_title) - {"closed"}
    if len(candidates) <= 1 or not words:
        return candidates
    same = [c for c in candidates if _type_words(c[0].title) & words]
    if same:
        return same
    plain = [c for c in candidates if not _type_words(c[0].title) - {"closed"}]
    return plain or candidates


@register
class BoardBook:
    name, transport = "boardbook", "http"
    LATER_AGENDAS = 6  # agendas read after a meeting with no Minutes link, looking for its minutes
    LATER_DAYS = 120

    def __init__(self, http: Fetch) -> None:
        self.http = http
        self._pages: dict[str, tuple[str, list[Row]]] = {}  # org -> (organisation name, rows)

    # ------------------------------------------------------------------ detect
    def detect(self, url: str, html: str | None = None) -> str | None:
        org = org_of(url)
        if org or not html:
            return org
        for m in _URL_IN_HTML.finditer(html):
            org = org_of(unescape(m.group(0)))
            if org:
                return org
        return None

    # ------------------------------------------------------------------ fetching
    def _get(self, url: str):
        r = self.http.get(url)
        if r.bot_wall:
            raise PortalError(f"BoardBook stopped at a bot wall or block ({url}, HTTP {r.status}): never solved")
        return r

    def _organization(self, org: str) -> tuple[str, list[Row]]:
        if org not in self._pages:
            url = f"{ORIGIN}/Public/Organization/{org}"
            r = self._get(url)
            if not r.ok:
                raise PortalError(f"BoardBook organisation page {url}: HTTP {r.status} {r.error or ''}".strip())
            html = r.text
            if "row-for-" not in html and "Public View" not in html:
                raise PortalError(f"BoardBook organisation page {url}: no meetings table (not an organisation page)")
            self._pages[org] = (organization_name(html), parse_organization_page(html))
        return self._pages[org]

    def _row(self, org: str, meeting: Meeting) -> Row:
        for row in self._organization(org)[1]:
            if row.key == str(meeting.id):
                return row
        raise PortalError(f"BoardBook meeting {meeting.id} is not on organisation {org}'s public page any more")

    # ------------------------------------------------------------------ meetings
    def meetings(self, client: str, since: date, until: date) -> Listing:
        name, rows = self._organization(client)
        meetings, notes = [], []
        for row in rows:
            if not since <= row.when <= until:
                continue
            kind, governing = meeting_kind(row)
            board = row.unit if row.unit and not row.board_row else (name or "Board")
            meetings.append(Meeting(id=row.key, date=row.when, title=row.title, board=board, kind=kind,
                                    governing=governing, url=row.links.get("Agenda") or f"{ORIGIN}/Public/Organization/{client}"))
        if rows and since < rows[0].when:
            notes.append(f"BoardBook's public list for {client} starts at {rows[0].when.isoformat()}: older meetings are "
                         "not shown (the tenant's retention setting; some tenants keep about 13 months)")
        if not rows:
            notes.append(f"BoardBook organisation {client} lists no meetings")
        dates = sorted({r.when for r in rows})
        for before, after in zip(dates, dates[1:]):
            # a hole in the public list that overlaps the window (Grayslake: nothing from May 2022 to Nov 2024)
            if (after - before).days > 120 and before < until and after > since:
                notes.append(f"no meetings listed between {before.isoformat()} and {after.isoformat()}: BoardBook's "
                             "public list leaves out meetings the tenant hides or no longer shows")
        with_minutes = [r for r in rows if since <= r.when <= until and "Minutes" in r.links]
        if meetings and not with_minutes:
            notes.append("no meeting in the window links its own minutes: minutes are looked for on later agendas "
                         "(documents())")
        return Listing(meetings, notes)

    # ------------------------------------------------------------------ documents
    def _viewer_document(self, page_url: str, kind: str, title: str, meeting_id: str, org: str) -> Document:
        """A minutes or notice page is a document viewer; its hidden NewDocumentViewerDocumentID is the PDF's id."""
        r = self._get(page_url)
        if r.ok:
            m = _DOC_ID.search(r.text)
            if m:
                h3 = re.search(r"<h3>(.*?)</h3>", r.text, re.S)
                return Document(f"{ORIGIN}/Documents/DownloadPDF/{m.group(1)}?org={org}", kind,
                                _text(h3.group(1)) if h3 else title, meeting_id, format="pdf")
        return Document(r.final_url or page_url, kind, title, meeting_id, format="html")

    def _supplementals(self, page_url: str, org: str, meeting_id: str) -> list[Document]:
        r = self._get(page_url)
        if not r.ok:
            return [Document(page_url, "other", "Supplemental documents (list page)", meeting_id, format="html")]
        _, files = parse_agenda_page(r.text, org, meeting_id)
        return [Document(d.url, "other", d.title, meeting_id, format=d.format) for d, _ in files]

    def _agenda(self, org: str, row: Row) -> tuple[list[Item], list[tuple[Document, str]]]:
        url = row.links.get("Agenda")
        if not url:
            return [], []
        r = self._get(url)
        if not r.ok:
            raise PortalError(f"BoardBook agenda page {url}: HTTP {r.status} {r.error or ''}".strip())
        return parse_agenda_page(r.text, org, row.key)

    def _minutes_on_later_agendas(self, org: str, row: Row) -> list[Document]:
        rows = self._organization(org)[1]
        later = [r for r in rows if row.when < r.when <= row.when + timedelta(days=self.LATER_DAYS)
                 and (r.unit_id if not r.board_row else None) == (row.unit_id if not row.board_row else None)
                 and "Agenda" in r.links][: self.LATER_AGENDAS]
        for other in later:
            try:
                _, files = self._agenda(org, other)
            except PortalError:
                continue
            dated = [(doc, item) for doc, item in files
                     if (_MINUTES.search(doc.title) or _MINUTES.search(item))
                     and (loose_date(doc.title) or loose_date(item)) == row.when]
            chosen = pick_minutes(row.title, dated)
            if chosen:
                return [Document(doc.url, "minutes", f"{doc.title} (attached to the agenda of {other.when.isoformat()})",
                                 row.key, format=doc.format) for doc, _ in chosen]
        return []

    def documents(self, client: str, meeting: Meeting) -> list[Document]:
        row = self._row(client, meeting)
        docs: list[Document] = []
        if "Agenda" in row.links:
            docs.append(Document(f"{ORIGIN}/Public/DownloadAgenda/{client}?meeting={row.key}", "agenda", "Agenda",
                                 row.key, format="pdf"))
        if "Minutes" in row.links:
            docs.append(self._viewer_document(row.links["Minutes"], "minutes", "Minutes", row.key, client))
        if "PublicNotice" in row.links:
            docs.append(self._viewer_document(row.links["PublicNotice"], "notice", "Public notice", row.key, client))
        if "Supplementals" in row.links:
            docs.extend(self._supplementals(row.links["Supplementals"], client, row.key))
        _, files = self._agenda(client, row)
        docs.extend(doc for doc, _ in files)
        if "Minutes" not in row.links:
            docs.extend(self._minutes_on_later_agendas(client, row))
        return docs

    # ------------------------------------------------------------------ items
    def items(self, client: str, meeting: Meeting) -> list[Item]:
        return self._agenda(client, self._row(client, meeting))[0]
