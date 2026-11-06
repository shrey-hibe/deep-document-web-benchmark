"""Legistar (<client>.legistar.com): the keyless Web API, cross-checked against the client's own web calendar.

Read:
  webapi.legistar.com/v1/<client>/bodies                 each board with its type ("Primary Legislative Body" governs)
  webapi.legistar.com/v1/<client>/events?$filter=&$top=&$skip=
                                                          meetings in the window, 1,000 a page at most, paged with $skip
                                                          until a short page; agenda and minutes files (EventAgendaFile,
                                                          EventMinutesFile), video (EventVideoPath)
  webapi.legistar.com/v1/<client>/events/<id>/eventitems?AgendaNote=1&MinutesNote=1&Attachments=1
                                                          agenda items with number, title, action, and their matters'
                                                          attachments (direct file links)
  <client>.legistar.com/Calendar.aspx                     the web calendar, one year at a time (a form post choosing the
                                                          year, then the grid's pager, 100 rows a page): agenda, packet,
                                                          minutes, accessible versions, video and extra columns as the
                                                          public sees them (View.ashx?M=A|PA|M|... is the file itself)
  <client>.legistar.com/MeetingDetail.aspx                one meeting's links and agenda grid, when the listing was not
                                                          read in this run or the API cannot give items
The web calendar is always read for the window's years: on real clients the API lists meetings the calendar does not and
the reverse, refuses some clients outright (Racine, WI: HTTP 400, a settings error), and leaves EventMinutesFile empty
where the calendar links the minutes (Guilford County, NC: 0 of 79 in the API, 56 on the calendar for 2025). The two are
joined by day and board; what only one side lists is kept and counted in the notes.

Cannot: items of a meeting the API does not know come from the MeetingDetail grid without their attachments (those are on
each item's LegislationDetail page, not read); time zones; the calendar's custom extra columns are kept by their link
text. Reads Legistar's web API and, where the API is closed, the web calendar. Checked on Hernando County, FL,
Guilford County, NC (counties) and Racine, WI (city), 2026-11-05.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from html import unescape
from pathlib import PurePosixPath
from urllib.parse import urlencode, urljoin

from . import civic_common as cc
from .base import Document, Fetch, Item, Listing, Meeting, PortalError
from .registry import register

API = "https://webapi.legistar.com/v1"
PAGE = 1000  # the API's largest $top
MAX_WEB_PAGES = 60  # 6,000 calendar rows in a year: a guard, never reached on real clients
_NOT_CLIENTS = {"www", "webapi", "legistar", "api", "docs", "help", "support", "granicus"}
_PATTERNS = (
    re.compile(r"\bwebapi\.legistar\.com/v1/([a-z0-9][a-z0-9-]*)", re.I),
    re.compile(r"\blegistar\d*\.granicus\.com/([a-z0-9][a-z0-9-]*)/", re.I),
    re.compile(r"(?<![\w.-])([a-z0-9][a-z0-9-]*)\.legistar\d?\.com\b", re.I),
)
_TITLE_LIMIT = 300

# Calendar and MeetingDetail links: the control's name -> (kind, label)
_WEB_LINKS = {
    "hypAgenda": ("agenda", "Agenda"),
    "hypAgendaPacket": ("packet", "Agenda packet"),
    "hypMinutes": ("minutes", "Minutes"),
    "hypMinutesPacket": ("minutes", "Minutes packet"),
    "hypAccessibleAgendaHTML": ("agenda", "Accessible agenda"),
    "hypAccessibleAgenda": ("agenda", "Accessible agenda"),
    "hypAccessibleMinutesHTML": ("minutes", "Accessible minutes"),
    "hypAccessibleMinutes": ("minutes", "Accessible minutes"),
    "hypVideo": ("video", "Video"),
}


def client_of(url: str) -> str | None:
    """The client id in a Legistar address: <client>.legistar.com, webapi.legistar.com/v1/<client>, or a file host
    (legistar1.granicus.com/<client>/..., <client>.legistar1.com)."""
    url = url or ""
    for pattern in _PATTERNS:
        m = pattern.search(url if "//" in url else f"https://{url}")
        if m and m.group(1).lower() not in _NOT_CLIENTS:
            return m.group(1).lower()
    return None


def _text(fragment) -> str:
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", str(fragment or ""))).replace("\xa0", " ").split())


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _suffix(name) -> str | None:
    suffix = PurePosixPath(str(name or "").split("?", 1)[0]).suffix.lower().lstrip(".")
    return suffix if suffix and len(suffix) <= 5 else None


def _clock(value: str) -> tuple[int, int]:
    m = re.search(r"(\d{1,2}):(\d{2})\s*([AP])M", value or "", re.I)
    if not m:
        return (99, 0)
    hour = int(m.group(1)) % 12 + (12 if m.group(3).upper() == "P" else 0)
    return (hour, int(m.group(2)))


def events_url(client: str, since: date, until: date, skip: int = 0) -> str:
    q = urlencode({"$filter": f"EventDate ge datetime'{since.isoformat()}' and EventDate lt datetime'{(until + timedelta(days=1)).isoformat()}'",
                   "$orderby": "EventDate asc, EventId asc", "$top": PAGE, "$skip": skip})
    return f"{API}/{client}/events?{q}"


# ------------------------------------------------------------------------------------------- the web pages
def hidden_fields(html: str) -> dict[str, str]:
    out = {}
    for m in re.finditer(r"<input\b[^>]*>", html, re.I):
        tag = m.group(0)
        if not re.search(r'type="hidden"', tag, re.I):
            continue
        name, value = re.search(r'name="([^"]+)"', tag), re.search(r'value="([^"]*)"', tag)
        if name:
            out[unescape(name.group(1))] = unescape(value.group(1)) if value else ""
    return out


def year_options(html: str) -> list[str]:
    i = html.find("lstYears_DropDown")
    return re.findall(r'<li class="rcbItem[^"]*">([^<]+)</li>', html[i:i + 20000]) if i >= 0 else []


def _link(row: str, name: str) -> tuple[str, str] | None:
    """(address, text) of the control `name` in a grid row or page, when it links anywhere."""
    m = re.search(r'<a\b([^>]*\bid="[^"]*_%s"[^>]*)>(.*?)</a>' % re.escape(name), row, re.S | re.I)
    if not m:
        return None
    attrs = m.group(1)
    href = re.search(r'\bhref="([^"#][^"]*)"', attrs)
    opened = re.search(r"window\.open\(&#39;([^&]+(?:&amp;[^&]+)*?)&#39;", attrs) or re.search(r"window\.open\('([^']+)'", attrs)
    target = href.group(1) if href and not href.group(1).startswith("javascript") else opened.group(1) if opened else None
    return (unescape(target), _text(m.group(2))) if target else None


def web_documents(fragment: str, base: str, meeting_id: str) -> list[Document]:
    """The documents a calendar row or a MeetingDetail page links: agenda, packet, minutes, accessible versions,
    video, and the client's extra columns (named by their link text)."""
    docs = []
    names = list(_WEB_LINKS) + sorted(set(re.findall(r'_(hypExtra\d+)"', fragment)))
    for name in names:
        found = _link(fragment, name)
        if not found:
            continue
        href, text = found
        kind, label = _WEB_LINKS.get(name) or (cc.document_kind(text), text or "Extra")
        pdf = re.search(r'_img%s"[^>]*src="[^"]*PDF\.gif"' % re.escape(name[3:]), fragment, re.I)
        docs.append(Document(url=urljoin(base, href), kind=kind, title=label, meeting_id=meeting_id,
                             format="pdf" if pdf else "html" if name.endswith("HTML") else None))
    return docs


