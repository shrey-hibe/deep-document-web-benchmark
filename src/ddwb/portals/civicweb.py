"""CivicWeb (<client>.civicweb.net, iCompass, now Diligent): the portal's own JSON service and its Document Center.
Plain GETs, no browser. The service is the one Diligent Community runs (same code base).

Endpoints and pages read
  /Services/MeetingsService.svc/meetings?from=1990-01-01&to=2100-12-31
                          every meeting the portal lists (Id, MeetingDate, Name, TypeId), all years in one answer,
                          so meetings() also says which dates the tenant covers: a city that moved to another portal
                          mid-period shows it as the last date listed
  /Portal/MeetingTypeList.aspx
                          the meeting types (TypeId -> "City Council Regular Meeting"): the board and kind
  /Services/MeetingsService.svc/meetings/{id}/meetingDocuments
                          the meeting's documents: Type 1 agenda family, Type 2 minutes; DocumentType says which (1 agenda
                          HTML, 4 agenda package PDF, 9 minutes HTML, 10 minutes PDF, 53 adopted minutes, and their
                          closed-session and split-screen variants); the open HTML agenda comes inline (items())
  /Services/MeetingsService.svc/meetings/{id}/meetingData
                          the meeting's external links (a video on another host, minutes kept elsewhere)
  /document/{id}          a document itself: a PDF, or the document as HTML (the portal renders Word minutes to HTML);
                          the agenda's attachments are /document/{id}/<name>.pdf?handle=... links
  /filepro/documents[/N]  the Document Center: folders and documents. When a past meeting has no minutes attached,
                          documents() walks from the root into the folders whose name says "minutes", then into the
                          subfolders named for the meeting's board and year, for a document whose title carries the
                          meeting's date: where many clerks file adopted minutes ("City Council Regular - 15 Dec 2025 -
                          Minutes - Adopted"); typically four folder pages per meeting, at most 25

What it cannot do
  - A tenant that has closed its portal answers with a sign-in page: PortalError. A tenant whose service fails
    (HTTP 400 on one seen) gives PortalError naming the status.
  - Documents behind a public-release delay are listed with the delay in their title; they may not open yet.
  - The Document Center match is by date (and board words when several share the date); minutes filed with no date in
    their title are not found. Folder pages are read as listed, without paging.
  - Agenda items come from the inline HTML agenda; a meeting with only a PDF agenda has no items here; item outcomes
    are not published in the agenda.

Checked on (2026-11-05/06): cityofwasilla (AK city: 2025 council meetings 34 of 35 with minutes attached; the
Document Center has no minutes for the 35th either; 36 items on 2025-12-15), nngov (VA city: 42 of 48, the other six
cancelled, a location notice, a vendor test entry and a budget hearing), solonohio (OH city: 24 of 29, the other five
cancelled or rescheduled; 40 items on 2025-12-15); the meetings service answered on 15 of 16 free-pool tenants tried
(newburynh no longer resolves; renton redirects to sign-in).
"""

from __future__ import annotations

import html as htmllib
import json
import re
from datetime import date, timedelta
from urllib.parse import urljoin, urlsplit

from .base import Document, Fetch, Item, Listing, Meeting, PortalError
from .meeting_words import board_from, clean, is_governing, meeting_kind, parse_date
from .registry import register

HOST = re.compile(r"^([a-z0-9][a-z0-9-]*)\.civicweb\.net$", re.I)
VENDOR = {"www", "i", "static", "assets", "cdn", "help", "support", "docs", "status", "api", "admin", "app", "login",
          "media", "learn", "demo"}
EMBED = re.compile(r"(?:https?:)?//([a-z0-9][a-z0-9-]*)\.civicweb\.net/(?:Portal|portal|filepro|document|Services|"
                   r"items|user)\b[^\s\"'<>]*", re.I)
ANY_LINK = re.compile(r"(?:https?:)?//([a-z0-9][a-z0-9-]*)\.civicweb\.net\b", re.I)
SERVICE = "/Services/MeetingsService.svc/meetings"
ALL_FROM, ALL_TO = "1990-01-01", "2100-12-31"
# DocumentType -> (kind, format); the split-screen copies (2, 6, 49, 51) repeat another document and are left out
DOC_TYPES = {1: ("agenda", "html"), 5: ("agenda", "html"), 4: ("packet", "pdf"), 8: ("packet", "pdf"),
             9: ("minutes", "html"), 11: ("minutes", "html"), 10: ("minutes", "pdf"), 12: ("minutes", "pdf"),
             53: ("minutes", None), 54: ("minutes", None)}
