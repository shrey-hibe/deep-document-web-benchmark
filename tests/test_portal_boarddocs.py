"""The BoardDocs adapter, offline: saved (trimmed) responses from Orange Unified's portal, a fake HTTP client and a fake
browser. Nothing here reaches the network or starts a browser."""

from datetime import date
from pathlib import Path

import pytest

from ddwb.deep.web import Response
from ddwb.portals import base, boarddocs, registry
from ddwb.portals.browser import Challenge, Visit

FIX = Path(__file__).parent / "fixtures" / "portals" / "boarddocs"
SITE = "https://go.boarddocs.com/ca/orangeusd/Board.nsf"


def fixture(name: str) -> bytes:
    return (FIX / name).read_bytes()


def ok(url: str, body: bytes, content_type: str = "text/html") -> Response:
    return Response(url, final_url=url, status=200, content_type=content_type, body=body)


def blocked(url: str) -> Response:
    return Response(url, final_url=url, status=403, content_type="text/html", body=fixture("blocked.html"), bot_wall=True)


class Portal:
    """Answers BoardDocs' endpoints from the fixtures; `wall` answers every request with CloudFront's 403."""

    def __init__(self, wall: bool = False, minutes: bytes | None = None) -> None:
        self.wall, self.minutes, self.calls = wall, fixture("minutes.html") if minutes is None else minutes, []

    def answer(self, method: str, url: str, data=None) -> Response:
        self.calls.append((method, url, data))
        if self.wall:
            return blocked(url)
        if url.endswith("/Public"):
            return ok(url, fixture("public.html"))
        if "BD-GetMeetingsList" in url and method == "POST":
            return ok(url, fixture("meetings_list.json"), "application/json; charset=utf-8")
        if "PRINT-AgendaDetailed" in url:
            return ok(url, fixture("print_agenda.html"))
        if "BD-GetMinutes" in url:
            return ok(url, self.minutes)
        return Response(url, status=404, body=b"not found")

    def get(self, url, use_cache=True):
        return self.answer("GET", url)

    def post(self, url, data=None, json_body=None, headers=None, use_cache=True):
        return self.answer("POST", url, data)


class FakeBrowser(Portal):
    """The browser tier's shape: open() a page, then get/post sent from it."""

    def __init__(self, wall: bool = False, challenge: bool = False) -> None:
        super().__init__(wall)
        self.challenge, self.opened, self.closed = challenge, [], False

    def open(self, url, ready=None, ready_ms=15_000, until_url=None, side=False):
        self.opened.append(url)
        if self.challenge:
            raise Challenge(f"{url}: a challenge frame; stopped there, not solved")
        return Visit(url, url, 200, "Featured - BoardDocs", fixture("public.html").decode())

    def close(self):
        self.closed = True


def test_detect_from_portal_urls_and_a_bodys_own_page():
    a = boarddocs.BoardDocs(Portal())
    assert a.detect(f"{SITE}/Public") == "ca/orangeusd"
    assert a.detect("https://go.boarddocs.com/CA/OrangeUSD/Board.nsf/goto?open&id=DSRLUF583E5B") == "ca/orangeusd"
    assert a.detect("https://www.boarddocs.com/ca/wiseburn/Board.nsf/vpublic?open") == "ca/wiseburn"
    assert a.detect("https://go.boarddocs.com//il/gls36/Board.nsf/Public") == "il/gls36"
    assert a.detect("https://www.orangeusd.org/board") is None
    assert a.detect("https://www.orangeusd.org/board", html=fixture("own_site.html").decode()) == "ca/orangeusd"
    assert a.detect("https://www.orangeusd.org/", html="<a href='https://www.legistar.com/'>x</a>") is None
    assert registry.detect(f"{SITE}/Public") == [("boarddocs", "ca/orangeusd")]


def test_meetings_over_plain_http_every_board_in_the_window():
    portal = Portal()
    listing = boarddocs.BoardDocs(portal).meetings("ca/orangeusd", date(2025, 1, 1), date(2026, 12, 31))
    assert listing.per_year == {2025: 2, 2026: 2} and listing.notes == []
    first = listing.meetings[0]
    assert (first.id, first.date, first.title) == ("DAANW961B2EA", date(2025, 12, 15), "Regular Board Meeting")
    assert (first.board, first.kind, first.governing) == ("Board of Education", "regular", True)
    assert first.url == f"{SITE}/goto?open&id=DAANW961B2EA"
    assert [m.kind for m in listing.meetings] == ["regular", "work_session", "regular", "regular"]
    posted = [c for c in portal.calls if c[0] == "POST"]
    assert posted == [("POST", f"{SITE}/BD-GetMeetingsList?open", {"current_committee_id": "A4EP6J588C05"})]