def parse_calendar(html: str, base: str) -> tuple[list[dict], dict]:
    """A calendar page -> its grid's rows (body, day, time, web id, detail address, comment, documents) and the pager
    (page, pages, total, the postback target of the next page)."""
    rows = []
    for m in re.finditer(r'<tr class="rg(?:Alt)?Row"[^>]*id="ctl00_ContentPlaceHolder1_gridCalendar_ctl00__\d+"[^>]*>(.*?)</tr>',
                         html, re.S | re.I):
        row = m.group(1)
        body = _link(row, "hypBody")
        body_name = body[1] if body else _text((re.search(r'_hypBody"[^>]*>(.*?)</a>', row, re.S) or [None, ""])[1])
        day = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", _text(row))
        ident = re.search(r"(?:M=IC&(?:amp;)?|MeetingDetail\.aspx\?)ID=(\d+)&(?:amp;)?GUID=([0-9A-Fa-f-]+)", row)
        if not day or not ident:
            continue
        detail = _link(row, "hypMeetingDetail")
        time = _text((re.search(r'_lblTime"[^>]*>(.*?)</span>', row, re.S) or [None, ""])[1])
        comment = _text((re.search(r"<em>(.*?)</em>", row, re.S) or [None, ""])[1])
        rows.append({"id": ident.group(1), "guid": ident.group(2), "body": body_name,
                     "date": date(int(day.group(3)), int(day.group(1)), int(day.group(2))), "time": time,
                     "comment": comment,
                     "url": urljoin(base, detail[0]) if detail else
                     urljoin(base, f"MeetingDetail.aspx?ID={ident.group(1)}&GUID={ident.group(2)}&Options=info|&Search="),
                     "row": row})
    pager = {"page": 1, "pages": 1, "total": None, "next": None}
    for key, ident in (("page", "CPITop"), ("pages", "PCNTop"), ("total", "DSCTop")):
        m = re.search(r"gridCalendar_ctl00%s'?\"?>(\d+)<" % ident, html)
        if m:
            pager[key] = int(m.group(1))
    i = html.find("gridCalendar_ctl00NPPHTop")
    if i >= 0:
        links = re.findall(r'<a([^>]*)href="javascript:__doPostBack\(&#39;([^&]+)&#39;', html[i:html.find("</div>", i)])
        current = next((n for n, (attrs, _) in enumerate(links) if "rgCurrentPage" in attrs), None)
        if current is not None and current + 1 < len(links):
            pager["next"] = unescape(links[current + 1][1])
    return rows, pager