SPLIT_SCREEN = {2, 6, 49, 51}
CLOSED = {5, 6, 8, 11, 12, 51, 54}
ADOPTED = {53, 54}
CALLED_OFF = re.compile(r"cancel|postpon|reschedul|disregard|\btest\b", re.I)  # no minutes to look for
MAX_FOLDERS = 25  # Document Center folder pages read per meeting


def client_of(url: str) -> str | None:
    m = HOST.match((urlsplit(url or "").hostname or "").lower())
    return m.group(1) if m and m.group(1) not in VENDOR else None


def origin(client: str) -> str:
    return f"https://{client}.civicweb.net"


def meetings_url(client: str, since: str = ALL_FROM, until: str = ALL_TO) -> str:
    return f"{origin(client)}{SERVICE}?from={since}&to={until}"


def documents_url(client: str, meeting_id: str) -> str:
    return f"{origin(client)}{SERVICE}/{int(meeting_id)}/meetingDocuments"


def data_url(client: str, meeting_id: str) -> str:
    return f"{origin(client)}{SERVICE}/{int(meeting_id)}/meetingData"


def meeting_page(client: str, meeting_id: str) -> str:
    return f"{origin(client)}/Portal/MeetingInformation.aspx?Org=Cal&Id={int(meeting_id)}"


def _text(fragment: str) -> str:
    return clean(htmllib.unescape(re.sub(r"(?s)<[^>]+>", " ", fragment or "")))


def parse_types(page: str) -> dict[int, str]:
    """TypeId -> name, from MeetingTypeList.aspx."""
    out = {}
    for m in re.finditer(r"""(?is)<a\b[^>]*href=["'][^"']*MeetingInformation\.aspx\?type=(\d+)[^"']*["'][^>]*>(.*?)</a>""", page or ""):
        name = _text(m.group(2))
        if name:
            out.setdefault(int(m.group(1)), name)
    return out


def parse_folder(page: str, base: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(subfolders, documents) of a Document Center page, each as (absolute url, title)."""
    folders, docs = [], []
    for m in re.finditer(r"""(?is)<a\b[^>]*href=["']([^"']*/(filepro/documents/\d+|document/\d+)[^"']*)["'][^>]*>(.*?)</a>""", page or ""):
        url, title = urljoin(base, m.group(1)), _text(m.group(3))
        (folders if "/filepro/documents/" in url else docs).append((url.split("?")[0].rstrip("/"), title))
    return list(dict.fromkeys(folders)), list(dict.fromkeys(docs))


ANCHOR = re.compile(r"""(?is)<a\b[^>]*\bname=["']Agenda(Heading|Item)(\d+)["'][^>]*>""")
NUMBER = re.compile(r"^(?:[0-9]{1,3}|[A-Za-z]|[IVXivx]{1,5})(?:\.[0-9A-Za-z]{1,3})*\.?$")


def parse_agenda(html: str, base: str, meeting_id: str) -> list[Item]:
    """The CivicWeb HTML agenda: an anchor named AgendaHeadingN or AgendaItemN opens each entry; its first table holds
    the number, the title (first paragraph) and the attachment links (/document/...). Headings are entries of their own
    and the section of the items after them."""
    parts = ANCHOR.split(html or "")
    items, section = [], None
    for i in range(1, len(parts) - 2, 3):
        kind, segment = parts[i].lower(), parts[i + 2]
        table = re.search(r"(?is)<table\b.*?</table>", segment)
        cells = [c for c in re.findall(r"(?is)<td\b[^>]*>(.*?)</td>", table.group(0) if table else segment)]
        number, title, text = None, "", ""
        for cell in cells:
            t = _text(cell)
            if not t:
                continue
            if number is None and not title and NUMBER.match(t):
                number = t.rstrip(".")
                continue
            if not title:
                bare = re.sub(r"(?is)<a\b[^>]*href=[^>]*>.*?</a>", " ", cell)  # attachment links are not the item's text
                paragraphs = [p for p in (_text(p) for p in re.findall(r"(?is)<p\b[^>]*>(.*?)</p>", bare)) if p]
                title = paragraphs[0] if paragraphs else t
                text = " ".join(paragraphs[1:])
                break
        rest = segment[table.end():] if table else ""
        more = _text(re.sub(r"(?is)<a\b[^>]*href=[^>]*>.*?</a>", " ", rest))
        text = clean(f"{text} {more}")
        atts = []
        for m in re.finditer(r"""(?is)<a\b[^>]*href=["']([^"']*/document/\d+[^"']*)["'][^>]*>(.*?)</a>""", segment):
            url = urljoin(base, htmllib.unescape(m.group(1)))
            if not any(d.url == url for d in atts):
                atts.append(Document(url, "item", _text(m.group(2)), meeting_id, number,
                                     "pdf" if ".pdf" in urlsplit(url).path.lower() else None))
        if not (title or number):
            continue
        if kind == "heading":
            items.append(Item(meeting_id, number, title, None, atts, text=text or None))
            section = f"{number}. {title}" if number else title
        else:
            items.append(Item(meeting_id, number, title, section, atts, text=text or None))
    return items


