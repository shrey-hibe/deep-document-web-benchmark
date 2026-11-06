"""Simbli (eBOARDsolutions; simbli.eboardsolutions.com, one number per district: S=<id>): the meeting listing, each
meeting's agenda tree and item files, and its published minutes as a PDF.

Every district shares one host; the client id is the district number ("36030744"), or a short link's path
("csba/bassettusd") that the site redirects to it. The adapter reads what the district's own pages read:

  GET  /SB_Meetings/SB_MeetingListing.aspx?S=<id>   the listing page; its grid holds the latest 50 meetings in the page
                                                    itself (id, title, local date and time, type, minutes status,
                                                    encrypted id), with the page's session token (sToken) and encrypted
                                                    district id (enDID)
  POST /Services/api/GetMeetingListing              the next 50, newest first: what the page sends when a visitor
                                                    scrolls to the bottom (the page's own grid settings, RecordStart)
  GET  /Services/api/MeetingView/GetItemsTreeDTO/?sct=&endid=&enmid=
                                                    a meeting's agenda as a tree (bullet label, title, has-files flag)
  GET  /Services/api/GetSupportingDocuments/?sct=&endid=&enentityid=<meeting>&enitemid=<item>
                                                    one item's files (AttachmentID, title, file name)
  file /Meetings/Attachment.aspx?S=&AID=&MID=        an item's file (redirects to it)
  minutes: the meeting's "Print Minutes" page (/SB_Meetings/PrintMinutesSettingsAnonymous.aspx?S=&enmid=) asks the print
           service to make the published minutes into a PDF and then shows it; the adapter opens that page in a side
           tab, as a visitor clicking "Print Minutes" would, and returns the PDF's own address
           (https://app1.eboardsolutions.com/api/PrintMinutes/ViewPrintMinutes?fid=Minutes_<S>_<MID>_<stamp>.pdf&...)

Transport: the browser tier only (ddwb.portals.browser, decision #123). The host sits behind Imperva, which answers a
plain client with its challenge page (checked 2026-11-05: HTTP 200 with the Incapsula frame). A challenge frame, a 403
or a challenge page stops the run with a PortalError: never solved.

Minutes: the published minutes, made into a PDF; when a meeting's minutes are not published in Simbli, documents()
looks at the next two meetings of the same type (one request for each agenda, one for each minutes item) for a
minutes file whose title carries the meeting's date. Minutes filed on a meeting's own agenda are an earlier meeting's:
they come back as that item's files (kind "item").

What it cannot do: the agenda has no file of its own (the print service makes one only on request), so the agenda
document is the meeting's page; documents() reads the files only of items about minutes (one request each), and items()
reads every item's files (one request per item with files: slow on long consent agendas); outcomes and item text are
not read (each is another request per item); a minutes PDF is made on request (about 20 to 40 s), and its address is
the print service's and may expire, so documents() makes it again when asked; policy-only districts list no meetings.
Files and the minutes PDF are fetched through the adapter's `browser` (a plain client meets Imperva there too).

Reads the grid rows in the page, the scroll-down call, the items tree and the challenge rules, plus item files and the
minutes PDF. Checked on 2026-11-05 (meetings 2024/2025/2026,
of which minutes published): 36030744 Covina-Valley Unified, CA (18/20/17; 18/20/16), 36030394 Red Bluff Union
Elementary, CA (19/20/21; 16/18/16), 170010 Sullivan West Central, NY (19/19/17; 19/19/15); a minutes PDF made and
read on Covina-Valley (application/pdf), item files read as PDFs through Attachment.aspx.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, parse_qsl, quote, urlsplit

from .base import Document, Item, Listing, Meeting, PortalError
from .boarddocs import FOLLOW_DAYS, FOLLOWING, date_pattern
from .browser import Challenge
from .registry import register

BASE = "https://simbli.eboardsolutions.com"
HOSTS = re.compile(r"(?:^|\.)(?:simbli|assistive)\.eboardsolutions\.com$|(?:^|\.)simbli\.com$", re.I)
_S_IN_TEXT = re.compile(r"(?:simbli|assistive)\.eboardsolutions\.com/[^\s\"'<>]*?[?&](?:amp;)?S=(\d+)", re.I)
_LEADING_NUMBER = re.compile(r"^\s*([0-9A-Za-z]{1,4})[.)]\s+")
_NUMBER_LIKE = re.compile(r"\d{1,3}|[A-Za-z]|([A-Za-z])\1|[IVXLC]{1,6}|[ivxlc]{1,6}")  # not "Dr" in "Dr. Lisa Simon"
_COMMITTEE = re.compile(r"\bcommittee\b|\badvisory\b|\btask force\b|\bcommission\b|\bcouncil\b|\bfoundation\b|\bstudent\b", re.I)
_GRID_READY = "() => { const g = window.ContentPlaceHolder1_MeetingGrid_newGrid; return !!(g && Array.isArray(g.dataBuffer)); }"
_MORE_BODY = """([start, size]) => {
  const g = window.ContentPlaceHolder1_MeetingGrid_newGrid;
  return {body: Object.assign({}, meetingCustGrd, {RecordCount: size, RecordStart: start, SortColName: g.sortColumn,
            IsSortDesc: g.sortDec, FilterExp: g.filterExp, IsUserLoggedIn: isLoggedInUser, SchoolID: schoolID,
            UserID: currUserID, TimeZone: timezoneDiff, ConnectionString: constr}),
          token: (typeof getCoreAuthTokenFromCookies === "function" ? getCoreAuthTokenFromCookies() : "") || ""};
}"""
PAGE_SIZE = 50
MINUTES_WAIT_MS = 90_000


def _true(value) -> bool:
    """The service writes flags as booleans or as the strings "True"/"False"."""
    return value is True or str(value).strip().lower() == "true"


def district_from(url: str, _unwraps: int = 0) -> str | None:
    """The district number in a Simbli address (S=), through an Outlook safe-link wrapper; a short link's path
    ("csba/bassettusd") when the address carries no number."""
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower()
    if "safelinks.protection.outlook.com" in host and _unwraps < 2:
        wrapped = parse_qs(parts.query).get("url")
        return district_from(wrapped[0], _unwraps + 1) if wrapped else None
    if not HOSTS.search(host):
        return None
    for key, value in parse_qsl(parts.query):
        if key.lower() == "s" and value.isdigit():
            return value
    path = parts.path.strip("/")
    if path and not re.search(r"\.(aspx|ashx|html?)$", path, re.I) and re.fullmatch(r"[\w-]+(/[\w-]+)?", path):
        return path.lower()
    return None


def listing_url(district: str) -> str:
    return f"{BASE}/SB_Meetings/SB_MeetingListing.aspx?S={int(district)}"


def meeting_url(district: str, meeting_id: str) -> str:
    return f"{BASE}/SB_Meetings/ViewMeeting.aspx?S={int(district)}&MID={int(meeting_id)}"


def attachment_url(district: str, attachment_id, meeting_id: str) -> str:
    return f"{BASE}/Meetings/Attachment.aspx?S={int(district)}&AID={int(attachment_id)}&MID={int(meeting_id)}"


def _js_string(html: str, name: str) -> str | None:
    m = re.search(rf"\b{name}\s*=\s*'([^']*)'", html or "") or re.search(rf'\b{name}\s*=\s*"([^"]*)"', html or "")
    return m.group(1) if m else None


def parse_listing_page(html: str) -> tuple[list[dict], str | None, str | None]:
    """The listing page -> (grid rows, sToken, enDID). Rows are the JSON the page assigns to its grid."""
    rows: list[dict] = []
    m = re.search(r"MeetingGrid_newGrid\.dataBuffer\s*=\s*", html or "")
    if m:
        try:
            rows, _ = json.JSONDecoder().raw_decode(html, m.end())
        except ValueError:
            rows = []
    return (rows if isinstance(rows, list) else []), _js_string(html, "sToken"), _js_string(html, "enDID")


def _when(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).rstrip("Z")) if value else None
    except ValueError:
        return None


def meeting_kind(title: str, kind: str) -> str:
    n = f"{title} {kind}"
    if re.search(r"\bwork(ing)?[ -]?sessions?\b|\bworkshops?\b|\bstudy sessions?\b|\bretreat\b", n, re.I):
        return "work_session"
    if re.search(r"\bspecial\b|\bemergency\b", n, re.I):
        return "special"
    if _COMMITTEE.search(kind or "") or re.search(r"\bcommittee\b", title or "", re.I):
        return "committee"
    if re.search(r"\bregular\b|\bboard (of \w+ )?meeting\b|\bboard of (education|trustees)\b", n, re.I):
        return "regular"
    return "other"


def governing(kind: str, title: str) -> bool | None:
    """The type the district gives the meeting ("Board Meeting", "Citizen's Bond Oversight Committee") says whose it is."""
    if _COMMITTEE.search(kind or "") or (not kind and _COMMITTEE.search(title or "")):
        return False
    if re.search(r"\bboard\b|\btrustees\b", f"{kind} {title}", re.I):
        return True
    return None


def rows_to_meetings(rows: list[dict], district: str) -> list[Meeting]:
    out = []
    for row in rows:
        start = _when(row.get("MM_DateTime"))
        if not row.get("Master_MeetingID") or start is None or row.get("ML_IsPublic") is False:
            continue
        title = " ".join(str(row.get("MM_MeetingTitle") or "").split()) or "Meeting"
        kind = " ".join(str(row.get("ML_TypeTitle") or "").split())
        mid = str(row["Master_MeetingID"])
        out.append(Meeting(mid, start.date(), title, board=kind, kind=meeting_kind(title, kind),
                           governing=governing(kind, title), url=meeting_url(district, mid)))
    return sorted(out, key=lambda m: (m.date, m.id))


def parse_items_tree(tree: dict, meeting_id: str) -> list[tuple[Item, dict]]:
    """GetItemsTreeDTO -> (item, its node) in agenda order, numbered by the path of bullet labels ("V.A")."""
    out: list[tuple[Item, dict]] = []

    def walk(nodes, prefix: str, section: str | None) -> None:
        for node in sorted(nodes or [], key=lambda n: int(str(n.get("SeqUnderParent") or 0) or 0)):
            if _true(node.get("Deleted")):
                continue
            title = " ".join(str(node.get("Title") or "").split())
            own = str(node.get("bulletLabel") or "").strip().rstrip(".")
            lead = _LEADING_NUMBER.match(title)
            if lead and (lead.group(1) == own or (not own and _NUMBER_LIKE.fullmatch(lead.group(1)))):
                own = own or lead.group(1)
                title = title[lead.end():]
            number = f"{prefix}.{own}" if prefix and own else (own or prefix or None)
            out.append((Item(meeting_id, number, title or "(untitled)", section=section), node))
            walk(node.get("Children"), number or "", section or (f"{own}. {title}" if own else title))

    walk((tree or {}).get("Items"), "", None)
    return out


def parse_supporting(body: dict, district: str, meeting_id: str, item_no: str | None) -> list[Document]:
    """GetSupportingDocuments -> the item's files (Attachment.aspx addresses)."""
    docs = []
    for a in (body or {}).get("Attachment") or []:
        if not a.get("AttachmentID"):
            continue
        name = a.get("FileName") or a.get("OrigFileName") or ""
        title = a.get("Title") or name or "Attachment"
        kind = "minutes" if re.search(r"\bminutes?\b", f"{title} {name}", re.I) else "item"
        fmt = a.get("FileExtension") or (name.rsplit(".", 1)[-1] if "." in name else None)
        docs.append(Document(attachment_url(district, a["AttachmentID"], meeting_id), kind, title, meeting_id=meeting_id,
                             item_no=item_no, format=fmt.lower() if fmt else None))
    for link in (body or {}).get("HyperLink") or []:
        url = link.get("Url") or link.get("URL") or link.get("HyperLink")
        if url:
            docs.append(Document(url, "item", link.get("Title") or url, meeting_id=meeting_id, item_no=item_no))
    return docs


