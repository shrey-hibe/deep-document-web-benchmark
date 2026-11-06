"""eScribe adapter, offline.

Every eScribe tenant tried on 2026-11-05 answered plain HTTP with Cloudflare's "Verifying your browser" page (HTTP 403),
so no response could be saved from the portal itself here. Fixtures:
- calendar_nationalcity_2026.json: GetCalendarMeetings rows for National City CA (pub-nationalcity) as published in City
  Bureau's city-scrapers-san-diego test files (MIT), trimmed; the first row (2026-09-01, ID ...0001) is hand-built from
  the 2024-09-17 row, adding PostMinutes and Video links in the shape eScribe uses, because no published row had minutes.
- calendar_chulavista_2025.json: one Chula Vista CA row from the same source (PostAgenda, AdditionalDocuments).
- agenda.html: hand-built in eScribe's Meeting.aspx agenda markup.
- browser_check.html: the 403 page as eScribe served it, the reference id and client address taken out.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from ddwb.deep.web import Response
from ddwb.portals import base, registry
from ddwb.portals.escribe import EScribe, parse_agenda

FIX = Path(__file__).parent / "fixtures" / "portals" / "escribe"
NC = "https://pub-nationalcity.escribemeetings.com"


class FakeHttp:
    def __init__(self, calendar: str | None = None, status: int = 200, agenda: str | None = None):
        self.calendar, self.status, self.agenda, self.posts = calendar, status, agenda, []

    def post(self, url, data=None, json_body=None, headers=None, use_cache=True):
        self.posts.append((url, json_body))
        assert url.endswith("/MeetingsCalendarView.aspx/GetCalendarMeetings") and set(json_body) == {"calendarStartDate", "calendarEndDate"}
        if self.status != 200:
            return Response(url, final_url=url, status=self.status, content_type="text/html; charset=UTF-8",
                            body=(FIX / "browser_check.html").read_bytes(), bot_wall=True)
        return Response(url, final_url=url, status=200, content_type="application/json; charset=utf-8",
                        body=(FIX / self.calendar).read_bytes())

    def get(self, url, use_cache=True):
        if self.agenda and "Meeting.aspx" in url:
            return Response(url, final_url=url, status=200, content_type="text/html", body=(FIX / self.agenda).read_bytes())
        return Response(url, status=404, content_type="text/html", body=b"")


def test_detect_from_url_and_own_page():
    a = EScribe(FakeHttp())
    assert a.detect("https://pub-nationalcity.escribemeetings.com/?Year=2026") == "pub-nationalcity"
    assert a.detect("https://bm-public-auroraco.escribemeetings.com/") == "bm-public-auroraco"
    assert a.detect("https://www.escribemeetings.com/") is None
    page = '<a href="https://pub-nationalcity.escribemeetings.com/">Agendas and Minutes</a>'
    assert a.detect("https://www.nationalcityca.gov/government/city-council", html=page) == "pub-nationalcity"
    assert a.detect("https://www.nationalcityca.gov/", html="<a href='/agendas'>Agendas</a>") is None
    assert "escribe" in registry.names()


def test_meetings_one_calendar_call_per_year():
    http = FakeHttp("calendar_nationalcity_2026.json")
    listing = EScribe(http).meetings("pub-nationalcity", date(2026, 1, 1), date(2026, 12, 31))
    assert len(http.posts) == 1 and http.posts[0][1] == {"calendarStartDate": "2026-01-01", "calendarEndDate": "2026-12-31"}
    assert listing.per_year == {2026: 4}
    first = listing.meetings[0]
    assert first.date == date(2026, 9, 1) and first.board == "City Council" and first.governing is True and first.kind == "regular"
    assert first.url == f"{NC}/Meeting.aspx?Id={first.id}&Agenda=Agenda&lang=English"
    special = next(m for m in listing.meetings if m.date == date(2026, 9, 9))
    assert special.kind == "special" and special.board == "City Council"
    assert any("minutes listed for 1 of 4" in n for n in listing.notes)


def test_documents_minutes_video_and_agenda_only():
    a = EScribe(FakeHttp("calendar_nationalcity_2026.json"))
    listing = a.meetings("pub-nationalcity", date(2026, 1, 1), date(2026, 12, 31))
    docs = a.documents("pub-nationalcity", listing.meetings[0])
    minutes = [d for d in docs if d.kind == "minutes"]
    assert [(d.url, d.format) for d in minutes] == [
        (f"{NC}/FileStream.ashx?DocumentId=90010", "pdf"),
        (f"{NC}/Meeting.aspx?Id=00000000-0000-4000-8000-000000000001&Agenda=PostMinutes&lang=English", "html")]
    assert any(d.kind == "video" and "ISIStandAlonePlayer.aspx" in d.url for d in docs)
    assert docs.notes == []
    closed = next(m for m in listing.meetings if m.date == date(2026, 9, 9))
    docs = a.documents("pub-nationalcity", closed)
    assert {d.kind for d in docs} == {"agenda"} and "no minutes posted" in docs.notes[-1]
    upcoming = next(m for m in listing.meetings if m.date == date(2026, 10, 6))
    docs = a.documents("pub-nationalcity", upcoming)
    assert not docs and any("no agenda posted yet" in n for n in docs.notes)


def test_additional_documents_are_classified_by_title():
    a = EScribe(FakeHttp("calendar_chulavista_2025.json"))
    m = a.meetings("pub-chulavista", date(2025, 1, 1), date(2025, 12, 31)).meetings[0]
    assert m.board == "Board of Ethics" and m.governing is False
    kinds = {d.title: d.kind for d in a.documents("pub-chulavista", m)}
    assert kinds["Post Agenda (PDF)"] == "packet" and kinds["Board of Ethics Regular Meeting Agenda - Spanish"] == "agenda"


def test_items_sections_and_attachments():
    a = EScribe(FakeHttp("calendar_nationalcity_2026.json", agenda="agenda.html"))
    m = a.meetings("pub-nationalcity", date(2026, 1, 1), date(2026, 12, 31)).meetings[0]
    items = a.items("pub-nationalcity", m)
    assert [(i.number, i.section) for i in items] == [("1", None), ("7.1", "7. CONSENT CALENDAR"),
                                                      ("7.2", "7. CONSENT CALENDAR"), ("8", None)]
    assert [d.url for d in items[1].attachments] == [f"{NC}/FileStream.ashx?DocumentId=9360"]  # inline and Details: once
    assert items[1].text == "Recommended Action: Approve the minutes of August 18, 2026."
    assert items[2].attachments[0].title == "Staff Report - Street Resurfacing.pdf" and items[2].attachments[0].item_no == "7.2"


def test_the_browser_check_is_a_portal_error_that_says_so():
    a = EScribe(FakeHttp(status=403))
    with pytest.raises(base.PortalError, match="browser check.*browser tier"):
        a.meetings("pub-slocity", date(2025, 1, 1), date(2026, 12, 31))


def test_an_answer_that_is_not_a_calendar_is_an_error():
    class Odd(FakeHttp):
        def post(self, url, **kw):
            return Response(url, final_url=url, status=200, content_type="application/json", body=json.dumps({"d": None}).encode())

    with pytest.raises(base.PortalError, match="not a list"):
        EScribe(Odd()).meetings("pub-nationalcity", date(2026, 1, 1), date(2026, 12, 31))
