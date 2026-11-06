"""The CivicPlus AgendaCenter adapter, offline, against trimmed pages of a real site (Vernon Hills, IL)."""

from datetime import date
from pathlib import Path

import pytest

from ddwb.deep.web import Response
from ddwb.portals import civic_common, registry
from ddwb.portals.base import Meeting, PortalError
from ddwb.portals.civicplus_agendacenter import AgendaCenter

FIX = Path(__file__).parent / "fixtures" / "portals" / "civicplus_agendacenter"
SITE = "https://vernonhills.org"
FIELDS = {"year", "catID", "startDate", "endDate", "term", "prevVersionScreen"}  # what the site's own script sends


class FakeSite:
    """The site's main page and its year requests; a year request without every field gets 400, as the real one does."""

    def __init__(self, years: dict[tuple[str, str], tuple[int, bytes]], main: tuple[int, bytes] | None = None):
        self.years, self.posts = years, []
        self.main = main or (200, (FIX / "vernonhills_main.html").read_bytes())

    def _resp(self, url, status, body):
        return Response(url, final_url=url, status=status, content_type="text/html; charset=utf-8", body=body,
                        bot_wall=status in (403, 429))

    def get(self, url, use_cache=True):
        if url == f"{SITE}/AgendaCenter":
            return self._resp(url, *self.main)
        return self._resp(url, 404, b"not found")

    def post(self, url, data=None, json_body=None, headers=None, use_cache=True):
        self.posts.append(dict(data or {}))
        if url != f"{SITE}/AgendaCenter/UpdateCategoryList" or set(data or {}) != FIELDS:
            return self._resp(url, 400, b"Bad Request")
        return self._resp(url, *self.years.get((data["catID"], data["year"]), (200, b"")))


def _years():
    return {("17", "2025"): (200, (FIX / "vernonhills_cat17_2025.html").read_bytes()),
            ("2", "2025"): (500, b"Server Error")}


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(civic_common, "BACKOFF", (0.0, 0.0))


def test_detect_by_path_by_page_and_by_link():
    a = AgendaCenter(FakeSite({}))
    assert a.detect("https://www.vernonhills.org/AgendaCenter") == "vernonhills.org"
    assert a.detect("https://il-peoriacounty.civicplus.com/AgendaCenter/ViewFile/Agenda/_09102026-1949") == "il-peoriacounty.civicplus.com"
    assert a.detect("https://www.vernonhills.org/") is None
    main = (FIX / "vernonhills_main.html").read_text()
    assert a.detect("https://www.vernonhills.org/155/Agendas-Minutes", html=main) == "vernonhills.org"
    nav = '<nav><a href="/AgendaCenter">Agendas &amp; Minutes</a> <a href="/Calendar.aspx">Calendar</a></nav>'
    assert a.detect("https://www.vernonhills.org/", html=nav) == "vernonhills.org"
    elsewhere = '<a href="https://www.co.example.il.us/AgendaCenter/Search/">County agendas</a>'
    assert a.detect("https://village.example.org/", html=elsewhere) == "co.example.il.us"
    assert a.detect("https://www.vernonhills.org/", html="<p>Agendas are posted at Village Hall.</p>") is None
    assert ("civicplus_agendacenter", "vernonhills.org") in registry.detect("https://www.vernonhills.org/AgendaCenter")


def test_meetings_read_each_category_year_with_every_field_and_join_packet_rows():
    site = FakeSite(_years())
    listing = AgendaCenter(site).meetings("vernonhills.org", date(2025, 1, 1), date(2025, 12, 31))
    assert {p["catID"] for p in site.posts} == {"17", "2"} and all(set(p) == FIELDS for p in site.posts)
    assert all(p["year"] == "2025" for p in site.posts)  # 2026 is on the main page, other years are outside the window
    # four rows, three meetings: "Village Board Meeting Full Packet" is the packet of the Village Board meeting
    assert [(m.id, m.title) for m in listing.meetings] == [
        ("1345", "Committee of the Whole Meeting"), ("1349", "Village Board Meeting"), ("1350", "Committee of the Whole Meeting")]
    board = next(m for m in listing.meetings if m.id == "1349")
    assert board.governing is True and board.kind == "regular"
    assert next(m for m in listing.meetings if m.id == "1350").governing is False
    assert any("H. Planning & Zoning Commission 2025" in n and "HTTP 500" in n for n in listing.notes)
    assert listing.per_year == {2025: 3}


def test_documents_give_agenda_packet_and_minutes_files_even_without_the_listing():
    listed = AgendaCenter(FakeSite(_years()))
    board = next(m for m in listed.meetings("vernonhills.org", date(2025, 1, 1), date(2025, 12, 31)).meetings if m.id == "1349")
    fresh = AgendaCenter(FakeSite(_years()))  # a new run that only has the meeting
    for adapter in (listed, fresh):
        docs = {d.kind: d for d in adapter.documents("vernonhills.org", board)}
        assert docs["minutes"].url == f"{SITE}/AgendaCenter/ViewFile/Minutes/_12092025-1349"
        assert docs["agenda"].url == f"{SITE}/AgendaCenter/ViewFile/Agenda/_12092025-1349" and docs["agenda"].format == "pdf"
        assert docs["packet"].url == f"{SITE}/AgendaCenter/ViewFile/Agenda/_12092025-1351"
        assert not any("PreviousVersions" in d.url for d in adapter.documents("vernonhills.org", board))
    assert fresh.items("vernonhills.org", board) == []


def test_a_bot_wall_or_a_lost_meeting_is_an_error_not_an_empty_answer():
    with pytest.raises(PortalError, match="bot wall"):
        AgendaCenter(FakeSite({}, main=(403, b"Forbidden"))).meetings("vernonhills.org", date(2025, 1, 1), date(2025, 12, 31))
    gone = Meeting("9999", date(2025, 12, 9), "Village Board Meeting",
                   url=f"{SITE}/AgendaCenter/Village-Board-17/?#_12092025-9999")
    with pytest.raises(PortalError, match="no longer listed"):
        AgendaCenter(FakeSite(_years())).documents("vernonhills.org", gone)
