"""NovusAgenda public sites (<client>.novusagenda.com/agendapublic/): an ASP.NET search page, read by posting its own form.

Reads:
  GET  /agendapublic/                     the search form (__VIEWSTATE and the date and meeting-type fields); its grid is
                                          empty until a search is posted
  POST /agendapublic/                     the form with "Custom Date Range" and our dates, as the Search button sends it:
                                          the meetings grid (Meeting Date, Meeting Type, Online Agenda, Download Agenda,
                                          Legal Minutes, Minutes Recap, ... as the tenant configures), 15 rows a page;
                                          the grid's "Next Page" postback is followed until the last page
  GET  /agendapublic/MeetingView.aspx?MeetingID=M&MinutesMeetingID=-1&doctype=Agenda   the agenda as HTML: section
                                          headings (A., B.) and items (1., 2.) linking CoverSheet.aspx?ItemID=I
  GET  /agendapublic/CoverSheet.aspx?ItemID=I&MeetingID=M  an item's cover sheet (its text and AttachmentViewer.ashx
                                          attachments), read only when `cover_sheets` is set: one request per item
Documents are files: DisplayAgendaPDF.ashx?MeetingID=M (agenda PDF), DisplayAgendaPDF.ashx?MinutesMeetingID=N (the legal
minutes), and the MeetingView.aspx HTML agenda. A grid column's header says the kind.

Cannot: list meetings when the site answers with its error page ("An HTTP error occurred", e.g. cityofdubuque,
greatfallsmt on 2026-11-05) or sends only the staff login (AgendaWeb/Login.aspx): PortalError. Many tenants stopped
posting years ago (rocklin ends in 2020, huntsvilleal and tamarac show nothing in 2025-26): an empty window is an empty
Listing with a note, not an error. Outcomes are not published.
Checked (2026-11-05) on Santa Rosa County School District FL (santarosa), El Paso ISD TX (episd), Gretna LA (gretna),
Mountain View Whisman SD CA (mvwsd), Rocklin CA (rocklin, 2015-2020).
"""

from __future__ import annotations

import re
from datetime import date, datetime
from html import unescape
from urllib.parse import parse_qs, urljoin, urlsplit

from . import _tree
from ._kinds import Documents, board_name, document_kind, governing, meeting_kind, missing_note
from .base import Document, Fetch, Item, Listing, Meeting, PortalError
from .registry import register

HOST = re.compile(r"^([a-z0-9][a-z0-9-]*)\.novusagenda\.com$", re.I)
IN_PAGE = re.compile(r"https?://([a-z0-9][a-z0-9-]*)\.novusagenda\.com", re.I)
VENDOR = {"www", "help", "support", "docs", "status", "api", "app", "login", "cdn", "static", "assets", "media"}
P = "ctl00$ContentPlaceHolder1$SearchAgendasMeetings$"
CS = "ctl00_ContentPlaceHolder1_SearchAgendasMeetings_"
GRID = "radGridMeetings"
MAX_PAGES = 200
_OPEN = re.compile(r"window\.open\('([^']+)'")
_PAGES = re.compile(r"(\d+)\s*items in\s*(\d+)\s*pages", re.I)
_COLUMNS = {"online agenda": ("agenda", "html"), "download agenda": ("agenda", "pdf"), "legal minutes": ("minutes", "pdf"),
            "minutes recap": ("minutes", None), "minutes": ("minutes", None), "post-agenda items": ("other", None),
            "video": ("video", None)}


def base_url(client: str) -> str:
    return f"https://{client.lower()}.novusagenda.com/agendapublic/"


def _date(text: str) -> date | None:
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            pass
    return None


def form_fields(root: _tree.Node) -> dict[str, str]:
    """Every field the page's form would post: hidden and text inputs, and each select's chosen option."""
    fields = {}
    for node in root.find_all("input"):
        name, kind = node.attrs.get("name"), node.attrs.get("type", "text").lower()
        if name and kind in ("hidden", "text"):
            fields[name] = node.attrs.get("value", "")
    for sel in root.find_all("select"):
        if sel.attrs.get("name"):
            opts = sel.find_all("option")
            chosen = next((o for o in opts if "selected" in o.attrs), opts[0] if opts else None)
            fields[sel.attrs["name"]] = chosen.attrs.get("value", chosen.text()) if chosen else ""
    return fields


