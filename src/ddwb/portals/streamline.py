"""Streamline websites (<name>.specialdistrict.org, and custom domains on the same platform): the site's own pages.

A Streamline site is a small CMS for special districts. Each meeting is a page (/2026-10-28-board-meeting) with a date
and attachments in labelled slots (Agenda, Minutes) plus any other files; meetings appear on a board page
("/board-meetings") that lists them by year (?year=2026 tabs), attachments included. There is no JSON API.

Reads:
  /sitemap.xml                   every page; meeting pages have date-first slugs (YYYY-MM-DD-...), which finds them
  /<meeting>                     a meeting page: "This item appears on" names its board page(s); the attachments
                                 (ul.attachments: span.title "Agenda", "Minutes", ...) and any embedded video
  /<board>?year=Y                a board page's year tab: article.poc-type-meeting rows with date, title, attachments
  /                              only when there is no sitemap: links that look like meeting pages
Files are /files/<hash>/<name>.pdf on the same host (the file itself).

Minutes: many districts post agendas and notices but no minutes; meetings() says the share per tenant (a note), and
documents() says, in its notes, when a meeting has only an agenda. Some districts put the previous meeting's draft minutes
in a meeting's "Minutes" slot (for approval): when the file names another date, the document keeps kind "minutes" but
its meeting_id is the meeting it records (when listed) or None, its title says so, and documents() of the recorded
meeting picks it up from the later meeting's row in the board listing.

Cannot: items (Streamline publishes no agenda items, only files): items() is []. A meeting page that sits on no board
page and is outside the sitemap is not found; one in the sitemap but on no board page is dated and titled from its
address (a note says how many). Boards are named from their board page's title, or from the meeting's title when the
page's title names no board ("Meetings / Agendas"); governing stays None when neither names one. Sites list some
events as meetings (a station re-dedication): they come through as the site lists them.
Checked (2026-11-05) on Tubac Fire District AZ (tubacfire), Calaveras County Water District CA (calaverascwd),
Nederland Fire Protection District CO (nfpd), Humboldt Bay Municipal Water District CA (hbmwd); minutes share also
read on englewood-fire FL, apldilus IL, coosswcd OR, tcesd4 TX, kcfd3 WA, deltonlib MI.
"""

from __future__ import annotations

import re
from datetime import date
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, unquote_plus

from . import _tree
from ._kinds import Documents, board_name, dates_named, document_kind, governing, meeting_kind
from .base import Document, Fetch, Item, Listing, Meeting, PortalError
from .registry import register

HOST = re.compile(r"^([a-z0-9][a-z0-9-]*)\.specialdistrict\.org$", re.I)
IN_PAGE = re.compile(r"https?://([a-z0-9][a-z0-9-]*)\.specialdistrict\.org", re.I)
VENDOR = {"www", "app", "help", "support", "docs", "status", "api", "login", "cdn", "static", "assets", "media", "streamline"}
MARKERS = re.compile(r"getstreamline\.com|streamline\.imgix\.net|/assets/traction/", re.I)
SLUG = re.compile(r"^/(\d{4})-(\d{2})-(\d{2})-[\w-]+$")
MEETINGISH = re.compile(r"meeting|agenda|board|session|hearing|minutes|committee|council|workshop|cancel", re.I)
DETAIL_CAP = 15  # meeting pages read to find board pages
PAGE_CAP = 30  # extra ?page= pages per board year
_MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1)}


def host_of(client: str) -> str:
    return client.lower() if "." in client else f"{client.lower()}.specialdistrict.org"


def base_url(client: str) -> str:
    return f"https://{host_of(client)}"


def _fmt(url: str) -> str | None:
    ext = re.search(r"\.([a-z0-9]{2,5})$", urlsplit(url).path.lower())
    return ext.group(1) if ext else None