def detail_items(html: str, meeting_id: str) -> list[Item]:
    """The agenda grid of a MeetingDetail page: file, agenda number, type, title, action, result (no attachments)."""
    i = html.find("ctl00_ContentPlaceHolder1_gridMain")
    if i < 0:
        return []
    heads = [_text(h).lower() for h in re.findall(r'<th scope="col" class="rgHeader[^>]*>(.*?)</th>', html[i:i + 60000], re.S)]
    col = {name: heads.index(name) for name in ("file #", "agenda #", "type", "title", "action", "result") if name in heads}
    items = []
    for m in re.finditer(r'<tr class="rg(?:Alt)?Row"[^>]*ID="ctl00_ContentPlaceHolder1_gridMain_ctl00__\d+"[^>]*>(.*?)</tr>',
                         html, re.S | re.I):
        cells = [_text(c) for c in re.findall(r"<td\b[^>]*>(.*?)</td>", m.group(1), re.S)]
        def get(name: str, cells=cells) -> str | None:
            return (cells[col[name]] if name in col and col[name] < len(cells) else "").strip() or None
        title = get("title")
        if not title:
            continue
        outcome = " - ".join(x for x in (get("action"), get("result")) if x) or None
        matter = " ".join(x for x in (f"File {get('file #')}" if get("file #") else None,
                                      f"({get('type')})" if get("type") else None) if x) or None
        items.append(Item(meeting_id=meeting_id, number=get("agenda #"), title=title[:_TITLE_LIMIT], outcome=outcome,
                          text="\n".join(x for x in (matter, title if len(title) > _TITLE_LIMIT else None) if x) or None))
    return items


# ------------------------------------------------------------------------------------------- the API's records
def api_documents(event: dict) -> list[Document]:
    mid = str(event["EventId"])
    docs = []
    for field_name, kind, label in (("EventAgendaFile", "agenda", "Agenda"), ("EventMinutesFile", "minutes", "Minutes")):
        if event.get(field_name):
            docs.append(Document(url=event[field_name], kind=kind, title=label, meeting_id=mid, format=_suffix(event[field_name])))
    if event.get("EventVideoPath"):
        docs.append(Document(url=event["EventVideoPath"], kind="video", title="Video", meeting_id=mid))
    return docs