def _client_state(day: date) -> str:
    s = day.strftime("%Y-%m-%d-00-00-00")
    return ('{"enabled":true,"emptyMessage":"","validationText":"%s","valueAsString":"%s","minDateStr":"1980-01-01-00-00-00",'
            '"maxDateStr":"2099-12-31-00-00-00","lastSetTextBoxValue":"%d/%d/%d"}' % (s, s, day.month, day.day, day.year))


def search_form(fields: dict[str, str], since: date, until: date) -> dict[str, str]:
    """The search form as the Search button posts it, with a custom date range and every meeting type."""
    form = dict(fields)
    form.update({"__EVENTTARGET": "", "__EVENTARGUMENT": "", P + "ddlDateRange": "cus", P + "ctl00": "-1", P + "ctl01": "-1",
                 P + "ctl02": "", P + "radCalendarFrom": since.isoformat(),
                 P + "radCalendarFrom$dateInput": f"{since.month}/{since.day}/{since.year}",
                 CS + "radCalendarFrom_dateInput_ClientState": _client_state(since), P + "radCalendarTo": until.isoformat(),
                 P + "radCalendarTo$dateInput": f"{until.month}/{until.day}/{until.year}",
                 CS + "radCalendarTo_dateInput_ClientState": _client_state(until),
                 P + "imageButtonSearch.x": "12", P + "imageButtonSearch.y": "9"})
    return form


def _href(node: _tree.Node) -> str | None:
    href = node.attrs.get("href", "")
    if href and not href.lower().startswith("javascript"):
        return href
    m = _OPEN.search(unescape(node.attrs.get("onclick", "")))
    return m.group(1) if m else None


def read_grid(html: str, client: str) -> tuple[list[dict], str | None, tuple[int, int] | None]:
    """(rows, the Next Page postback target or None, (items, pages) as the pager says)."""
    root = _tree.parse(html)
    table = root.find("table", pred=lambda n: n.attrs.get("id", "").endswith(f"{GRID}_ctl00"))
    rows, nxt = [], None
    if table is not None:
        heads = [th.text().lower() for th in table.find_all("th", cls="rgHeader")]
        for tr in table.find_all("tr", pred=lambda n: re.search(rf"{GRID}_ctl00__\d+$", n.attrs.get("id", ""))):
            cells = [td for td in tr.find_all("td") if td.parent is tr]
            row = {"date": None, "type": "", "location": "", "docs": [], "meeting_id": None}
            for head, td in zip(heads, cells):
                if "date" in head:
                    row["date"] = _date(td.text())
                elif "type" in head:
                    row["type"] = td.text()
                elif "location" in head:
                    row["location"] = td.text()
                else:
                    for a in td.find_all("a"):
                        href = _href(a)
                        if not href:
                            continue
                        url = urljoin(base_url(client), href)
                        kind, fmt = _COLUMNS.get(head, (document_kind(head), None))
                        if kind == "agenda" and "video" in (a.find("img").attrs.get("alt", "").lower() if a.find("img") else ""):
                            row["video_in_agenda"] = True
                        row["docs"].append({"url": url, "kind": kind, "title": td.text() or head.title(),
                                            "format": fmt or ("pdf" if ".ashx" in url.lower() else "html")})
                        q = parse_qs(urlsplit(url).query)
                        if "MeetingID" in q and not row["meeting_id"]:
                            row["meeting_id"] = q["MeetingID"][0]
            rows.append(row)
        link = table.find("a", pred=lambda n: n.attrs.get("title") == "Next Page" and GRID in n.attrs.get("href", ""))
        if link is not None:
            m = re.search(r"__doPostBack\('([^']+)'", unescape(link.attrs["href"]))
            nxt = m.group(1) if m else None
    pager = table.find(cls="rgPager") if table is not None else None  # a one-page grid has no pager
    m = _PAGES.search(pager.text()) if pager is not None else None
    return rows, nxt, ((int(m.group(1)), int(m.group(2))) if m else None)