def _tile_date(node: _tree.Node) -> date | None:
    t = node.find("time", attr="datetime")
    if t is not None:
        try:
            return date.fromisoformat(t.attrs["datetime"][:10])
        except ValueError:
            pass
    month, day, year = (node.find("span", cls=c) for c in ("month", "day", "year"))
    if month and day and year:
        try:
            return date(int(year.text()), _MONTHS[month.text()[:3].lower()], int(day.text()))
        except (KeyError, ValueError):
            return None
    return None


def _slug_date(path: str) -> date | None:
    m = SLUG.match(path)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def attachments(node: _tree.Node, base: str) -> list[tuple[str, str]]:
    """(label, absolute url) of the files in an article's attachments block(s), as the site labels them."""
    out = []
    for block in node.find_all(cls="attachments"):
        for a in block.find_all("a", attr="href"):
            label = (a.find(cls="title") or a).text()
            out.append((label, urljoin(base + "/", a.attrs["href"])))
    return out


def read_listing(html: str, base: str) -> tuple[str, list[dict], list[str], str | None, list[str]]:
    """A board page: (its title, its meeting rows, the year tabs' years, the active year, further ?page= links)."""
    root = _tree.parse(html)
    full = root.find("article", cls="full-poc")
    head = full.find(pred=lambda n: n.tag in ("h1", "h2")) if full is not None else None
    title = head.text() if head is not None else (root.find("title").text() if root.find("title") else "")
    rows = []
    for art in root.find_all("article", pred=lambda n: "poc-instance" in n.classes and "poc-type-meeting" in n.classes):
        link = art.find("a", attr="href")
        if link is None:
            continue
        path = urlsplit(urljoin(base + "/", link.attrs["href"])).path
        h3 = art.find("h3")
        rows.append({"path": path, "date": _tile_date(art) or _slug_date(path), "title": h3.text() if h3 else "",
                     "files": attachments(art, base)})
    tabs = root.find(cls="instances-tabs")
    years, active = [], None
    if tabs is not None:
        for a in tabs.find_all("a", attr="href"):
            y = parse_qs(urlsplit(a.attrs["href"]).query).get("year", [None])[0]
            if y and y.isdigit():
                years.append(y)
                if a.parent is not None and "active" in a.parent.classes:
                    active = y
    pages = []
    section = root.find(cls="poc-instances") or root
    for a in section.find_all("a", attr="href"):
        if "page" in parse_qs(urlsplit(a.attrs["href"]).query):
            pages.append(urljoin(base + "/", a.attrs["href"]))
    return title, rows, years, active or (years[0] if years else None), pages


def classify(label: str, url: str) -> str:
    """The slot label first (Agenda, Minutes), then the label with the file name."""
    if label.strip().lower() in ("agenda", "minutes"):
        return label.strip().lower()
    return document_kind(f"{label} {unquote_plus(urlsplit(url).path.rsplit('/', 1)[-1])}")


def _minutes_dates(row: dict) -> tuple[bool, set[date]]:
    """(the row has minutes of its own meeting, the other dates its minutes files name)."""
    own, others = False, set()
    for label, url in row["files"] or []:
        if classify(label, url) != "minutes":
            continue
        named = dates_named(f"{label} {unquote_plus(urlsplit(url).path)}")
        if not named or row["date"] in named:
            own = True
        else:
            others |= named
    return own, others


def minutes_share(rows: list[dict]) -> str:
    """How many listed meetings have minutes anywhere in the listing: on their own row, or on a later row whose
    minutes file names their date."""
    marks = {id(r): _minutes_dates(r) for r in rows}
    named_elsewhere = set().union(*(o for _, o in marks.values())) if marks else set()
    own = [r for r in rows if marks[id(r)][0]]
    later = [r for r in rows if not marks[id(r)][0] and r["date"] in named_elsewhere]
    with_files = [r for r in rows if r["files"] and r not in own and r not in later]
    bare = [r for r in rows if not r["files"] and r not in later]
    n = len(own) + len(later)
    return (f"minutes found for {n} of {len(rows)} listed meetings ({100 * n // len(rows)}%), {len(later)} of them posted "
            f"with a later meeting; {len(with_files)} with only an agenda, notice or other files; {len(bare)} with no files")