def minutes_pdf_url(final_url: str) -> str | None:
    """The PDF's own address from the print viewer's address (its pdfurl), or the address itself when it is the PDF."""
    if urlsplit(final_url or "").path.rstrip("/").endswith("ViewPrintMinutes"):
        return final_url
    pdf = parse_qs(urlsplit(final_url or "").query).get("pdfurl")  # decoded once already: the PDF's address as it is
    return pdf[0] if pdf else None


@register
class Simbli:
    name, transport = "simbli", "browser"

    def __init__(self, browser=None) -> None:
        """`browser`: a ddwb.portals.browser.Browser; None makes one on first use."""
        self.browser = browser
        self._districts: dict[str, str] = {}  # short link -> district number
        self._session: dict[str, tuple[str, str]] = {}  # district -> (sToken, enDID) of its open listing page
        self._rows: dict[tuple[str, str], dict] = {}  # (district, meeting id) -> its grid row
        self._current: str | None = None  # the district whose listing page is open in the main tab

    # ------------------------------------------------------------------------------------------- recognising a district
    def detect(self, url: str, html: str | None = None) -> str | None:
        found = district_from(url)
        if found or not html:
            return found
        ids = Counter(_S_IN_TEXT.findall(html))
        return ids.most_common(1)[0][0] if ids else None

    # ------------------------------------------------------------------------------------------- transport
    def _b(self):
        if self.browser is None:
            from .browser import Browser
            self.browser = Browser()
        return self.browser

    def _district(self, client: str) -> str:
        client = str(client).strip().strip("/")
        if client.isdigit():
            return client
        if client not in self._districts:
            visit = self._b().open(f"{BASE}/{client}", side=True)
            number = district_from(visit.final_url)
            if not (number and number.isdigit()):
                ids = Counter(re.findall(r"[?&](?:amp;)?S=(\d+)", visit.html)).most_common(1)
                number = ids[0][0] if ids else None
            if not number:
                raise PortalError(f"simbli {client!r}: no district number behind the short link")
            self._districts[client] = number
        return self._districts[client]

    def _listing(self, district: str) -> list[dict]:
        """Open the district's listing page in the main tab (the page every later call is sent from)."""
        visit = self._b().open(listing_url(district), ready=_GRID_READY)
        rows, token, end = parse_listing_page(visit.html)
        if not token or not end:
            raise PortalError(f"simbli {district}: the listing page has no grid session (sToken, enDID): changed format?")
        self._session[district], self._current = (token, end), district
        return rows

    def _ensure(self, district: str) -> tuple[str, str]:
        if self._current != district or district not in self._session:
            self._listing(district)
        return self._session[district]

    def _json(self, url: str, step: str, body: dict | None = None, headers: dict | None = None):
        b = self._b()
        r = b.post(url, json_body=body, headers=headers, use_cache=False) if body is not None else \
            b.get(url, use_cache=False)
        if r.bot_wall:
            raise Challenge(f"simbli {step}: a block or challenge page ({r.error or f'HTTP {r.status}'}): stopped there, not solved")
        if not r.ok:
            raise PortalError(f"simbli {step}: {r.error or f'HTTP {r.status}'}")
        if r.body.lstrip()[:1] == b"<":
            raise Challenge(f"simbli {step}: a page came back where data was expected (a challenge?): stopped")
        return json.loads(r.body)

    # ------------------------------------------------------------------------------------------- the interface
    def meetings(self, client: str, since: date, until: date) -> Listing:
        district = self._district(client)
        rows = self._listing(district)
        notes: list[str] = []
        total = max([int(r.get("MeetingsCount") or 0) for r in rows] or [0])
        while rows and len(rows) < total:
            oldest = min((_when(r.get("MM_DateTime")) for r in rows if _when(r.get("MM_DateTime"))), default=None)
            if oldest is not None and oldest.date() < since:
                break
            try:
                more = self._b().evaluate(_MORE_BODY, [len(rows) + 1, PAGE_SIZE])
                batch = self._json(f"{BASE}/Services/api/GetMeetingListing", "older meetings", body=more["body"],
                                   headers={"Accept": "application/json", "Authorization": f"Bearer {more['token']}"})
            except Challenge as wall:
                notes.append(f"older meetings not read (after {len(rows)} of {total}): {wall}")
                break
            except (PortalError, ValueError, KeyError) as error:
                notes.append(f"older meetings not read (after {len(rows)} of {total}): {error}")
                break
            seen = {r.get("Master_MeetingID") for r in rows}
            fresh = [r for r in batch or [] if isinstance(r, dict) and r.get("Master_MeetingID") not in seen]
            if not fresh:
                break
            rows += fresh
        for row in rows:
            self._rows[(district, str(row.get("Master_MeetingID")))] = row
        hidden = sum(1 for r in rows if r.get("ML_IsPublic") is False)
        if hidden:
            notes.append(f"{hidden} meeting(s) the listing marks as not public were left out")
        meetings = [m for m in rows_to_meetings(rows, district) if m.date and since <= m.date <= until]
        if not rows:
            notes.append("the listing shows no meetings (a district that uses Simbli only for policies lists none)")
        return Listing(meetings, notes)

    def _row(self, district: str, meeting: Meeting) -> dict:
        if (district, meeting.id) not in self._rows and meeting.date:
            self.meetings(district, meeting.date, meeting.date)
        row = self._rows.get((district, meeting.id))
        if not row or not row.get("EncrMeetingId"):
            raise PortalError(f"simbli {district}: meeting {meeting.id} is not in the listing")
        return row

    def _tree(self, district: str, meeting: Meeting) -> tuple[list[tuple[Item, dict]], dict, tuple[str, str]]:
        row = self._row(district, meeting)
        token, end = self._ensure(district)
        q = lambda s: quote(s, safe="")  # noqa: E731
        tree = self._json(f"{BASE}/Services/api/MeetingView/GetItemsTreeDTO/?sct={q(token)}&endid={q(end)}"
                          f"&enmid={q(row['EncrMeetingId'])}&enuid=&v=", "agenda tree")
        return parse_items_tree(tree, meeting.id), row, (token, end)

    def _files(self, district: str, row: dict, session: tuple[str, str], item: Item, node: dict) -> list[Document]:
        token, end = session
        q = lambda s: quote(s, safe="")  # noqa: E731
        body = self._json(f"{BASE}/Services/api/GetSupportingDocuments/?sct={q(token)}&endid={q(end)}"
                          f"&enentityid={q(row['EncrMeetingId'])}&enitemid={q(str(node.get('ID') or ''))}", "item files")
        return parse_supporting(body, district, item.meeting_id, item.number)

    def minutes_pdf(self, client: str, meeting: Meeting) -> str:
        """Have the print service make the meeting's published minutes into a PDF, as "Print Minutes" does; its address."""
        district = self._district(client)
        row = self._row(district, meeting)
        page = f"{BASE}/SB_Meetings/PrintMinutesSettingsAnonymous.aspx?S={int(district)}&enmid={quote(row['EncrMeetingId'], safe='')}"
        visit = self._b().open(page, until_url=r"CommonPrint/ViewFile|ViewPrintMinutes", ready_ms=MINUTES_WAIT_MS, side=True)
        url = minutes_pdf_url(visit.final_url)
        if not url:
            raise PortalError(f"simbli {district}: the print page ended at {visit.final_url[:120]}, not at a PDF")
        return url

    def _minutes_files(self, district: str, meeting: Meeting) -> list[Document]:
        """The files of a meeting's items about minutes (its "approval of minutes" items): one request each."""
        items, row, session = self._tree(district, meeting)
        return [doc for item, node in items if _true(node.get("HasAttachment")) and re.search(r"\bminutes\b", item.title, re.I)
                for doc in self._files(district, row, session, item, node)]

    def following(self, client: str, meeting: Meeting) -> list[Meeting]:
        """The next meetings of the same type (up to FOLLOWING, within FOLLOW_DAYS, already held): where a district
        that files its minutes as attachments approves them."""
        district = self._district(client)
        row = self._row(district, meeting)
        last = min(meeting.date + timedelta(days=FOLLOW_DAYS), date.today())
        rows = [r for (d, _), r in self._rows.items() if d == district and r.get("ML_TypeTitle") == row.get("ML_TypeTitle")]
        return [m for m in rows_to_meetings(rows, district) if meeting.date < m.date <= last][:FOLLOWING]

    def documents(self, client: str, meeting: Meeting) -> list[Document]:
        district = self._district(client)
        items, row, session = self._tree(district, meeting)
        docs = []
        if items:
            docs.append(Document(meeting_url(district, meeting.id), "agenda",
                                 f"Agenda: {meeting.title} (the meeting's page; no agenda file until printed)",
                                 meeting_id=meeting.id, format="html"))
        if str(row.get("MinutesStatus") or "").upper() == "PUBLISHED":
            docs.append(Document(self.minutes_pdf(district, meeting), "minutes", f"Minutes: {meeting.title}",
                                 meeting_id=meeting.id, format="pdf"))
        elif meeting.date:  # not published in Simbli: filed as a file on a later meeting's approval item?
            pattern = date_pattern(meeting.date)
            for later in self.following(district, meeting):
                found = [Document(d.url, "minutes", f"{d.title} (filed with item {d.item_no} of the {later.date} meeting)",
                                  meeting_id=meeting.id, format=d.format)
                         for d in self._minutes_files(district, later) if d.kind == "minutes" and pattern.search(d.title)]
                if found:
                    docs += found
                    break
        for item, node in items:  # minutes filed here are an earlier meeting's, approved at this one: this item's files
            if _true(node.get("HasAttachment")) and re.search(r"\bminutes\b", item.title, re.I):
                docs += [Document(d.url, "item", d.title, d.meeting_id, d.item_no, d.format)
                         for d in self._files(district, row, session, item, node)]
        return docs

    def items(self, client: str, meeting: Meeting) -> list[Item]:
        district = self._district(client)
        items, row, session = self._tree(district, meeting)
        for item, node in items:
            if _true(node.get("HasAttachment")):
                item.attachments = self._files(district, row, session, item, node)
        return [item for item, _ in items]

    def close(self) -> None:
        if self.browser is not None:
            self.browser.close()