def _meeting(client: str, row: dict) -> Meeting:
    title = row["type"].strip()
    board = board_name(title)
    mid = row["meeting_id"] or f"{row['date']}:{title}"
    return Meeting(id=mid, date=row["date"], title=title, board=board, kind=meeting_kind(title, board),
                   governing=governing(board),
                   url=f"{base_url(client)}MeetingView.aspx?MeetingID={mid}&MinutesMeetingID=-1&doctype=Agenda"
                   if row["meeting_id"] else base_url(client))


@register
class NovusAgenda:
    name = "novusagenda"
    transport = "http"
    cover_sheets = False  # items(): also read each item's cover sheet (its text and attachments), one request per item

    def __init__(self, http: Fetch) -> None:
        self.http = http
        self._rows: dict[tuple[str, str], dict] = {}

    def detect(self, url: str, html: str | None = None) -> str | None:
        m = HOST.match((urlsplit(url or "").hostname or "").lower())
        if m and m.group(1) not in VENDOR:
            return m.group(1)
        for m in IN_PAGE.finditer(html or ""):
            if m.group(1).lower() not in VENDOR:
                return m.group(1).lower()
        return None

    def _page(self, resp, what: str) -> str:
        if resp.bot_wall:
            raise PortalError(f"{what}: bot wall ({resp.error or f'HTTP {resp.status}'})")
        if not resp.ok:
            raise PortalError(f"{what}: {resp.error or f'HTTP {resp.status}'}")
        text = resp.text
        if "__VIEWSTATE" not in text or GRID not in text:
            said = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text))).strip()
            where = "the staff login" if "login.aspx" in (resp.final_url or "").lower() else f"a page without the meetings grid: {said[-160:]!r}"
            raise PortalError(f"{what}: the site sent {where}")
        return text

    def meetings(self, client: str, since: date, until: date) -> Listing:
        url = base_url(client)
        first = self._page(self.http.get(url), "search page")
        fields = form_fields(_tree.parse(first))
        resp = self.http.post(url, data=search_form(fields, since, until))
        html = self._page(resp, f"search {since}..{until}")
        notes, found, page = [], {}, 1
        while True:
            rows, nxt, count = read_grid(html, client)
            before = len(found)
            for row in rows:
                if row["date"] and since <= row["date"] <= until:
                    found.setdefault(row["meeting_id"] or f"{row['date']}:{row['type']}", row)
            if page > 1 and len(found) == before:  # the pager went nowhere new
                notes.append(f"page {page} repeated earlier rows: stopped there")
                break
            if not nxt or (count and page >= count[1]) or page >= MAX_PAGES:
                if count and page < count[1]:
                    notes.append(f"stopped at page {page} of {count[1]}")
                break
            form = form_fields(_tree.parse(html))
            for key in [k for k in form if k.endswith(".x") or k.endswith(".y")]:
                form.pop(key)
            form.update({"__EVENTTARGET": nxt, "__EVENTARGUMENT": ""})
            try:
                html = self._page(self.http.post(url, data=form), f"page {page + 1}")
            except PortalError as error:
                notes.append(f"{error}: later pages not read")
                break
            page += 1
        meetings = []
        for key, row in found.items():
            m = _meeting(client, row)
            self._rows[(client, m.id)] = row
            meetings.append(m)
        meetings.sort(key=lambda m: (m.date, m.title))
        if not meetings:
            notes.append(f"the site lists no meetings from {since} to {until}")
        else:
            with_minutes = sum(1 for k in found.values() if any(d["kind"] == "minutes" for d in k["docs"]))
            notes.append(f"minutes listed for {with_minutes} of {len(meetings)} meetings")
        return Listing(meetings, notes)

    def _row(self, client: str, meeting: Meeting) -> dict | None:
        if (client, meeting.id) not in self._rows and meeting.date:
            self.meetings(client, meeting.date, meeting.date)
        return self._rows.get((client, meeting.id))

    def documents(self, client: str, meeting: Meeting) -> Documents:
        row = self._row(client, meeting)
        if row is None:
            raise PortalError(f"meeting {meeting.id} not in the site's list for {meeting.date}")
        docs = Documents([Document(url=d["url"], kind=d["kind"], title=d["title"], meeting_id=meeting.id, format=d["format"])
                          for d in row["docs"]])
        if row.get("video_in_agenda"):
            docs.notes.append("the online agenda carries the meeting video")
        missing_note(docs)
        return docs

    def items(self, client: str, meeting: Meeting) -> list[Item]:
        if not re.fullmatch(r"\d+", meeting.id or ""):
            return []
        url = f"{base_url(client)}MeetingView.aspx?MeetingID={meeting.id}&MinutesMeetingID=-1&doctype=Agenda"
        resp = self.http.get(url)
        if resp.bot_wall or not resp.ok:
            raise PortalError(f"HTML agenda {url}: {resp.error or f'HTTP {resp.status}'}")
        items = parse_agenda(resp.text, client, meeting.id)
        if self.cover_sheets:
            for item in items:
                self._cover_sheet(client, item)
        return items

    def _cover_sheet(self, client: str, item: Item) -> None:
        sheet = item.attachments[0].url if item.attachments else None
        if not sheet:
            return
        resp = self.http.get(sheet)
        if not resp.ok:
            return
        root = _tree.parse(resp.text)
        seen = {sheet}
        for a in root.find_all("a", pred=lambda n: "attachmentviewer.ashx" in n.attrs.get("href", "").lower()):
            url = urljoin(base_url(client), a.attrs["href"])
            if url not in seen:
                seen.add(url)
                row = next((p for p in a.ancestors() if p.tag == "tr"), a)
                item.attachments.append(Document(url=url, kind="item", title=row.text() or a.text(), meeting_id=item.meeting_id,
                                                 item_no=item.number))
        item.text = root.text()[:4000] or None