@register
class Streamline:
    name = "streamline"
    transport = "http"

    def __init__(self, http: Fetch) -> None:
        self.http = http
        self._rows: dict[tuple[str, str], dict] = {}  # (client, meeting id) -> its listing row, board path and title

    # ------------------------------------------------------------------ detect
    def detect(self, url: str, html: str | None = None) -> str | None:
        host = (urlsplit(url or "").hostname or "").lower()
        m = HOST.match(host)
        if m and m.group(1) not in VENDOR:
            return m.group(1)
        if html:
            for m in IN_PAGE.finditer(html):
                if m.group(1).lower() not in VENDOR:
                    return m.group(1).lower()  # e.g. the "Sign in" link on a custom domain
            if MARKERS.search(html) and "streamline" in html.lower() and host:
                return host  # a custom domain on the platform: the host is the client
        return None

    # ------------------------------------------------------------------ fetching
    def _get(self, url: str, what: str):
        resp = self.http.get(url)
        if resp.bot_wall:
            raise PortalError(f"{what}: bot wall ({resp.error or f'HTTP {resp.status}'})")
        return resp

    def _sitemap(self, client: str, notes: list[str]) -> dict[str, date]:
        resp = self._get(base_url(client) + "/sitemap.xml", "sitemap")
        if not resp.ok or "<loc>" not in resp.text:
            notes.append(f"no sitemap ({resp.error or f'HTTP {resp.status}'}): board pages found from the home page")
            return {}
        found = {}
        for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", resp.text):
            path = urlsplit(loc).path
            d = _slug_date(path)
            if d:
                found[path] = d
        return found

    def _home_boards(self, client: str) -> list[str]:
        resp = self._get(base_url(client) + "/", "home page")
        if not resp.ok:
            raise PortalError(f"home page: {resp.error or f'HTTP {resp.status}'}")
        root, out = _tree.parse(resp.text), []
        for a in root.find_all("a", attr="href"):
            u = urlsplit(urljoin(base_url(client) + "/", a.attrs["href"]))
            if (u.hostname or "").lower() in (host_of(client), "www." + host_of(client)) and u.path not in ("", "/") \
                    and re.search(r"meeting|agenda|minutes|board", f"{a.text()} {u.path}", re.I) and u.path not in out:
                out.append(u.path)
        return out[:8]

    def _board(self, client: str, path: str, since: date, until: date, notes: list[str]) -> tuple[str, list[dict]]:
        """A board page's title and its meeting rows in the window, every year tab and ?page= page read."""
        base = base_url(client)
        resp = self._get(base + path, f"board page {path}")
        if not resp.ok:
            notes.append(f"board page {path}: {resp.error or f'HTTP {resp.status}'}")
            return "", []
        title, rows, years, active, pages = read_listing(resp.text, base)
        wanted = [y for y in years if since.year <= int(y) <= until.year]
        out = [] if years else list(rows)
        for y in wanted:
            if y == active:
                year_rows, more = rows, pages
            else:
                r = self._get(f"{base}{path}?{urlencode({'year': y})}", f"board page {path} {y}")
                if not r.ok:
                    notes.append(f"board page {path} year {y}: {r.error or f'HTTP {r.status}'}")
                    continue
                _, year_rows, _, _, more = read_listing(r.text, base)
            out.extend(year_rows)
            out.extend(self._pages(more, base, path, notes))
        if not years:
            out.extend(self._pages(pages, base, path, notes))
        return title, out

    def _pages(self, pages: list[str], base: str, path: str, notes: list[str]) -> list[dict]:
        out, seen, queue = [], set(), list(pages)
        while queue and len(seen) < PAGE_CAP:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            r = self._get(url, f"board page {path}")
            if not r.ok:
                notes.append(f"{url}: {r.error or f'HTTP {r.status}'}")
                continue
            _, rows, _, _, more = read_listing(r.text, base)
            out.extend(rows)
            queue.extend(u for u in more if u not in seen)
        return out

    def _appears_on(self, client: str, path: str) -> tuple[list[str], _tree.Node | None]:
        resp = self._get(base_url(client) + path, f"meeting page {path}")
        if not resp.ok:
            return [], None
        root = _tree.parse(resp.text)
        nav = root.find("nav", cls="appears-on")
        links = [urlsplit(urljoin(base_url(client) + "/", a.attrs["href"])).path for a in nav.find_all("a", attr="href")] if nav else []
        return links, root

    # ------------------------------------------------------------------ meetings
    def meetings(self, client: str, since: date, until: date) -> Listing:
        notes: list[str] = []
        sitemap = self._sitemap(client, notes)
        in_window = sorted((p for p, d in sitemap.items() if since <= d <= until and MEETINGISH.search(p)),
                           key=lambda p: sitemap[p], reverse=True)  # date-first pages that read like meetings, not events
        found: dict[str, dict] = {}
        boards: dict[str, str] = {}

        def add_board(path: str) -> None:
            if path in boards:
                return
            title, rows = self._board(client, path, since, until, notes)
            boards[path] = title
            for row in rows:
                if not (row["date"] and since <= row["date"] <= until):
                    continue
                known = found.get(row["path"])
                # a meeting on two pages (its board's and a series page like "Regular Meeting, 7PM"): the board's wins
                if known is None or (governing(known["board"]) is None and governing(title) is not None):
                    found[row["path"]] = row | {"board": title, "board_path": path}

        if sitemap:
            reads = 0
            for path in in_window:
                if path in found:
                    continue
                if reads >= DETAIL_CAP:
                    break
                reads += 1
                on, _ = self._appears_on(client, path)
                for board in on:
                    add_board(board)
        else:
            for path in self._home_boards(client):
                add_board(path)
            for path in [p for p, t in boards.items() if not any(r["board_path"] == p for r in found.values())]:
                boards.pop(path)  # pages the home page linked that list no meetings
        leftovers = [p for p in in_window if p not in found]
        for path in leftovers:  # in the sitemap, on no board page read: dated and titled from the slug alone
            words = path.split("-", 3)[-1].replace("-", " ").strip()
            found[path] = {"path": path, "date": sitemap[path], "title": words.capitalize(), "files": None, "board": "",
                           "board_path": None}
        if leftovers:
            notes.append(f"{len(leftovers)} meeting pages known only from the sitemap (no board page lists them): "
                         f"dated and titled from their address; documents() reads their pages")
        if not found and not sitemap and not boards:
            raise PortalError("no sitemap and no board page with meetings found from the home page")
        meetings = []
        for path, row in found.items():
            mid = path.lstrip("/")
            board = row["board"]
            if governing(board) is None and governing(board_name(row["title"])) is not None:
                board = board_name(row["title"])  # a page titled "Meetings / Agenda": the meeting's title names the board
            kind = meeting_kind(row["title"], board)
            gov = governing(board) if board else None
            if gov and kind == "committee":  # a committee's meeting listed on the board's page
                gov = False
            meetings.append(Meeting(id=mid, date=row["date"], title=row["title"], board=board, kind=kind, governing=gov,
                                    url=base_url(client) + path))
            self._rows[(client, mid)] = row
        meetings.sort(key=lambda m: (m.date, m.id))
        listed = [r for r in found.values() if r["files"] is not None]
        if listed:
            notes.append(minutes_share(listed))
        used = {r["board_path"] for r in found.values()}
        if used - {None}:
            notes.append("board pages: " + "; ".join(f"{p} ({t})" for p, t in boards.items() if p in used))
        return Listing(meetings, notes)

    # ------------------------------------------------------------------ documents
    def documents(self, client: str, meeting: Meeting) -> Documents:
        base = base_url(client)
        path = "/" + meeting.id.lstrip("/")
        on, root = self._appears_on(client, path)
        if root is None:
            raise PortalError(f"meeting page {path} could not be read")
        article = root.find("article", cls="full-poc") or root
        docs, seen = Documents(), set()
        for label, url in attachments(article, base):
            if url not in seen:
                seen.add(url)
                docs.append(Document(url=url, kind=classify(label, url), title=label, meeting_id=meeting.id, format=_fmt(url)))
        body = article.find(cls="body")
        for a in (body.find_all("a", attr="href") if body is not None else []):
            url = urljoin(base + "/", a.attrs["href"])
            if "/files/" in url and url not in seen:
                seen.add(url)
                docs.append(Document(url=url, kind=classify(a.text(), url), title=a.text(), meeting_id=meeting.id, format=_fmt(url)))
            elif re.search(r"youtube\.com|youtu\.be|vimeo\.com", url) and url not in seen:
                seen.add(url)
                docs.append(Document(url=url, kind="video", title=a.text() or "Video", meeting_id=meeting.id))
        for frame in article.find_all("iframe", attr="src"):
            src = urljoin("https:", frame.attrs["src"])
            if re.search(r"youtube|vimeo|granicus|swagit|zoom", src) and src not in seen:
                seen.add(src)
                docs.append(Document(url=src, kind="video", title=frame.attrs.get("title") or "Video", meeting_id=meeting.id))
        # minutes in the Minutes slot that name another date are another meeting's (draft minutes up for approval)
        board_rows = self._board_rows(client, on, meeting)
        by_date = {}
        for r in board_rows:
            by_date.setdefault(r["date"], r["path"].lstrip("/"))
        for d in docs:
            if d.kind != "minutes" or not meeting.date:
                continue
            named = dates_named(f"{d.title} {unquote_plus(urlsplit(d.url).path)}")
            if named and meeting.date not in named:
                other = max(named)
                d.meeting_id = by_date.get(other)
                d.title = f"{d.title} (names {other}: minutes of another meeting, posted with this one)"
                docs.notes.append(f"the minutes posted with this meeting name {other}: they record that meeting, not this one")
        # this meeting's minutes posted with a later meeting
        for r in board_rows:
            if r["path"].lstrip("/") == meeting.id or not r["files"]:
                continue
            for label, url in r["files"]:
                if url in seen or classify(label, url) != "minutes":
                    continue
                if meeting.date and meeting.date in dates_named(f"{label} {unquote_plus(urlsplit(url).path)}") and r["date"] != meeting.date:
                    seen.add(url)
                    docs.append(Document(url=url, kind="minutes", meeting_id=meeting.id, format=_fmt(url),
                                         title=f"{label} (posted with the {r['date']} meeting)"))
        own = [d for d in docs if d.kind == "minutes" and d.meeting_id == meeting.id]
        if not own:
            has = sorted({d.kind for d in docs if d.kind not in ("other",)} - {"minutes"})
            what = f"only {', '.join(has)}" if has else ("only other files" if docs else "no files at all")
            docs.notes.append(f"no minutes posted for this meeting: {what}"
                              + ("; none named for it on the board's other meetings either" if board_rows else ""))
        return docs

    def _board_rows(self, client: str, on: list[str], meeting: Meeting) -> list[dict]:
        if not on or not meeting.date:
            return []
        lo, hi = date(meeting.date.year, 1, 1), date(meeting.date.year + 1, 12, 31)
        rows, notes = [], []
        for path in on[:2]:
            _, r = self._board(client, path, lo, hi, notes)
            rows.extend(r)
        return rows

    def items(self, client: str, meeting: Meeting) -> list[Item]:
        return []  # Streamline posts files, not agenda items
