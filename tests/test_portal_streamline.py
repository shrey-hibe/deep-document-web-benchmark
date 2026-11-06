"""Streamline adapter, offline. Fixtures are Tubac Fire District's site (tubacfire.specialdistrict.org, also served at
www.tubacfiredistrict.org) as it answered on 2026-11-05, trimmed: five sitemap entries, the board page's 2026 tab with
its first three meetings, those three meeting pages; and three rows of Calaveras County Water District's 2025 board
listing (calaverascwd), for the <time> date markup and a single-attachment block. Tubac posts each meeting's draft
minutes with the next meeting, which the documents test follows."""

from datetime import date
from pathlib import Path

import pytest

from ddwb.deep.web import Response
from ddwb.portals import base, registry
from ddwb.portals.streamline import Streamline, read_listing

FIX = Path(__file__).parent / "fixtures" / "portals" / "streamline"
B = "https://tubacfire.specialdistrict.org"
OCT, SEP, NOV = ("2026-10-28-tubac-fire-district-board-information-meeting",
                 "2026-09-30-tubac-fire-district-board-information-meeting",
                 "2026-11-18-tubac-fire-district-board-information-meeting")


class FakeHttp:
    def __init__(self, routes=None):
        self.routes = routes if routes is not None else {
            f"{B}/sitemap.xml": "sitemap.xml", f"{B}/tubac-fire-district-board-information": "board_2026.html",
            f"{B}/{OCT}": "meeting_2026-10-28.html", f"{B}/{SEP}": "meeting_2026-09-30.html", f"{B}/{NOV}": "meeting_2026-11-18.html"}
        self.calls = []

    def get(self, url, use_cache=True):
        self.calls.append(url)
        name = self.routes.get(url)
        if isinstance(name, int):
            return Response(url, final_url=url, status=name, content_type="text/html", body=b"<html>Access denied</html>",
                            bot_wall=name in (403, 429))
        if name is None:
            return Response(url, final_url=url, status=404, content_type="text/html", body=b"<html>Not found</html>")
        ctype = "text/xml" if name.endswith(".xml") else "text/html; charset=utf-8"
        return Response(url, final_url=url, status=200, content_type=ctype, body=(FIX / name).read_bytes())

    def post(self, *a, **kw):
        raise AssertionError("Streamline needs no POST")


def test_detect_subdomain_and_custom_domain():
    a = Streamline(FakeHttp({}))
    assert a.detect("https://tubacfire.specialdistrict.org/about") == "tubacfire"
    assert a.detect("https://www.specialdistrict.org/") is None
    oct_page = (FIX / "meeting_2026-10-28.html").read_text()
    assert a.detect("https://www.tubacfiredistrict.org/", html=oct_page) == "tubacfire"  # its "Sign in" link
    sep_page = (FIX / "meeting_2026-09-30.html").read_text()  # signs in on the custom domain: the host is the client
    assert a.detect("https://www.tubacfiredistrict.org/x", html=sep_page) == "www.tubacfiredistrict.org"
    assert a.detect("https://www.example.gov/", html="<a href='/minutes'>Minutes</a>") is None
    assert "streamline" in registry.names()


def test_meetings_from_the_board_page_the_sitemap_points_to():
    http = FakeHttp()
    listing = Streamline(http).meetings("tubacfire", date(2026, 1, 1), date(2026, 12, 31))
    assert [m.date for m in listing.meetings] == [date(2026, 9, 30), date(2026, 10, 28), date(2026, 11, 18)]
    m = listing.meetings[0]
    assert m.id == SEP and m.url == f"{B}/{SEP}" and m.board == "Tubac Fire District Board Information"
    assert m.governing is True and m.kind == "regular"
    share = next(n for n in listing.notes if n.startswith("minutes found"))
    # Sep 30 has its minutes on Oct 28's row; Oct 28's own Minutes slot holds Sep 30's; Nov 18 has nothing yet
    assert share.startswith("minutes found for 1 of 3 listed meetings (33%), 1 of them posted with a later meeting")
    assert "1 with only an agenda" in share and "1 with no files" in share
    assert f"{B}/tubac-fire-district-board-information?year=2026" not in http.calls  # the active tab is the page itself


def test_documents_follow_minutes_posted_with_the_next_meeting():
    a = Streamline(FakeHttp())
    listing = a.meetings("tubacfire", date(2026, 1, 1), date(2026, 12, 31))
    sep, oct_, nov = listing.meetings
    docs = a.documents("tubacfire", oct_)
    slot = next(d for d in docs if d.url.endswith("Minutes+September+30+2026.pdf"))
    assert slot.kind == "minutes" and slot.meeting_id == SEP and "names 2026-09-30" in slot.title
    assert not [d for d in docs if d.kind == "minutes" and d.meeting_id == OCT]
    assert any("no minutes posted for this meeting: only agenda, packet" in n for n in docs.notes)
    assert {d.format for d in docs if d.url.endswith(".pdf")} == {"pdf"}
    docs = a.documents("tubacfire", sep)
    own = [d for d in docs if d.kind == "minutes" and d.meeting_id == SEP]
    assert [d.url for d in own] == [f"{B}/files/cdd1b4d51/Minutes+September+30+2026.pdf"]
    assert "posted with the 2026-10-28 meeting" in own[0].title
    docs = a.documents("tubacfire", nov)
    assert not docs and "no files at all" in docs.notes[0]


def test_listing_with_time_tiles_and_single_attachment_blocks():
    title, rows, years, active, pages = read_listing((FIX / "listing_calaveras_2025.html").read_text(),
                                                     "https://calaverascwd.specialdistrict.org")
    assert title == "Board Meetings" and active == "2025" and "2021" in years and pages == []
    assert [(r["date"], [label for label, _ in r["files"]]) for r in rows] == [
        (date(2025, 12, 24), []),
        (date(2025, 12, 15), ["Agenda", "Minutes", "Agenda Package", "Calaveras Civil Grand Jury & CCWD"]),
        (date(2025, 8, 13), ["Cancellation Notice.pdf"])]


def test_items_are_empty_and_refusals_are_errors():
    a = Streamline(FakeHttp())
    assert a.items("tubacfire", base.Meeting(SEP, date(2026, 9, 30), "x")) == []
    with pytest.raises(base.PortalError, match="bot wall"):
        Streamline(FakeHttp({f"{B}/sitemap.xml": 403})).meetings("tubacfire", date(2026, 1, 1), date(2026, 12, 31))
    with pytest.raises(base.PortalError, match="no sitemap and no board page"):
        Streamline(FakeHttp({f"{B}/": "meeting_2026-11-18.html"})).meetings("tubacfire", date(2026, 1, 1), date(2026, 12, 31))