_OUTLINE = re.compile(r"^(?:[A-Z]|[IVXL]{1,5}|\d{1,3}|[a-z]|[ivx]{1,4})[.)]$")


def parse_agenda(html: str, client: str, meeting_id: str) -> list[Item]:
    """The MeetingView agenda is an outline: each row puts its number ("VI.", "B.", "1.") in a column as deep as its level.
    A numbered row with deeper rows under it is a section; one without is an item, with its cover sheet when it links
    one. Unnumbered rows under an item are its text."""
    root = _tree.parse(html)
    rows = []
    for tr in root.find_all("tr"):
        if tr.find("tr") is not None:  # a row that holds a whole nested table: its inner rows are read instead
            continue
        cells = [td for td in tr.find_all("td") if td.parent is tr]
        texts = [td.text() for td in cells]
        filled = [i for i, t in enumerate(texts) if t]
        if not filled:
            continue
        link = tr.find("a", pred=lambda n: "coversheet.aspx" in n.attrs.get("href", "").lower())
        first = texts[filled[0]]
        numbered = bool(_OUTLINE.match(first)) and len(filled) > 1
        title = (link.text() if link is not None else texts[filled[1]]) if numbered else " ".join(texts[i] for i in filled)
        rows.append({"level": filled[0], "number": first.rstrip(".)") if numbered else None, "title": title,
                     "link": urljoin(base_url(client), link.attrs["href"]) if link is not None else None})
    items, stack = [], []  # stack: (level, "number. title") of the open sections
    numbered = [r for r in rows if r["number"]]
    for i, row in enumerate(rows):
        if not row["number"]:
            if items and row["level"] > items[-1][0] and not row["link"]:  # an item's own note under it
                items[-1][1].text = f"{items[-1][1].text or ''} {row['title']}".strip()
            continue
        while stack and stack[-1][0] >= row["level"]:
            stack.pop()
        nxt = next((r for r in numbered[numbered.index(row) + 1:]), None)
        if nxt is not None and nxt["level"] > row["level"] and not row["link"]:
            stack.append((row["level"], f"{row['number']}. {row['title']}"))
            continue
        attachments = [Document(url=row["link"], kind="item", title="Cover sheet", meeting_id=meeting_id,
                                item_no=row["number"], format="html")] if row["link"] else []
        items.append((row["level"], Item(meeting_id=meeting_id, number=row["number"], title=row["title"],
                                         section=stack[-1][1] if stack else None, attachments=attachments)))
    return [item for _, item in items]