def test_cloudfront_refusing_plain_http_moves_to_the_browser_and_says_so():
    browser = FakeBrowser()
    a = boarddocs.BoardDocs(Portal(wall=True), browser=browser)
    listing = a.meetings("ca/orangeusd", date(2023, 1, 1), date(2026, 12, 31))
    assert len(listing.meetings) == 6 and listing.per_year == {2023: 2, 2025: 2, 2026: 2}
    assert any("plain HTTP refused (HTTP 403" in n and "browser tier" in n for n in listing.notes)
    assert browser.opened == [f"{SITE}/Public"]  # the portal page once; every call after it is sent from that page
    assert a.fetch_for("ca/orangeusd") is browser
    a.close()
    assert browser.closed


def test_a_wall_in_the_browser_too_is_a_portal_error_never_an_empty_answer():
    with pytest.raises(base.PortalError, match="not solved"):
        boarddocs.BoardDocs(Portal(wall=True), browser=FakeBrowser(challenge=True)).meetings("ca/orangeusd", date(2025, 1, 1), date(2026, 12, 31))
    with pytest.raises(base.PortalError, match="not solved"):
        boarddocs.BoardDocs(Portal(wall=True), browser=FakeBrowser(wall=True)).meetings("ca/orangeusd", date(2025, 1, 1), date(2026, 12, 31))
    with pytest.raises(base.PortalError, match="browser tier is switched off"):
        boarddocs.BoardDocs(Portal(wall=True), browser=False).meetings("ca/orangeusd", date(2025, 1, 1), date(2026, 12, 31))


def test_a_site_that_moved_off_boarddocs_says_where_it_went():
    class Moved(FakeBrowser):
        def open(self, url, **kw):
            return Visit(url, "https://eaglecounty.community.diligentoneplatform.com/Portal/VirtualLibrary.aspx", 200,
                         "Eagle County Government - E-Resources", "<html></html>")

    with pytest.raises(base.PortalError, match="moved off BoardDocs.*diligent"):
        boarddocs.BoardDocs(Portal(wall=True), browser=Moved()).meetings("co/eagleco", date(2025, 1, 1), date(2026, 12, 31))


def test_documents_find_the_minutes_the_agenda_and_the_items_files():
    a = boarddocs.BoardDocs(Portal())
    meeting = base.Meeting("DSRLUF583E5B", date(2026, 6, 18), "Regular Board Meeting")
    docs = a.documents("ca/orangeusd", meeting)
    kinds = [d.kind for d in docs]
    assert kinds[:2] == ["agenda", "minutes"] and kinds.count("item") == 4
    agenda, minutes = docs[0], docs[1]
    assert agenda.url == f"{SITE}/PRINT-AgendaDetailed?open&id=DSRLUF583E5B" and agenda.format == "html"
    assert minutes.url == f"{SITE}/BD-GetMinutes?open&id=DSRLUF583E5B" and minutes.title.startswith("Minutes: APPROVED MINUTES")
    first_file = docs[2]
    assert first_file.url == f"{SITE}/files/DTLKEK5183E5/$file/Section%20B%20Governance%20and%20Administration.pdf"
    assert (first_file.item_no, first_file.format, first_file.title) == ("7.A", "pdf", "Section B Governance and Administration.pdf")
    assert all(d.meeting_id == "DSRLUF583E5B" for d in docs)


def test_no_minutes_in_boarddocs_means_no_minutes_document():
    a = boarddocs.BoardDocs(Portal(minutes=b"\n\n"))
    docs = a.documents("ca/orangeusd", base.Meeting("DSRLUF583E5B", date(2026, 6, 18), "Regular Board Meeting"))
    assert "minutes" not in [d.kind for d in docs] and docs[0].kind == "agenda"


