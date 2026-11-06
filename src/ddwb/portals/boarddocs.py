"""BoardDocs (go.boarddocs.com/<state>/<site>/Board.nsf): every board's meetings, the printable agenda with its items,
files and votes, and the minutes the board keeps in BoardDocs.

A BoardDocs site is one database per body; its single-page app fills itself from small endpoints in it. The adapter
calls the same ones the page calls (client id "<state>/<site>", e.g. "ca/orangeusd"):

  GET  /<state>/<site>/Board.nsf/Public          the portal page: its boards ("committees": id and name)
  POST /BD-GetMeetingsList?open                  current_committee_id=<board>: every meeting of one board, all years,
                                                 as JSON (unique, name, numberdate)
  GET  /PRINT-AgendaDetailed?open&id=<meeting>   the whole agenda as one printable page: numbered sections, items
                                                 (number, title, type, recommended action, text), each item's files
                                                 (/files/<id>/$file/<name>) and its motions and votes
  GET  /BD-GetMinutes?open&id=<meeting>          the minutes, as HTML, when the board keeps them in BoardDocs (the
                                                 page's "View Minutes"); empty when it does not
So a board's list costs one request, and a meeting's agenda, items, files and minutes two more.

Transport: plain HTTP first (the Fetch given, with the honest research user agent). BoardDocs sits behind CloudFront,
which answers scripts with "Request blocked" (403); the adapter then switches to the browser tier
(ddwb.portals.browser, decision #123: it opens the portal page as a visitor would and sends the same requests from it)
and says so in the listing's notes. A block inside the browser (CloudFront's 403 again, a challenge) stops the run with
a PortalError: never solved. CloudFront also counts the page's own loading (about 60 requests), so the portal page is
opened once per site and every call after it is paced like a person; it still answered the browser with a 403 after
several back-to-back runs within a quarter of an hour (2026-11-05), so leave a site some minutes between runs.

Minutes come from BD-GetMinutes; when a board keeps none there, documents() looks at the agendas of the board's next two
meetings (FOLLOWING, within FOLLOW_DAYS: one request each) for a minutes file whose title or name carries this
meeting's date (the "approval of minutes" item), and returns it as this meeting's minutes. Minutes filed on a meeting's
own agenda are an earlier meeting's: they come back as that agenda's files (kind "item").

What it cannot do: minutes a board keeps only as files elsewhere (the Library tab, another site), or filed without
the meeting's date in their title, are not found; the agenda's "Download as PDF" is made in the visitor's browser, so the agenda
document is the printable HTML page; video links in meeting descriptions are not read. Documents are fetched through
`fetch_for(client)` (the transport the adapter ended on): a plain client gets CloudFront's 403 for them too.

Uses BoardDocs' own endpoints, the committee id and its block rules, and reads the agenda from the printable page instead
of one request per item. Checked on 2026-11-05 (plain HTTP got CloudFront's
403 every time; the browser tier read the rest): ca/orangeusd Orange Unified, CA (meetings 2024/2025/2026: 17/22/14,
the list ends 2026-07-16; BoardDocs minutes for all 53); nm/gmcs Gallup-McKinley County Schools, NM (17/19/18;
BD-GetMinutes empty for 38 of the 40 latest, minutes filed as PDFs on the next agenda); co/eagleco Eagle County, CO
(redirects to Diligent Community: a PortalError saying so); il/syc427 Sycamore CUSD 427, IL (list ends August 2024).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import date, timedelta
from html import unescape
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlsplit

from ..deep.web import Response
from .base import Document, Fetch, Item, Listing, Meeting, PortalError
from .browser import Challenge
from .registry import register

ORIGIN = "https://go.boarddocs.com"
_SITE = re.compile(r"(?:https?:)?//(?:[\w-]+\.)?boarddocs\.com/+([a-z]{2,4})/([\w.-]+)/board\.nsf", re.I)
_COMMITTEE = re.compile(r"""committeeid=["']([A-Z0-9]{8,16})["'][^>]*?(?:aria-label=["']([^"']*)["'])?[^>]*>([^<]*)""", re.I)
_SIZE = re.compile(r"\s*\((\d[\d,.]*)\s*(bytes?|KB|MB|GB)\)\s*$", re.I)
_NUMBER = re.compile(r"^\s*((?:\d{1,3}|[A-Za-z]|([A-Za-z])\2|[ivxlc]{1,6}|[IVXLC]{1,6})(?:\.(?:\d{1,3}|[A-Za-z]))*)[.)]\s+(.*\S)"
                     r"|^\s*(\d{1,3}(?:\.\d{1,3})+)\s+(.*\S)")  # "A. Title", "3.D) Title", "IV. Title", "1.1 Title"
_GOVERNING = re.compile(r"\bboard of (education|trustees|directors|(county )?commissioners|supervisors|selectmen|aldermen)\b|"
                        r"\bgoverning board\b|\bschool (board|committee)\b|\b(city|town|village) council\b|"
                        r"\bcouncil\b|\bboard of (school )?directors\b|\bboard meetings?\b|^board$", re.I)
_COMMITTEE_NAME = re.compile(r"\bcommittee\b|\bsub-?committee\b|\badvisory\b|\btask force\b|\bcommission\b|\bfoundation\b|"
                             r"\bcouncil of\b|\bstudent\b|\bpta\b|\bauthority\b|\bboard of (review|appeals|adjustment|zoning)\b", re.I)
FRESH_DAYS = 120  # a meeting newer than this is read fresh: its minutes may have been posted since
FOLLOWING, FOLLOW_DAYS = 2, 120  # later meetings of the same board searched for a meeting's minutes, attached to their agendas


def date_pattern(d: date) -> re.Pattern:
    """The ways a file's title or name writes a meeting's date: "September 17, 2026", "Sept. 17 2026", "9/17/26",
    "09-17-2026", "9.17.26", "2026-09-17"."""
    month, yy = d.strftime("%b"), f"{d.year % 100:02d}"
    return re.compile(rf"\b{month}[a-z]*\.?\s+0?{d.day}(?:st|nd|rd|th)?,?\s+{d.year}\b|"
                      rf"(?<!\d)0?{d.month}[-/._]0?{d.day}[-/._](?:20)?{yy}(?!\d)|"
                      rf"(?<!\d){d.year}[-/._]0?{d.month}[-/._]0?{d.day}(?!\d)", re.I)


def meeting_kind(name: str, committee: bool = False) -> str:
    """regular, special, work_session, committee or other, from the meeting's name as the portal prints it."""
    n = name or ""
    if re.search(r"\bwork(ing)?[ -]?sessions?\b|\bworkshops?\b|\bstudy sessions?\b|\bretreat\b|\bplanning session\b", n, re.I):
        return "work_session"
    if re.search(r"\bspecial\b|\bemergency\b", n, re.I):
        return "special"
    if committee or re.search(r"\bcommittee\b|\bsubcommittee\b", n, re.I):
        return "committee"
    if re.search(r"\bregular\b|\bbusiness meeting\b|\bboard meeting\b|\bcouncil meeting\b|\bmonthly\b", n, re.I):
        return "regular"
    return "other"


def governing_board(name: str, boards: int) -> bool | None:
    """Is this board the body's governing board? A committee or advisory body is not; a single board usually is."""
    if _COMMITTEE_NAME.search(name or "") and not re.search(r"\bboard of (county )?commissioners\b|\bschool committee\b", name or "", re.I):
        return False
    if _GOVERNING.search(name or ""):
        return True
    return True if boards == 1 else None


def client_from(url: str) -> str | None:
    m = _SITE.search(url or "")
    return f"{m.group(1).lower()}/{m.group(2).lower()}" if m else None


def site_url(client: str) -> str:
    state, _, site = client.strip("/").partition("/")
    if not state or not site or "/" in site:
        raise PortalError(f"not a BoardDocs client id: {client!r} (expected <state>/<site>, e.g. ca/orangeusd)")
    return f"{ORIGIN}/{state}/{site}/Board.nsf"


def parse_committees(html: str) -> list[tuple[str, str]]:
    """The portal page's boards, as (committee id, name), in the page's order."""
    out = {}
    for cid, label, text in _COMMITTEE.findall(html or ""):
        out.setdefault(cid, unescape(label or text).strip())
    return list(out.items())


def parse_meetings_list(body: bytes | str) -> list[tuple[str, date, str]]:
    """BD-GetMeetingsList JSON -> (unique, date, name) by date; empty entries and bad dates are skipped."""
    data = json.loads(body or b"[]")
    if not isinstance(data, list):
        raise ValueError("BD-GetMeetingsList: expected a JSON list")
    out = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        key, nd = str(entry.get("unique") or "").strip(), str(entry.get("numberdate") or "").strip()
        if not key or not re.fullmatch(r"\d{8}", nd):
            continue
        try:
            when = date(int(nd[:4]), int(nd[4:6]), int(nd[6:]))
        except ValueError:
            continue
        out.append((key, when, " ".join(str(entry.get("name") or "").split())))
    return sorted(out, key=lambda m: (m[1], m[0]))


def _text(parts: list[str]) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*\n+", "\n", "".join(parts).replace("\xa0", " "))).strip()


def _split_number(subject: str) -> tuple[str | None, str]:
    """("3.D", "Adoption of Agenda") from "3.D. Adoption of Agenda"; (None, subject) when it starts with no number."""
    m = _NUMBER.match(subject or "")
    if not m:
        return None, (subject or "").strip()
    number, title = (m.group(1), m.group(3)) if m.group(1) else (m.group(4), m.group(5))
    return (number, title.strip()) if re.search(r"[A-Za-z]", title) else (None, (subject or "").strip())


class _PrintAgenda(HTMLParser):
    """PRINT-AgendaDetailed in order: section headings, then each item's rows (Subject, Category, Type, Recommended
    Action), its text, its public files and its motions."""

    _BLOCKS = ("p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "dd", "dt")

    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.items: list[dict] = []
        self.section: str | None = None
        self.stack: list[str] = []  # the role of each open div: "item", "body", "files", "file", "motion", "heading", ""
        self.item: dict | None = None
        self.capture: str | None = None  # "heading", "dt", "dd", "body", "motion", "file"
        self.buf: list[str] = []
        self.label = ""
        self.href: str | None = None

    def _in(self, role: str) -> bool:
        return role in self.stack

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        classes = set(a.get("class", "").split())
        if tag == "div":
            role = ""
            if {"container", "item"} <= classes:
                role = "item"
                self.item = {"rows": {}, "text": "", "files": [], "motions": []}
            elif self.item is None and a.get("role") == "heading" and a.get("aria-level") == "1":
                role, self.capture, self.buf = "heading", "heading", []
            elif self.item is not None and "itembody" in classes:
                role, self.capture, self.buf = "body", "body", []
            elif self.item is not None and "print-files" in classes:
                role = "files"
            elif self.item is not None and "public-file" in classes:
                role = "file"
            elif self.item is not None and "motion" in classes and not self._in("motion"):
                role, self.capture, self.buf = "motion", "motion", []
            self.stack.append(role)
        elif self.item is not None and tag in ("dt", "dd") and self.capture is None:
            self.capture, self.buf = tag, []
        elif tag == "a" and self.item is not None and self._in("file") and a.get("href"):
            self.capture, self.buf, self.href = "file", [], a["href"]
        if self.capture in ("body", "motion") and tag in self._BLOCKS:
            self.buf.append("\n")

    def handle_startendtag(self, tag, attrs):
        if self.capture in ("body", "motion") and tag in ("br", "hr"):
            self.buf.append("\n")

    def handle_data(self, data):
        if self.capture:
            self.buf.append(data)

    def handle_endtag(self, tag):
        if tag in ("dt", "dd") and self.capture == tag and self.item is not None:
            text = " ".join("".join(self.buf).split())
            if tag == "dt":
                self.label = text
            elif self.label:
                self.item["rows"].setdefault(self.label, text)
                self.label = ""
            self.capture = None
        elif tag == "a" and self.capture == "file" and self.item is not None:
            text = " ".join("".join(self.buf).split())
            self.item["files"].append((urljoin(self.base, self.href), text))
            self.capture, self.href = None, None
        elif tag == "div" and self.stack:
            role = self.stack.pop()
            if role == "heading" and self.capture == "heading":
                self.section, self.capture = " ".join("".join(self.buf).split()) or None, None
            elif role == "body" and self.capture == "body" and self.item is not None:
                self.item["text"], self.capture = _text(self.buf), None
            elif role == "motion" and self.capture == "motion" and self.item is not None:
                self.item["motions"].append(_text(self.buf))
                self.capture = None
            elif role == "item" and self.item is not None:
                self.item["section"] = self.item["rows"].get("Category") or self.section
                self.items.append(self.item)
                self.item, self.capture = None, None
        if self.capture in ("body", "motion") and tag in self._BLOCKS:
            self.buf.append("\n")


def _file_document(url: str, text: str, meeting_id: str, item_no: str | None) -> Document:
    title = _SIZE.sub("", text or "").strip() or unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else None
    kind = "minutes" if re.search(r"\bminutes?\b", f"{title} {name}", re.I) else "item"
    return Document(url, kind, title, meeting_id=meeting_id, item_no=item_no, format=ext)


def parse_print_agenda(html: str, base: str, meeting_id: str) -> list[Item]:
    """PRINT-AgendaDetailed HTML -> items in agenda order, numbered as the agenda prints them ("3.D")."""
    parser = _PrintAgenda(base)
    parser.feed(html or "")
    parser.close()
    items = []
    for raw in parser.items:
        rows = raw["rows"]
        order, title = _split_number(rows.get("Subject", ""))
        section = raw.get("section")
        sec_no, _ = _split_number(section or "")
        number = order
        if order and sec_no and not order.startswith(f"{sec_no}."):
            number = f"{sec_no}.{order}"
        elif not order:
            number = sec_no
        text = raw["text"]
        if rows.get("Recommended Action"):
            text = (text + "\n\n" if text else "") + f"Recommended action: {rows['Recommended Action']}"
        outcome = "\n\n".join(m for m in raw["motions"] if m) or None
        items.append(Item(meeting_id, number, title or "(untitled)", section=section,
                          attachments=[_file_document(u, t, meeting_id, number) for u, t in raw["files"]],
                          outcome=outcome, text=text or None))
    return items


def minutes_text(html: str) -> str:
    """The minutes' words, without markup ("" when the board keeps no minutes in BoardDocs)."""
    return " ".join(unescape(re.sub(r"(?is)<(script|style)\b.*?</\1>|<[^>]+>", " ", html or "")).split())


def _moved(client: str, final_url: str | None) -> None:
    """A site that left BoardDocs sends visitors on (often to Diligent Community, BoardDocs' successor): said, not
    read as an empty BoardDocs site."""
    host = (urlsplit(final_url or "").hostname or "").lower()
    if host and not (host == "boarddocs.com" or host.endswith(".boarddocs.com")):
        raise PortalError(f"{client}: the BoardDocs site now sends visitors to {final_url[:160]} (moved off BoardDocs; "
                          "a Diligent Community address is read by the diligent adapter)")


@register
class BoardDocs:
    name, transport = "boarddocs", "http"

    def __init__(self, http: Fetch | None = None, browser=None) -> None:
        """`browser`: a ddwb.portals.browser.Browser to use once plain HTTP is refused; None makes one when needed
        (Playwright installed); False never uses one (a refusal is then a PortalError)."""
        self.http = http
        self.browser = browser
        self.walled_http: str | None = None  # why plain HTTP was given up, when it was
        self._pages: dict[str, str] = {}  # client -> its portal page, as opened in the browser
        self._listed: dict[str, list[Meeting]] = {}  # client -> every meeting its boards list, all years

    # ------------------------------------------------------------------------------------------- recognising a site
    def detect(self, url: str, html: str | None = None) -> str | None:
        found = client_from(url)
        if found or not html:
            return found
        clients = Counter(f"{m.group(1).lower()}/{m.group(2).lower()}" for m in _SITE.finditer(html))
        return clients.most_common(1)[0][0] if clients else None

    # ------------------------------------------------------------------------------------------- transport
    def _browser(self):
        if self.browser is False:
            raise PortalError(f"BoardDocs refused plain HTTP ({self.walled_http}) and the browser tier is switched off")
        if self.browser is None:
            from .browser import Browser
            self.browser = Browser()
        return self.browser

    def fetch_for(self, client: str) -> Fetch:
        """The transport that reads this site: plain HTTP until CloudFront refuses it, then the browser (with the
        site's page open, so documents can be fetched through it too)."""
        if self.http is not None and self.walled_http is None:
            return self.http
        self._open(client)
        return self.browser

    def _open(self, client: str) -> str:
        if client not in self._pages:
            visit = self._browser().open(f"{site_url(client)}/Public", ready="() => window.bd && bd.app_path")
            _moved(client, visit.final_url)
            self._pages[client] = visit.html
        return self._pages[client]

    def _call(self, client: str, method: str, url: str, data: dict | None = None, use_cache: bool = True) -> Response:
        """One request through the current transport; a wall on plain HTTP moves to the browser and asks again; a wall
        in the browser, or an error, is a PortalError."""
        for _ in range(2):
            fetch = self.fetch_for(client)
            r = fetch.get(url, use_cache=use_cache) if method == "GET" else fetch.post(url, data=data, use_cache=use_cache)
            if r.bot_wall and fetch is self.http:
                self.walled_http = f"HTTP {r.status}" if r.status else (r.error or "a bot wall")
                continue
            if r.bot_wall:
                raise Challenge(f"BoardDocs answered the browser with a block page ({r.error or f'HTTP {r.status}'}): "
                                "stopped there, not solved")
            if not r.ok:
                raise PortalError(f"{url.rsplit('/', 1)[-1][:80]}: {r.error or f'HTTP {r.status}'}")
            return r
        raise PortalError(f"{url}: no transport left")  # pragma: no cover

    def _portal_page(self, client: str) -> str:
        if self.http is not None and self.walled_http is None:
            r = self.http.get(f"{site_url(client)}/Public")
            if r.ok:
                _moved(client, r.final_url)
                return r.text
            if not r.bot_wall:
                raise PortalError(f"{client}: the portal page failed: {r.error or f'HTTP {r.status}'}")
            self.walled_http = f"HTTP {r.status}" if r.status else (r.error or "a bot wall")
        return self._open(client)

    # ------------------------------------------------------------------------------------------- the interface
    def meetings(self, client: str, since: date, until: date) -> Listing:
        base = site_url(client)
        html = self._portal_page(client)
        notes = []
        if self.walled_http:
            notes.append(f"plain HTTP refused ({self.walled_http}, CloudFront): read through the browser tier")
        boards = parse_committees(html)
        if not boards:
            notes.append("no boards (committees) on the portal page: read the list the site gives without one")
            boards = [("", "")]
        meetings: list[Meeting] = []
        listed_all: list[Meeting] = []
        for cid, board in boards:
            try:
                r = self._call(client, "POST", f"{base}/BD-GetMeetingsList?open", data={"current_committee_id": cid},
                               use_cache=False)
                listed = parse_meetings_list(r.body)
            except Challenge as wall:
                if not meetings:
                    raise
                notes.append(f"board {board or cid}: meetings not read, and the boards after it: {wall}")
                break
            except (PortalError, ValueError) as error:
                notes.append(f"board {board or cid or '(default)'}: meetings not read: {error}")
                continue
            governing = governing_board(board, len(boards))
            for key, when, name in listed:
                m = Meeting(key, when, name or f"Meeting {key}", board=board, kind=meeting_kind(name, committee=governing is False),
                            governing=governing, url=f"{base}/goto?open&id={key}")
                listed_all.append(m)
                if since <= when <= until:
                    meetings.append(m)
        self._listed[client] = sorted(listed_all, key=lambda m: (m.date, m.id))
        if not meetings and not notes:
            notes.append(f"no meetings between {since} and {until} on any of {len(boards)} board(s)")
        return Listing(sorted(meetings, key=lambda m: (m.date, m.id)), notes)

    def _fresh_ok(self, meeting: Meeting) -> bool:
        """Read from the cache only when the meeting is old enough that its agenda and minutes are settled."""
        return meeting.date is not None and meeting.date < date.today() - timedelta(days=FRESH_DAYS)

    def _agenda(self, client: str, meeting: Meeting) -> tuple[str, str]:
        url = f"{site_url(client)}/PRINT-AgendaDetailed?open&id={meeting.id}"
        return url, self._call(client, "GET", url, use_cache=self._fresh_ok(meeting)).text

    def documents(self, client: str, meeting: Meeting) -> list[Document]:
        base = site_url(client)
        agenda_url, html = self._agenda(client, meeting)
        items = parse_print_agenda(html, base + "/", meeting.id)
        docs = []
        if items:
            docs.append(Document(agenda_url, "agenda", f"Agenda: {meeting.title} (printable, with item details)",
                                 meeting_id=meeting.id, format="html"))
        minutes_url = f"{base}/BD-GetMinutes?open&id={meeting.id}"
        minutes = self._call(client, "GET", minutes_url, use_cache=self._fresh_ok(meeting)).text
        words = minutes_text(minutes)
        if len(words) >= 100:
            docs.append(Document(minutes_url, "minutes", f"Minutes: {words[:100]}", meeting_id=meeting.id, format="html"))
        elif meeting.date:
            docs += self._attached_later(client, meeting)
        seen = {d.url for d in docs}
        for item in items:
            for doc in item.attachments:
                if doc.url not in seen:
                    seen.add(doc.url)
                    # minutes filed on this agenda are an earlier meeting's, approved here: a file of this item
                    docs.append(Document(doc.url, "item", doc.title, doc.meeting_id, doc.item_no, doc.format))
        return docs

    def following(self, client: str, meeting: Meeting) -> list[Meeting]:
        """The next meetings of the same board (up to FOLLOWING, within FOLLOW_DAYS, already held): where a board
        that keeps its minutes as files approves them."""
        if client not in self._listed:
            self.meetings(client, meeting.date, meeting.date + timedelta(days=FOLLOW_DAYS))
        known = self._listed.get(client, [])
        board = next((m.board for m in known if m.id == meeting.id), meeting.board)
        last = min(meeting.date + timedelta(days=FOLLOW_DAYS), date.today())
        return [m for m in known if m.board == board and m.id != meeting.id and meeting.date < m.date <= last][:FOLLOWING]

    def _attached_later(self, client: str, meeting: Meeting) -> list[Document]:
        """This meeting's minutes, filed as a file on a later meeting's agenda (its "approval of minutes" item)."""
        pattern, base = date_pattern(meeting.date), site_url(client)
        for later in self.following(client, meeting):
            _, html = self._agenda(client, later)
            found = [Document(d.url, "minutes", f"{d.title} (filed with item {item.number} of the {later.date} meeting)",
                              meeting_id=meeting.id, format=d.format)
                     for item in parse_print_agenda(html, base + "/", later.id) for d in item.attachments
                     if d.kind == "minutes" and pattern.search(f"{d.title} {unquote(d.url)}")]
            if found:
                return found
        return []

    def items(self, client: str, meeting: Meeting) -> list[Item]:
        _, html = self._agenda(client, meeting)
        return parse_print_agenda(html, site_url(client) + "/", meeting.id)

    def close(self) -> None:
        if self.browser not in (None, False):
            self.browser.close()
