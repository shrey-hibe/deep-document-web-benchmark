"""Google Drive and Docs: small bodies that post agendas and minutes as files in public Drive folders, or as shared
Google Docs and Drive files linked from their own page. Plain GETs, no sign-in, no Drive API, no browser.

Client ids, two forms:
  "<folder id>[,<folder id>...]"   public Drive folders (detect() on a folder URL)
  "page:<url>"                     a body's own page that links or embeds meeting folders, Google Docs or Drive files
                                   (detect() with the page's HTML): meetings() reads the page (its link texts say what
                                   a folder holds: "2025 - 2026 Board Minutes" for a folder named "2025-2026"), then
                                   walks the folders it links

Pages and endpoints read:
  https://drive.google.com/embeddedfolderview?id=<folder>   a public folder as plain HTML: one "flip-entry" per child
                                   with its id, link (/file/d/<id> for files, /drive/folders/<id> for folders), title
                                   and type icon (.../type/application/pdf); the page title is the folder's name. A file
                                   id gives a page with the file's name and no list (so open?id= links, which may be
                                   either, are sorted out here)
  the body's page (page form)      links and iframes to Drive folders, Docs and Drive files, each link with its text,
                                   table row and heading (ddwb.deep.web.read_html)
Documents are given at the address that returns their content (ddwb.deep.web.direct_url): a Drive file's download, a
Doc's plain-text export, a Sheet's CSV.

Meetings: files are dated by their names (2025_07_16, 2026.10.19, "January 6 2025", 05272026, "agn-071426" beside a
meeting word), else by the folder they sit in when its name is dated ("2026-10-19 Council Meeting"); files of one date
are one meeting, split when their names say different meetings (a policy committee and a regular meeting on one day).
A file's kind comes from its name, else from its folder's ("2025 Council Meeting Minutes" makes "2025-01-06.pdf"
minutes). Folders are walked to depth 3, skipping subfolders named only for years or dates outside the window, at
most MAX_FOLDERS per call (a note says when the limit stopped the walk).

What it cannot do: files with no date in their name or folder are left out (counted in the notes); a private folder
asks for a Google sign-in and is reported, never signed into; the embedded view may list only the first few hundred
entries of a very large folder; items() is always [] (Drive has no agenda items); a Drive file over about 100 MB
answers its download with a virus-scan page instead of the file.

Reads the embedded folder view, dates from file names and the newest subfolders, and fetches files through Drive's own
download links. Checked on Richland GSD 88A IL (d88a.org's minutes folders by school year) and the
City of Vernonia OR (a council folder of minutes by year and one folder per meeting) on 2026-11-05.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from html import unescape
from urllib.parse import urlsplit

from ..deep import web
from ..deep.text import find_date
from .base import Document, Fetch, Item, Listing, Meeting, PortalError
from .registry import register

VIEW = "https://drive.google.com/embeddedfolderview?id={}"
_ID = r"[A-Za-z0-9_-]{10,}"
_FOLDER = re.compile(rf"^https?://drive\.google\.com/(?:drive/(?:u/\d+/)?folders/({_ID})|embeddedfolderview\?(?:.*&)?id=({_ID})"
                     rf"|open\?(?:.*&)?id=({_ID}))", re.I)
_FILE = re.compile(rf"^https?://(?:docs\.google\.com/(?:a/[^/]+/)?(?:document|spreadsheets|presentation)/d/{_ID}"
                   rf"|drive\.google\.com/(?:a/[^/]+/)?file/d/{_ID}|drive\.google\.com/uc\?(?:.*&)?id={_ID})", re.I)
_IFRAME = re.compile(r"<iframe[^>]+src=[\"']([^\"']*(?:drive|docs)\.google\.com[^\"']*)[\"']", re.I)
_MEETINGISH = re.compile(r"minute|agenda|meeting|board|council|commission|committee|trustee|packet|\bboe\b", re.I)
_BODY_WORD = re.compile(r"council|board|commission|committee|trustees|\bboe\b|authority|district", re.I)
_YEAR = re.compile(r"(?<!\d)(20[0-4]\d)(?!\d)")
_MEETING_WORD = re.compile(r"agn|agenda|min|minutes|mtg|meeting|packet|notice", re.I)
_MIME_FORMAT = {"application/pdf": "pdf", "application/vnd.google-apps.document": "txt",
                "application/vnd.google-apps.spreadsheet": "csv", "application/vnd.google-apps.presentation": "txt",
                "application/msword": "doc", "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
                "text/plain": "txt", "text/html": "html"}


def folder_id(url: str) -> str | None:
    """A folder id from a Drive folder link (/drive/folders/, /drive/u/0/folders/, embeddedfolderview?id=), or from an
    open?id= link, which may also be a file (meetings() tells them apart)."""
    m = _FOLDER.match(unescape(url or "").strip())
    return next((g for g in m.groups() if g), None) if m else None


def is_file_link(url: str) -> bool:
    return bool(_FILE.match(unescape(url or "").strip()))


def file_date(name: str) -> date | None:
    """A date in a file or folder name, including forms only file names use: underscores for spaces, WordPress's
    duplicate suffix ("..._20261.pdf"), digits run together (05272026 = MMDDYYYY, 20260527), and "2026-agn-071426"
    (MMDDYY, only beside a meeting word, so record numbers aren't read as dates)."""
    stem = re.sub(r"\.\w{2,4}$", "", (name or "").strip())
    stem = re.sub(r"(?<!\d)(20\d{2})\d$", r"\1", stem)
    for candidate in (stem, re.sub(r"[_+]+", " ", stem), re.sub(r"[_,+]+", " ", stem)):
        found = find_date(candidate)
        if found:
            return found
    if _MEETING_WORD.search(stem):
        for m in re.finditer(r"(?<!\d)(\d{2})(\d{2})(\d{2})(?!\d)", stem):
            try:
                return date(2000 + int(m.group(3)), int(m.group(1)), int(m.group(2)))
            except ValueError:
                continue
    return None


def doc_kind(text: str) -> str | None:
    """What a name says a document is; None when it says nothing or more than one thing ("Agendas and Minutes")."""
    t = (text or "").lower()
    found = [kind for kind, pattern in (("minutes", r"minute|\bmins\b"), ("packet", r"packet|board ?book|materials"),
                                        ("agenda", r"agenda|\bagn\b"), ("video", r"video|recording|audio|youtube"),
                                        ("notice", r"\bnotice\b")) if re.search(pattern, t)]
    if "packet" in found and "agenda" in found:  # "agenda packet"
        found.remove("agenda")
    return found[0] if len(found) == 1 else None


def board_name(label: str) -> str:
    """The body a folder's name names: "BOARD MINUTES: 2025 - 2026" -> "BOARD", "City Council Meetings" -> "City
    Council", "Planning Commission Agendas and Minutes" -> "Planning Commission"."""
    t = re.sub(r"(?<!\d)20\d\d(?!\d)|\b(?:minutes|agendas?|packets?|meetings?|and|current|present|documents?|files?|"
               r"archives?|the)\b", " ", label or "", flags=re.I)
    words = [w for w in re.split(r"[\s:|,&()\-\u2013\u2014/]+", t) if w]
    return " ".join(w for i, w in enumerate(words) if i == 0 or w.lower() != words[i - 1].lower())


def meeting_type(text: str) -> str | None:
    t = (text or "").lower()
    if re.search(r"\bcommittee\b|\bcommission\b", t) and "regular" not in t:
        return "committee"
    if re.search(r"\bspecial\b|\bemergency\b", t):
        return "special"
    if re.search(r"work ?session|workshop|study session|retreat|\bws\b", t):
        return "work_session"
    if "regular" in t:
        return "regular"
    return None


@dataclass
class Folder:
    id: str
    label: str  # the folder's name (or the link text that led to it)
    depth: int
    heading: str = ""  # on a body's page, the heading over a link that names nothing itself
    when: date | None = None  # a meeting folder's date
    kind: str | None = None  # what its name says its files are
    board: str = ""  # the nearest name that names a body
    mtype: str | None = None


@dataclass
class Entry:
    id: str
    title: str
    url: str
    mime: str
    is_folder: bool


@dataclass
class File:
    title: str
    url: str  # as linked (a viewer); the document's address is web.direct_url(url)
    mime: str = ""
    folder: Folder | None = None
    context: str = ""  # the link's row and heading, on a body's page
    extra: dict = field(default_factory=dict)


def parse_folder_view(html: str) -> tuple[str, list[Entry], bool]:
    """(the folder's name, its entries, whether the page is a folder list at all: a file id shows no list)."""
    title = re.search(r"<title>(.*?)</title>", html or "", re.S)
    name = " ".join(unescape(re.sub(r"<[^>]+>", " ", title.group(1))).split()) if title else ""
    entries = []
    for chunk in re.split(r'<div class="flip-entry" ', html or "")[1:]:
        ident = re.search(r'id="entry-([^"]+)"', chunk)
        href = re.search(r'<a href="([^"]+)"', chunk)
        label = re.search(r'<div class="flip-entry-title">(.*?)</div>', chunk, re.S)
        mime = re.search(r"/type/([^\"?]+)\"", chunk)
        if not (ident and href and label):
            continue
        url = unescape(href.group(1))
        entries.append(Entry(ident.group(1), " ".join(unescape(re.sub(r"<[^>]+>", " ", label.group(1))).split()), url,
                             unescape(mime.group(1)) if mime else "", "/drive/folders/" in url or "folder" in (mime.group(1) if mime else "")))
    return name, entries, ("flip-list-header" in (html or "") or bool(entries))


def page_links(html: str, url: str) -> tuple[list[tuple[str, tuple[str, str]]], list[File]]:
    """A body's page -> (meeting folders as (id, (link text, heading)), meeting files): Drive folder links whose text is
    about meetings (or is a school year under a heading that is), Docs and Drive file links whose text or row is about
    meetings or carries a date, and every Drive iframe."""
    page = web.read_html(html or "", url)
    folders: dict[str, tuple[str, str]] = {}
    files: dict[str, File] = {}
    for src in _IFRAME.findall(html or ""):
        fid = folder_id(src)
        if fid:
            folders.setdefault(fid, ("", ""))
    for link in page.links:
        fid = folder_id(link.href)
        # a link that says only "2025 - 2026" takes what it holds from the heading above it ("BOARD MINUTES"); a link
        # that says something else keeps to its own words (on some pages the heading is the previous link's text)
        years_only = bool(_YEAR.search(link.text)) and not re.sub(r"[\d\s\-\u2013/]+", "", link.text)
        if fid and (_MEETINGISH.search(link.text) or (years_only and _MEETINGISH.search(link.heading))):
            folders.setdefault(fid, (link.text.strip(), link.heading if years_only else ""))
        elif is_file_link(link.href) and (_MEETINGISH.search(f"{link.text} {link.row}") or file_date(link.text)
                                          or file_date(link.row)):
            files.setdefault(link.href, File(link.text or link.row, link.href, context=f"{link.row} {link.heading}".strip()))
    return list(folders.items()), list(files.values())


@register
class GoogleDrive:
    name, transport = "gdrive", "http"
    MAX_FOLDERS = 150
    MAX_DEPTH = 3

    def __init__(self, http: Fetch) -> None:
        self.http = http
        self._docs: dict[tuple[str, str], list[Document]] = {}

    # ------------------------------------------------------------------ detect
    def detect(self, url: str, html: str | None = None) -> str | None:
        fid = folder_id(url)
        if fid:
            return fid
        if not html:
            return None
        folders, files = page_links(html, url)
        # the page form keeps what the page says of each folder ("2025 - 2026 Board Minutes" for a folder named
        # "2025-2026"), which names the board and the kind of its files
        return f"page:{url}" if folders or files else None

    # ------------------------------------------------------------------ walking
    def _get(self, url: str):
        r = self.http.get(url)
        if r.bot_wall:
            raise PortalError(f"Google stopped us at a bot wall or block ({url}, HTTP {r.status}): never solved")
        return r

    def _walk(self, starts: list[tuple[str, tuple[str, str]]], since: date, until: date, notes: list[str]) -> list[File]:
        # a body's page may link a folder per year: those named only for years outside the window are not read
        queue = [Folder(fid, text, 0, heading=heading) for fid, (text, heading) in starts
                 if not (text and self._outside(text, since, until))]
        seen: set[str] = set()
        files: list[File] = []
        while queue:
            folder = queue.pop(0)
            if folder.id in seen:
                continue
            if len(seen) >= self.MAX_FOLDERS:
                notes.append(f"stopped after {self.MAX_FOLDERS} folders; {len(queue) + 1} more not read")
                break
            seen.add(folder.id)
            r = self._get(VIEW.format(folder.id))
            host = (urlsplit(r.final_url or "").hostname or "").lower()
            if host.startswith("accounts.google."):
                notes.append(f"folder {folder.id} ({folder.label or 'unnamed'}) is private: it asks for a Google sign-in")
                continue
            if not r.ok:
                notes.append(f"folder {folder.id} ({folder.label or 'unnamed'}): HTTP {r.status} {r.error or ''}".strip())
                continue
            name, entries, is_list = parse_folder_view(r.text)
            if not is_list:  # an open?id= link to a file, not a folder
                files.append(File(name or folder.label, f"https://drive.google.com/file/d/{folder.id}/view", folder=folder))
                continue
            names = [n for n in dict.fromkeys((name, folder.label, folder.heading)) if n]
            said = " ".join(names)  # the link text or heading may say more than the folder's name ("2025-2026")
            folder.label = name or folder.label
            folder.when = folder.when or file_date(name)
            folder.kind = doc_kind(said) or folder.kind
            folder.mtype = meeting_type(said) or folder.mtype
            if not folder.board:  # the outermost name of a body wins ("City Council"), the folder's own name first
                folder.board = next((board_name(n) for n in names if _BODY_WORD.search(n)), "")
            for entry in entries:
                if entry.is_folder:
                    if folder.depth + 1 > self.MAX_DEPTH or self._outside(entry.title, since, until):
                        continue
                    queue.append(Folder(entry.id, entry.title, folder.depth + 1, kind=folder.kind, board=folder.board,
                                        mtype=folder.mtype))
                else:
                    files.append(File(entry.title, entry.url, entry.mime, folder))
        return files

    @staticmethod
    def _outside(name: str, since: date, until: date) -> bool:
        """A subfolder named only for dates or years outside the window ("2019 - Council Meeting Packets")."""
        when = file_date(name)
        if when:
            return not since <= when <= until
        years = [int(y) for y in _YEAR.findall(name)]
        if not years or re.search(r"current|present", name, re.I):
            return False
        return max(years) < since.year or min(years) > until.year

    # ------------------------------------------------------------------ meetings
    def meetings(self, client: str, since: date, until: date) -> Listing:
        notes: list[str] = []
        loose: list[File] = []
        if client.startswith("page:"):
            url = client.removeprefix("page:")
            r = self._get(url)
            if not r.ok:
                raise PortalError(f"the body's page {url}: HTTP {r.status} {r.error or ''}".strip())
            starts, loose = page_links(r.text, r.final_url or url)
        else:
            starts = [(f.strip(), ("", "")) for f in client.split(",") if f.strip()]
        if not starts and not loose:
            raise PortalError(f"no Google Drive folders or files for {client}")
        files = loose + self._walk(starts, since, until, notes)
        groups: dict[tuple[date, str, str], list[File]] = defaultdict(list)
        undated = 0
        for f in files:
            folder = f.folder
            when = file_date(f.title) or file_date(f.context) or (folder.when if folder else None)
            if when is None:
                undated += 1
                continue
            if not since <= when <= until:
                continue
            mtype = meeting_type(f.title) or (folder.mtype if folder else None) or ""
            groups[(when, folder.board if folder else "", mtype)].append(f)
        if undated:
            notes.append(f"{undated} files with no date in their name or folder were left out")
        # files that name no meeting type join the one typed meeting of their date, if there is just one
        for (when, board, mtype) in [k for k in groups if not k[2]]:
            typed = [k for k in groups if k[0] == when and k[1] == board and k[2]]
            if len(typed) == 1:
                groups[typed[0]].extend(groups.pop((when, board, mtype)))
        meetings = []
        for (when, board, mtype), group in sorted(groups.items()):
            mid = when.isoformat() + (f":{re.sub(r'[^a-z0-9]+', '-', board.lower()).strip('-')}" if board else "") \
                + (f":{mtype}" if mtype else "")
            named = next((f.title for f in group if doc_kind(f.title) in ("minutes", "agenda")), group[0].title)
            folder = next((f.folder for f in group if f.folder), None)
            governing = None if not board else not re.search(r"committee|commission", board, re.I)
            if mtype == "committee":
                governing = False
            meetings.append(Meeting(mid, when, re.sub(r"\.\w{2,4}$", "", named).strip(), board=board,
                                    kind=mtype or "other", governing=governing,
                                    url=f"https://drive.google.com/drive/folders/{folder.id}" if folder else client.removeprefix("page:")))
            self._docs[(client, mid)] = [self._document(f, mid) for f in group]
        return Listing(meetings, notes)

    @staticmethod
    def _document(f: File, meeting_id: str) -> Document:
        kind = doc_kind(f.title) or doc_kind(f.context) or (f.folder.kind if f.folder else None) or "other"
        url = web.direct_url(f.url)
        fmt = _MIME_FORMAT.get(f.mime) or ("txt" if "/document/d/" in f.url else "csv" if "/spreadsheets/d/" in f.url else None)
        if fmt is None:
            ext = re.search(r"\.(pdf|docx?|txt|html?)\s*$", f.title, re.I)
            fmt = ext.group(1).lower() if ext else None
        return Document(url, kind, f.title, meeting_id, format=fmt)

    def documents(self, client: str, meeting: Meeting) -> list[Document]:
        if (client, meeting.id) not in self._docs and meeting.date:
            self.meetings(client, meeting.date, meeting.date)
        docs = self._docs.get((client, meeting.id))
        if docs is None:
            raise PortalError(f"no files dated {meeting.date} for meeting {meeting.id} in {client}")
        return docs

    def items(self, client: str, meeting: Meeting) -> list[Item]:
        return []