def test_items_number_section_files_votes_and_text():
    items = boarddocs.BoardDocs(Portal()).items("ca/orangeusd", base.Meeting("DSRLUF583E5B", date(2026, 6, 18), "Regular"))
    assert [i.number for i in items] == ["1.A", "3.D", "5.A", "7.A"]
    opening, adoption, minutes, hearing = items
    assert (opening.title, opening.section) == ("Call Meeting to Order", "1. OPENING ITEMS")
    assert adoption.title == "Adoption of Agenda" and "Motion carries" in adoption.outcome
    assert "Motion by Matthew Thomas, second by Sara Pelly." in adoption.outcome
    assert adoption.text == "Recommended action: Approval"
    assert minutes.title.startswith("Approval of Minutes") and minutes.attachments == []
    assert len(hearing.attachments) == 4 and {d.kind for d in hearing.attachments} == {"item"}
    assert hearing.text.startswith("DESCRIPTION: The California Department of Education requires")


def test_parsers_on_their_own():
    assert boarddocs.parse_committees('<a class="committee-trigger" committeeid="AAL7TX1B3857" aria-label="Main Governing Board">'
                                      'Main Governing Board</a><a class="committee-trigger" committeeid="AB12CD34EF56">'
                                      'Finance Committee</a>') == [("AAL7TX1B3857", "Main Governing Board"),
                                                                   ("AB12CD34EF56", "Finance Committee")]
    assert boarddocs.governing_board("Finance Committee", 2) is False
    assert boarddocs.governing_board("Main Governing Board", 2) is True
    assert boarddocs.governing_board("Board of Commissioners", 3) is True
    assert boarddocs.governing_board("Superintendent's Cabinet", 2) is None
    assert boarddocs.meeting_kind("Special Board Meeting Closed Session") == "special"
    assert boarddocs.meeting_kind("Audit Committee Meeting") == "committee"
    assert boarddocs.site_url("ca/orangeusd") == SITE
    with pytest.raises(base.PortalError):
        boarddocs.site_url("orangeusd")
    assert boarddocs.minutes_text("<div>&nbsp;</div>\n<p> </p>") == ""
    files = boarddocs._file_document(f"{SITE}/files/X1/$file/Minutes%206-4-26.pdf", "Minutes 6-4-26.pdf (120 KB)", "M", "5.A")
    assert (files.kind, files.title) == ("minutes", "Minutes 6-4-26.pdf")


def test_minutes_kept_as_files_are_found_on_the_next_meetings_agenda():
    """Gallup-McKinley files each meeting's minutes on a later agenda's approval item, not in BD-GetMinutes."""
    gmcs = "https://go.boarddocs.com/nm/gmcs/Board.nsf"

    class Filed(Portal):
        def answer(self, method, url, data=None):
            self.calls.append((method, url, data))
            if "BD-GetMeetingsList" in url:
                return ok(url, b'[{"unique": "DXSEPT17AAAA", "name": "Regular Meeting", "numberdate": "20260917"},'
                               b' {"unique": "DY2P3K62A9E0", "name": "Regular Meeting", "numberdate": "20261015"}]')
            if url.endswith("/Public"):
                return ok(url, fixture("public.html"))
            if "BD-GetMinutes" in url:
                return ok(url, b"")  # this board keeps no minutes in BoardDocs
            if "PRINT-AgendaDetailed" in url and "DY2P3K62A9E0" in url:
                return ok(url, fixture("print_agenda_minutes_filed.html"))
            if "PRINT-AgendaDetailed" in url:
                return ok(url, fixture("print_agenda.html"))
            return Response(url, status=404)

    portal = Filed()
    docs = boarddocs.BoardDocs(portal).documents("nm/gmcs", base.Meeting("DXSEPT17AAAA", date(2026, 9, 17), "Regular Meeting"))
    minutes = [d for d in docs if d.kind == "minutes"]
    assert len(minutes) == 1 and minutes[0].meeting_id == "DXSEPT17AAAA"
    assert minutes[0].url == f"{gmcs}/files/DYLT5L75B46F/$file/September%2017%2C%202026%20Regular%20Board%20Meeting%20Minutes.pdf"
    assert "filed with item 1.C of the 2026-10-15 meeting" in minutes[0].title
    later = boarddocs.BoardDocs(Filed()).documents("nm/gmcs", base.Meeting("DY2P3K62A9E0", date(2026, 10, 15), "Regular Meeting"))
    filed_here = [d for d in later if "Minutes.pdf" in d.title]
    assert len(filed_here) == 2 and {d.kind for d in filed_here} == {"item"}  # earlier meetings' minutes, approved here