def api_items(records: list[dict], meeting_id: str) -> list[Item]:
    """Event items in printed order. Legistar has no sections: an item without a matter (a heading such as "CONSENT
    AGENDA") is the section of the matters that follow it."""
    ordered = sorted(records, key=lambda r: (r.get("EventItemAgendaSequence") is None, r.get("EventItemAgendaSequence") or 0,
                                             r.get("EventItemMinutesSequence") or 0, r.get("EventItemId") or 0))
    items, section = [], None
    for r in ordered:
        full = _text(r.get("EventItemTitle")) or _text(r.get("EventItemMatterName")) or "(untitled)"
        number = _text(r.get("EventItemAgendaNumber")) or None
        has_matter = r.get("EventItemMatterId") is not None
        attachments = [Document(url=a["MatterAttachmentHyperlink"], kind="item",
                                title=_text(a.get("MatterAttachmentName")) or "Attachment", meeting_id=meeting_id,
                                item_no=number, format=_suffix(a.get("MatterAttachmentFileName") or a["MatterAttachmentHyperlink"]))
                       for a in r.get("EventItemMatterAttachments") or [] if a.get("MatterAttachmentHyperlink")]
        outcome = " - ".join(x for x in (_text(r.get("EventItemActionName")), _text(r.get("EventItemPassedFlagName")),
                                         _text(r.get("EventItemTally"))) if x) or None
        matter = " ".join(x for x in (f"File {_text(r.get('EventItemMatterFile'))}" if r.get("EventItemMatterFile") else "",
                                      f"({_text(r.get('EventItemMatterType'))})" if r.get("EventItemMatterType") else "") if x)
        text = "\n".join(x for x in (matter, full if len(full) > _TITLE_LIMIT else "", _text(r.get("EventItemAgendaNote")),
                                     _text(r.get("EventItemMinutesNote")), _text(r.get("EventItemActionText"))) if x) or None
        items.append(Item(meeting_id=meeting_id, number=number,
                          title=full if len(full) <= _TITLE_LIMIT else full[:_TITLE_LIMIT - 1].rstrip() + "…",
                          section=section if has_matter else None, attachments=attachments, outcome=outcome, text=text))
        if not has_matter and not attachments:
            section = f"{number} {full}".strip() if number else full
    return items