@register
class CivicWeb:
    name, transport = "civicweb", "http"

    def __init__(self, http: Fetch, fresh: bool = True, document_center: bool = True) -> None:
        self.http, self.fresh, self.document_center = http, fresh, document_center
        self._types: dict[str, dict[int, str]] = {}
        self._folders: dict[tuple[str, str], tuple | None] = {}

    def detect(self, url: str, html: str | None = None) -> str | None:
        client = client_of(url)
        if client:
            return client
        for pattern in (EMBED, ANY_LINK):
            for m in pattern.finditer(html or ""):
                if m.group(1).lower() not in VENDOR:
                    return m.group(1).lower()
        return None

    def _json(self, client: str, url: str, fresh: bool = False):
        r = self.http.get(url, use_cache=not fresh)
        if r.bot_wall:
            raise PortalError(f"civicweb {client}: bot wall ({r.status or r.error})")
        if "/user/signin" in (r.final_url or "").lower():
            raise PortalError(f"civicweb {client}: the portal asks for sign-in (closed to the public, or moved)")
        if not r.ok or "json" not in (r.content_type or ""):
            raise PortalError(f"civicweb {client}: {urlsplit(url).path} not read: {r.error or f'HTTP {r.status}'}")
        try:
            return json.loads(r.body or b"null")
        except ValueError as error:
            raise PortalError(f"civicweb {client}: {urlsplit(url).path} answered something that is not JSON: {error}")

    def types(self, client: str) -> dict[int, str]:
        if client not in self._types:
            r = self.http.get(f"{origin(client)}/Portal/MeetingTypeList.aspx", use_cache=not self.fresh)
            if "/user/signin" in (r.final_url or "").lower():
                raise PortalError(f"civicweb {client}: the portal asks for sign-in (closed to the public, or moved)")
            self._types[client] = parse_types(r.body.decode("utf-8", errors="replace")) if r.ok else {}
        return self._types[client]

    def meetings(self, client: str, since: date, until: date) -> Listing:
        notes = []
        types = self.types(client)
        if not types:
            notes.append("meeting type names not read: boards come from meeting names")
        data = self._json(client, meetings_url(client), fresh=self.fresh)
        if not isinstance(data, list):
            raise PortalError(f"civicweb {client}: the meetings service answered an unexpected shape")
        dates = sorted(str(m.get("MeetingDate") or "")[:10] for m in data if m.get("MeetingDate"))
        if dates:
            notes.append(f"the portal lists {len(data)} meetings from {dates[0]} to {dates[-1]}")
            last = date.fromisoformat(dates[-1])
            if last < until and last < date.today() - timedelta(days=90):
                notes.append(f"no meeting listed after {last}: the body may have moved to another portal")
        else:
            notes.append("the portal lists no meetings")
        out = []
        for m in data:
            try:
                when = date.fromisoformat(str(m.get("MeetingDate"))[:10])
            except ValueError:
                continue
            if not since <= when <= until:
                continue
            type_name = types.get(int(m.get("TypeId") or 0), "")
            name = clean(m.get("Name") or type_name)
            board = board_from(type_name) or board_from(name)
            governing = is_governing(board)
            out.append(Meeting(str(m["Id"]), when, name, board, meeting_kind(type_name, name, governing=governing),
                               governing, meeting_page(client, m["Id"])))
        out.sort(key=lambda x: (x.date, x.id))
        return Listing(out, notes)

    # ------------------------------------------------------------------ documents
    def _docs(self, client: str, meeting: Meeting) -> list[dict]:
        data = self._json(client, documents_url(client, meeting.id))
        return data if isinstance(data, list) else (data or {}).get("Documents") or []

    def documents(self, client: str, meeting: Meeting) -> list[Document]:
        docs = []
        for d in self._docs(client, meeting):
            dtype = d.get("DocumentType")
            if dtype in SPLIT_SCREEN:
                continue
            kind, fmt = DOC_TYPES.get(dtype, ("agenda" if d.get("Type") == 1 else "minutes" if d.get("Type") == 2 else "other", None))
            title = clean(d.get("Name") or kind.title())
            title += " (adopted)" if dtype in ADOPTED else ""
            title += " (closed session)" if dtype in CLOSED else ""
            if d.get("PublicReleaseDelayDateString"):
                title += f" (public release: {d['PublicReleaseDelayDateString']})"
            docs.append(Document(f"{origin(client)}/document/{int(d['Id'])}", kind, title, meeting.id, None, fmt))
        try:
            extra = self._json(client, data_url(client, meeting.id))
        except PortalError:
            extra = {}
        for key, kind in (("MeetingExternalLinkUrl", "video"), ("MeetingExternalMinutesLinkUrl", "minutes")):
            url = extra.get(key) if isinstance(extra, dict) else None
            if url:
                label = extra.get(key.replace("Url", "Name")) or kind.title()
                k = "video" if re.search(r"video|youtube|swagit|vimeo|granicus|boxcast", f"{label} {url}", re.I) else kind
                docs.append(Document(url, k, label, meeting.id, None, None))
        if self.document_center and not any(d.kind == "minutes" for d in docs) and meeting.date and meeting.date < date.today() \
                and not CALLED_OFF.search(meeting.title):
            for title, url in self.center_minutes(client, meeting):
                docs.append(Document(url, "minutes", title, meeting.id, None, None))
        return docs

    def _folder(self, client: str, url: str):
        """(subfolders, documents) of a Document Center folder, read once per adapter; None when it cannot be read."""
        if (client, url) not in self._folders:
            r = self.http.get(url + "/")
            if r.bot_wall:
                raise PortalError(f"civicweb {client}: bot wall in the Document Center ({r.status or r.error})")
            self._folders[(client, url)] = parse_folder(r.body.decode("utf-8", errors="replace"), url + "/") if r.ok else None
        return self._folders[(client, url)]

    def center_minutes(self, client: str, meeting: Meeting) -> list[tuple[str, str]]:
        """Minutes filed in the Document Center for the meeting's date, as (title, /document/ url): from the root into the
        folders whose name says minutes, then depth first into the subfolders named for the meeting's board and year
        (a folder named for another year is skipped), at most MAX_FOLDERS folder pages per meeting. When several boards
        filed minutes that day, the ones whose title shares the board's words are kept."""
        words = set(re.findall(r"[a-z]{4,}", f"{meeting.board} {meeting.title}".lower())) - {"meeting", "minutes"}
        year, found, seen = meeting.date.year, [], set()
        stack = [(f"{origin(client)}/filepro/documents", False, 0)]
        while stack and len(seen) < MAX_FOLDERS:
            url, inside, depth = stack.pop()
            if url in seen:
                continue
            seen.add(url)
            page = self._folder(client, url)
            if page is None:
                continue
            folders, docs = page
            if inside:
                found += [(t, u) for u, t in docs if parse_date(t) == meeting.date and re.search(r"minute|summary|journal|proceeding", t, re.I)]
                if found:
                    break
            children = []
            for folder_url, title in folders:
                named_years = {int(y) for y in re.findall(r"\b((?:19|20)\d\d)\b", title)}
                is_minutes = bool(re.search(r"minute", title, re.I))
                if folder_url in seen or (named_years and year not in named_years) or depth >= 5 or not (inside or is_minutes):
                    continue
                score = 3 * (year in named_years) + 2 * len(words & set(re.findall(r"[a-z]{4,}", title.lower()))) + is_minutes
                children.append((score, folder_url))
            stack += [(u, True, depth + 1) for _, u in sorted(children)]  # the best-named folder is read next
        if len(found) > 1:
            keep = [f for f in found if words & set(re.findall(r"[a-z]{4,}", f[0].lower()))]
            found = keep or found
        return found

    # ------------------------------------------------------------------ items
    def items(self, client: str, meeting: Meeting) -> list[Item]:
        for d in self._docs(client, meeting):
            if d.get("DocumentType") in (1, 5) and isinstance(d.get("Html"), str) and d["Html"].strip():
                items = parse_agenda(d["Html"], origin(client) + "/", meeting.id)
                if items:
                    return items
                from ..deep import diligent  # the Diligent Community layout (h2 sections, h3 items), same service

                parsed, attachments = diligent.parse_agenda(d["Html"], origin(client))
                out = [Item(meeting.id, i.get("number"), i["title"], i.get("section"), []) for i in parsed]
                for a in attachments:
                    if a.get("item") and 0 < a["item"] <= len(out):
                        out[a["item"] - 1].attachments.append(Document(a["url"], "item", a["title"], meeting.id,
                                                                       out[a["item"] - 1].number))
                return out
        return []
