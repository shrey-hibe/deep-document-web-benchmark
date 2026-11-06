"""NovusAgenda adapter, offline. Fixtures are Mountain View Whisman School District's site (mvwsd.novusagenda.com) as it
answered on 2026-11-05, trimmed to the form, the meetings grid and its pager: the search page, the two pages of the
2025 search (25 meetings), and the 2025-09-04 agenda up to its consent items; error.html is the page Dubuque's site
(cityofdubuque) sends in place of the search page."""

from datetime import date
from pathlib import Path

import pytest

from ddwb.deep.web import Response
from ddwb.portals import base, registry
from ddwb.portals.novusagenda import P, NovusAgenda

FIX = Path(__file__).parent / "fixtures" / "portals" / "novusagenda"
B = "https://mvwsd.novusagenda.com/agendapublic/"
NEXT = "ctl00$ContentPlaceHolder1$SearchAgendasMeetings$radGridMeetings$ctl00$ctl03$ctl01$ctl10"


class FakeHttp:
    def __init__(self, search="search.html", status=200):
        self.search, self.status, self.posts = search, status, []

    def _resp(self, url, name, status=200):
        return Response(url, final_url=url, status=status, content_type="text/html; charset=utf-8",
                        body=(FIX / name).read_bytes(), bot_wall=status in (403, 429))

    def get(self, url, use_cache=True):
        if url == B:
            return self._resp(url, self.search, self.status)
        if url.startswith(B + "MeetingView.aspx?MeetingID=301&"):
            return self._resp(url, "agenda.html")
        return Response(url, status=404, content_type="text/html", body=b"")

    def post(self, url, data=None, json_body=None, headers=None, use_cache=True):
        self.posts.append(data)
        assert url == B
        if data["__EVENTTARGET"] == "":
            assert data["__VIEWSTATE"] == "VS0" and data[P + "ddlDateRange"] == "cus"
            assert data[P + "radCalendarFrom"] == "2025-01-01" and data[P + "radCalendarTo$dateInput"] == "12/31/2025"
            return self._resp(url, "results_1.html")
        assert data["__EVENTTARGET"] == NEXT and data["__VIEWSTATE"] == "VS1"  # the page's own state, posted back
        assert not any(k.endswith(".x") for k in data)
        return self._resp(url, "results_2.html")


def test_detect_from_url_and_own_page():
    a = NovusAgenda(FakeHttp())
    assert a.detect("https://mvwsd.novusagenda.com/agendapublic/") == "mvwsd"
    assert a.detect("https://www.novusagenda.com/") is None
    page = '<a href="https://mvwsd.novusagenda.com/AgendaPublic/Meetings.aspx">Board Agendas</a>'
    assert a.detect("https://www.mvwsd.org/board", html=page) == "mvwsd"
    assert a.detect("https://www.mvwsd.org/") is None
    assert "novusagenda" in registry.names()


def test_meetings_post_the_search_and_follow_the_pager():
    http = FakeHttp()
    listing = NovusAgenda(http).meetings("mvwsd", date(2025, 1, 1), date(2025, 12, 31))
    assert len(http.posts) == 2
    assert listing.per_year == {2025: 25} and len({m.id for m in listing.meetings}) == 25
    m = next(m for m in listing.meetings if m.id == "301")
    assert m.date == date(2025, 9, 4) and m.board == "Board of Trustees" and m.governing is True and m.kind == "regular"
    assert any("minutes listed for 24 of 25" in n for n in listing.notes)


def test_documents_minutes_and_agenda_only():
    a = NovusAgenda(FakeHttp())
    listing = a.meetings("mvwsd", date(2025, 1, 1), date(2025, 12, 31))
    docs = a.documents("mvwsd", next(m for m in listing.meetings if m.id == "301"))
    assert [(d.kind, d.format, d.url.rsplit("/", 1)[1]) for d in docs] == [
        ("agenda", "html", "MeetingView.aspx?MeetingID=301&MinutesMeetingID=307&doctype=Agenda"),
        ("agenda", "pdf", "DisplayAgendaPDF.ashx?MeetingID=301"),
        ("minutes", "html", "MeetingView.aspx?MeetingID=301&MinutesMeetingID=307&doctype=Minutes"),
        ("minutes", "pdf", "DisplayAgendaPDF.ashx?MinutesMeetingID=307")]
    docs = a.documents("mvwsd", next(m for m in listing.meetings if m.id == "292"))
    assert {d.kind for d in docs} == {"agenda"} and "no minutes posted" in docs.notes[0]


def test_items_follow_the_outline():
    a = NovusAgenda(FakeHttp())
    m = base.Meeting("301", date(2025, 9, 4), "Board of Trustees Regular Meeting")
    items = a.items("mvwsd", m)
    assert (items[0].number, items[0].title, items[0].section) == ("A", "Pledge", "I. CALL TO ORDER (6:00 p.m.)")
    minutes = [i for i in items if i.section == "B. Minutes"]
    assert [i.number for i in minutes] == ["1", "2", "3", "4"]
    assert minutes[0].attachments[0].url.startswith(B + "CoverSheet.aspx?ItemID=")
    closed = next(i for i in items if i.title.startswith("CONFERENCE WITH REAL PROPERTY"))
    assert closed.text.startswith("Property:")  # the unnumbered row under it
    assert next(i for i in items if i.number == "III").attachments == []  # an item with no cover sheet


def test_the_error_page_and_a_bot_wall_are_errors():
    with pytest.raises(base.PortalError, match="without the meetings grid.*HTTP error"):
        NovusAgenda(FakeHttp(search="error.html")).meetings("mvwsd", date(2025, 1, 1), date(2025, 12, 31))
    with pytest.raises(base.PortalError, match="bot wall"):
        NovusAgenda(FakeHttp(status=403)).meetings("mvwsd", date(2025, 1, 1), date(2025, 12, 31))