@register
class Legistar:
    name, transport = "legistar", "http"

    def __init__(self, http: Fetch) -> None:
        self.http = http
        self._events: dict[tuple[str, str], dict] = {}  # (client, meeting id) -> the API event
        self._web: dict[tuple[str, str], list[Document]] = {}  # (client, meeting id) -> documents the calendar links

    # ------------------------------------------------------------------ detect
    def detect(self, url: str, html: str | None = None) -> str | None:
        client = client_of(url or "")
        if client or not html:
            return client
        found = Counter(m.group(1).lower() for p in _PATTERNS for m in p.finditer(html) if m.group(1).lower() not in _NOT_CLIENTS)
        return found.most_common(1)[0][0] if found else None

    # ------------------------------------------------------------------ the API
    def _api_json(self, url: str):
        resp = cc.fetch(self.http, url)
        if not resp.ok:
            return None, cc.refusal(resp), resp.bot_wall
        try:
            return json.loads(resp.body), None, False
        except ValueError:
            return None, f"not JSON ({cc.refusal(resp)})", False

    def _api_events(self, client: str, since: date, until: date) -> tuple[list[dict] | None, str | None]:
        rows: list[dict] = []
        while True:
            page, error, _ = self._api_json(events_url(client, since, until, len(rows)))
            if error or not isinstance(page, list):
                return (rows or None), f"API events from number {len(rows) + 1} on not read: {error or 'not a list'}"
            rows.extend(page)
            # A short page ends the list; a page of a round hundred may be a client's own cap, so ask once more.
            if not page or (len(page) < PAGE and len(page) % 100):
                return rows, None

    def _body_types(self, client: str) -> dict[int, str]:
        bodies, error, _ = self._api_json(f"{API}/{client}/bodies")
        if error or not isinstance(bodies, list):
            return {}
        return {b["BodyId"]: str(b.get("BodyTypeName") or "") for b in bodies if isinstance(b, dict) and "BodyId" in b}

    # ------------------------------------------------------------------ the web calendar
    def _calendar_year(self, client: str, year: int) -> tuple[list[dict], str | None]:
        """Every calendar row of one year: the form post that picks the year, then each page of the grid."""
        url = f"https://{client}.legistar.com/Calendar.aspx"
        first = cc.fetch(self.http, url)
        if not first.ok:
            return [], f"web calendar not read: {cc.refusal(first)}"
        if str(year) not in year_options(first.text):
            return [], f"web calendar offers no year {year}"
        choice = {"ctl00$ContentPlaceHolder1$lstYears": str(year),
                  "ctl00_ContentPlaceHolder1_lstYears_ClientState": json.dumps(
                      {"logEntries": [], "value": str(year), "text": str(year), "enabled": True, "checkedIndices": [],
                       "checkedItemsTextOverflows": False}),
                  "ctl00$ContentPlaceHolder1$lstBodies": "All Meeting Bodies"}
        data = hidden_fields(first.text) | choice | {"__EVENTTARGET": "", "__EVENTARGUMENT": "",
                                                       "ctl00$ContentPlaceHolder1$btnSearch": "Search Meeting Calendar"}
        resp = cc.fetch(self.http, url, data=data, post=True)
        if not resp.ok and first.from_cache and not resp.bot_wall:  # an old page's form may have expired: a fresh one
            first = cc.fetch(self.http, url, use_cache=False)
            data = hidden_fields(first.text) | choice | {"__EVENTTARGET": "", "__EVENTARGUMENT": "",
                                                           "ctl00$ContentPlaceHolder1$btnSearch": "Search Meeting Calendar"}
            resp = cc.fetch(self.http, url, data=data, post=True)
        rows: list[dict] = []
        pager: dict = {}
        for _ in range(MAX_WEB_PAGES):
            if not resp.ok:
                return rows, f"web calendar {year}: rows after {len(rows)} not read: {cc.refusal(resp)}"
            shown = re.search(r'lstYears_Input"[^>]*value="([^"]*)"', resp.text)
            if shown and shown.group(1) != str(year):
                return rows, f"web calendar {year}: the form showed {shown.group(1)!r} instead of the year"
            got, pager = parse_calendar(resp.text, url)
            rows.extend(got)
            if not pager.get("next") or pager.get("page", 1) >= pager.get("pages", 1):
                break
            data = hidden_fields(resp.text) | choice | {"__EVENTTARGET": pager["next"], "__EVENTARGUMENT": ""}
            resp = cc.fetch(self.http, url, data=data, post=True)
        total = pager.get("total")
        if total is not None and len({r["id"] for r in rows}) < total:
            return rows, f"web calendar {year}: read {len(rows)} of the {total} rows it counts"
        return rows, None

    # ------------------------------------------------------------------ meetings
    def meetings(self, client: str, since: date, until: date) -> Listing:
        notes: list[str] = []
        events, api_note = self._api_events(client, since, until)
        if api_note:
            notes.append(api_note)
        types = self._body_types(client) if events else {}
        web_rows: list[dict] = []
        web_years: set[int] = set()
        web_failed = []
        for year in range(since.year, until.year + 1):
            got, note = self._calendar_year(client, year)
            if note:
                notes.append(note)
                if not got:
                    web_failed.append(year)
            if got:
                web_years.add(year)
            web_rows.extend(r for r in got if since <= r["date"] <= until)
        if events is None and not web_rows and web_failed:
            raise PortalError(f"legistar {client}: neither the API nor the web calendar answered ({'; '.join(notes)})")

        # Join the two by day and board, in time order within a day.
        api_by_key: dict[tuple, list[dict]] = defaultdict(list)
        for e in events or []:
            api_by_key[(str(e.get("EventDate"))[:10], _norm(e.get("EventBodyName")))].append(e)
        web_by_key: dict[tuple, list[dict]] = defaultdict(list)
        for r in {r["id"]: r for r in web_rows}.values():
            web_by_key[(r["date"].isoformat(), _norm(r["body"]))].append(r)
        meetings, only_api, only_web = [], Counter(), Counter()
        for key in sorted(set(api_by_key) | set(web_by_key)):
            api_side = sorted(api_by_key.get(key, []), key=lambda e: (_clock(e.get("EventTime")), e["EventId"]))
            web_side = sorted(web_by_key.get(key, []), key=lambda r: (_clock(r["time"]), int(r["id"])))
            for n in range(max(len(api_side), len(web_side))):
                e = api_side[n] if n < len(api_side) else None
                r = web_side[n] if n < len(web_side) else None
                m = self._meeting(client, e, r, types)
                if m.date and since <= m.date <= until:
                    meetings.append(m)
                    if e is None:
                        only_web[m.date.year] += 1
                    elif r is None and m.date.year in web_years:
                        only_api[m.date.year] += 1
                        m.title += " (not on the public calendar)"  # seen: test meetings, removed pages
        if only_web and events is None:
            notes.append(f"all {sum(only_web.values())} meetings come from the web calendar (the API gave none)")
        elif only_web:
            notes.append("the web calendar lists meetings the API does not (kept): "
                         + ", ".join(f"{y}: {n}" for y, n in sorted(only_web.items())))
        if only_api:
            notes.append("the API lists meetings the web calendar does not (kept): "
                         + ", ".join(f"{y}: {n}" for y, n in sorted(only_api.items())))
        meetings.sort(key=lambda m: (m.date or date.min, m.id))
        return Listing(meetings, notes)

    def _meeting(self, client: str, event: dict | None, row: dict | None, types: dict[int, str]) -> Meeting:
        if event is not None:
            board = _text(event.get("EventBodyName"))
            mid = str(event["EventId"])
            day = datetime.fromisoformat(str(event["EventDate"])[:19]).date()
            comment = _text(event.get("EventComment"))
            url = event.get("EventInSiteURL") or (row or {}).get("url")
            self._events[(client, mid)] = event
            body_type = types.get(event.get("EventBodyId"), "")
        else:
            board, mid, day, comment, url, body_type = row["body"], f"web-{row['id']}", row["date"], "", row["url"], ""
        if row is not None:
            comment = comment or row["comment"]
            self._web[(client, mid)] = web_documents(row["row"], f"https://{client}.legistar.com/", mid)
        cancelled = bool(re.search(r"cancel", " ".join([comment, (row or {}).get("time", "")]), re.I))
        if "primary legislative" in body_type.lower():
            governing = True
        else:
            governing = cc.governing_of(board)
            if governing is None and body_type:
                governing = False  # a known body type that is not the primary legislative body
        short = comment if comment and len(comment) <= 60 and "http" not in comment.lower() else ""  # not a dial-in note
        title = board + (f": {short}" if short and short.lower() not in board.lower() else "")
        title += " (canceled)" if cancelled and "cancel" not in title.lower() else ""
        return Meeting(id=mid, date=day, title=title or "Meeting", board=board, kind=cc.kind_of(comment, board, governing=governing),
                       governing=governing, url=url)

    # ------------------------------------------------------------------ one meeting
    def _event(self, client: str, meeting: Meeting) -> dict | None:
        if not meeting.id.isdigit():
            return None
        event = self._events.get((client, meeting.id))
        if event is None:
            event, error, _ = self._api_json(f"{API}/{client}/events/{meeting.id}")
            if error or not isinstance(event, dict):
                return None
            self._events[(client, meeting.id)] = event
        return event

    def _detail(self, client: str, meeting: Meeting) -> tuple[str | None, str | None]:
        """The meeting's own page on the site: (html, None), or (None, why not)."""
        if not meeting.url or "legistar.com" not in meeting.url:
            return None, "the meeting has no page on the site"
        resp = cc.fetch(self.http, meeting.url)
        if not resp.ok:
            return None, f"its page {meeting.url} refused: {cc.refusal(resp)}"
        return resp.text, None

    def documents(self, client: str, meeting: Meeting) -> list[Document]:
        event = self._event(client, meeting)
        docs = api_documents(event) if event else []
        web, why = self._web.get((client, meeting.id)), None
        if web is None and not (event and event.get("EventAgendaFile") and event.get("EventMinutesFile")):
            page, why = self._detail(client, meeting)
            web = web_documents(page, meeting.url, meeting.id) if page else None
            if web is not None:
                self._web[(client, meeting.id)] = web
        have = {d.kind for d in docs}
        for d in web or []:
            if d.title in ("Agenda", "Minutes") and d.kind in have:
                continue  # the same file the API gave
            if all(d.url != x.url for x in docs):
                docs.append(d)
        if not docs and why:  # nothing from the API and the page could not be read: say so, not []
            raise PortalError(f"legistar {client}: no documents for meeting {meeting.id}: {why}")
        return docs

    def items(self, client: str, meeting: Meeting) -> list[Item]:
        if meeting.id.isdigit():
            url = f"{API}/{client}/events/{meeting.id}/eventitems?AgendaNote=1&MinutesNote=1&Attachments=1"
            records, error, wall = self._api_json(url)
            if not error and isinstance(records, list):
                return api_items(records, meeting.id)
            if wall:
                raise PortalError(f"legistar {client}: items of {meeting.id} refused: {error}")
        page, why = self._detail(client, meeting)
        if page is None:
            raise PortalError(f"legistar {client}: no items for meeting {meeting.id}: the API gave none and {why}")
        return detail_items(page, meeting.id)
