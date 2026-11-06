"""The IQM2 adapter, offline, on trimmed Wayback Machine captures of real IQM2 pages (fixtures under
tests/fixtures/portals/iqm2/: scripts, styles and view state removed; the calendar keeps five of its rows)."""

from datetime import date
from pathlib import Path

import pytest

from ddwb.deep.web import Response
from ddwb.portals import base, iqm2, registry

FIX = Path(__file__).parent / "fixtures" / "portals" / "iqm2"
CAL_2018 = iqm2.calendar_url("olmstedcountymn", date(2018, 1, 1), date(2018, 12, 31))


class FakeHttp:
    def __init__(self, pages: dict):
        self.pages, self.asked = pages, []

    def get(self, url, use_cache=True):
        self.asked.append(url)
        return self.pages.get(url) or Response(url, final_url=url, status=404, content_type="text/html", body=b"")

    def post(self, url, data=None, json_body=None, headers=None, use_cache=True):
        return self.get(url)


def page(url, name):
    return Response(url, final_url=url, status=200, content_type="text/html; charset=utf-8", body=(FIX / name).read_bytes())


def test_detect():
    a = iqm2.IQM2(FakeHttp({}))
    assert a.detect("https://olmstedcountymn.iqm2.com/Citizens/Default.aspx") == "olmstedcountymn"
    assert a.detect("https://prod-app.iqm2.com/") is None
    own = '<iframe src="https://pleasanthillca.iqm2.com/Citizens/Calendar.aspx?Frame=Yes" width="100%"></iframe>'
    assert a.detect("https://www.pleasanthillca.org/agendas", html=own) == "pleasanthillca"
    assert a.detect("https://www.example.gov/", html="<p>no portal here</p>") is None
    assert registry.detect("http://olmstedcountymn.iqm2.com:80/Citizens/") == [("iqm2", "olmstedcountymn")]


def test_meetings_from_the_calendar_list():
    http = FakeHttp({CAL_2018: page(CAL_2018, "olmsted_calendar_2018.html")})
    listing = iqm2.IQM2(http).meetings("olmstedcountymn", date(2018, 1, 1), date(2018, 12, 31))
    assert http.asked == [CAL_2018]
    by = {m.id: m for m in listing.meetings}
    assert set(by) == {"1661", "1687", "1688", "1595", "1564"}
    bcc = by["1661"]
    assert (bcc.date, bcc.board, bcc.kind, bcc.governing) == (date(2018, 1, 2), "Board of County Commissioners", "regular", True)
    assert bcc.url == "https://olmstedcountymn.iqm2.com/Citizens/Detail_Meeting.aspx?ID=1661"
    assert by["1595"].governing is False and by["1595"].kind == "committee"
    assert by["1564"].title.endswith("(Cancelled)")
    assert listing.per_year == {2018: 5}


def test_documents_from_the_list_row_and_from_the_meeting_page():
    http = FakeHttp({CAL_2018: page(CAL_2018, "olmsted_calendar_2018.html")})
    a = iqm2.IQM2(http)
    m = next(x for x in a.meetings("olmstedcountymn", date(2018, 1, 1), date(2018, 12, 31)).meetings if x.id == "1661")
    docs = a.documents("olmstedcountymn", m)
    assert [(d.kind, d.title) for d in docs] == [("agenda", "Agenda"), ("packet", "Agenda Packet"), ("minutes", "Summary"),
                                                ("minutes", "Minutes"), ("video", "Video")]
    assert docs[3].url == "https://olmstedcountymn.iqm2.com/Citizens/FileOpen.aspx?Type=12&ID=1441&Inline=True"
    assert docs[4].url.startswith("https://olmstedcountymn.iqm2.com/Citizens/SplitView.aspx?Mode=Video&MeetingID=1661")

    detail = iqm2.detail_url("olmstedcountymn", "1689")
    fresh = iqm2.IQM2(FakeHttp({detail: page(detail, "olmsted_detail_1689.html")}))  # no list row: the meeting page
    m = base.Meeting("1689", date(2018, 2, 15), "Oronoco Planning Advisory Commission - Special Meeting", url=detail)
    docs = fresh.documents("olmstedcountymn", m)
    assert [(d.kind, d.format) for d in docs[:4]] == [("agenda", "pdf"), ("packet", "pdf"), ("minutes", "pdf"), ("minutes", "pdf")]
    assert docs[-1].kind == "video" and docs[-1].title == "Video"
    assert fresh.items("olmstedcountymn", m) == []  # this meeting's page has no outline


def test_items_with_sections_and_attachments():
    detail = iqm2.detail_url("pleasanthillca", "1830")
    a = iqm2.IQM2(FakeHttp({detail: page(detail, "pleasanthill_detail_1830.html")}))
    items = a.items("pleasanthillca", base.Meeting("1830", date(2021, 6, 14), "Budget Committee - Special Meeting"))
    assert [i.title for i in items[:4]] == ["CALL TO ORDER", "ROLL CALL", "PUBLIC COMMENT", "ITEMS"]
    three = next(i for i in items if i.number == "3")
    assert three.title == "Discussion of Draft Master Fee Schedule for FY 2021/22" and three.section == "ITEMS"
    assert [d.title for d in three.attachments][:2] == ["Budget Committee Staff Report Printout", "Draft City Council Staff Report"]
    assert all(d.kind == "item" and "/Citizens/FileOpen.aspx?Type=" in d.url for d in three.attachments)
    assert len(three.attachments) == 6
    assert next(i for i in items if i.number == "5").attachments == []


def test_a_host_that_never_answers_is_an_error_and_a_failed_later_year_a_note():
    timeout = FakeHttp({CAL_2018: Response(CAL_2018, error="ReadTimeout: The read operation timed out")})
    with pytest.raises(base.PortalError, match="browser tier"):
        iqm2.IQM2(timeout).meetings("olmstedcountymn", date(2018, 1, 1), date(2018, 12, 31))
    cal_2019 = iqm2.calendar_url("olmstedcountymn", date(2019, 1, 1), date(2019, 3, 31))
    partial = FakeHttp({CAL_2018: page(CAL_2018, "olmsted_calendar_2018.html"),
                        cal_2019: Response(cal_2019, status=500, content_type="text/html", body=b"error")})
    listing = iqm2.IQM2(partial).meetings("olmstedcountymn", date(2018, 1, 1), date(2019, 3, 31))
    assert len(listing.meetings) == 5 and listing.notes == ["2019 not read: HTTP 500"]
    with pytest.raises(base.PortalError, match="no calendar year could be read"):
        iqm2.IQM2(FakeHttp({})).meetings("notaclient", date(2018, 1, 1), date(2018, 12, 31))  # 404 on every year
    walled = FakeHttp({CAL_2018: Response(CAL_2018, status=403, bot_wall=True)})
    with pytest.raises(base.PortalError, match="bot wall"):
        iqm2.IQM2(walled).meetings("olmstedcountymn", date(2018, 1, 1), date(2018, 12, 31))


def test_the_listing_says_where_the_calendar_ends():
    assert iqm2.calendar_years((FIX / "olmsted_calendar_2018.html").read_text())[:3] == [2019, 2018, 2017]
    http = FakeHttp({CAL_2018: page(CAL_2018, "olmsted_calendar_2018.html")})
    listing = iqm2.IQM2(http).meetings("olmstedcountymn", date(2018, 1, 1), date(2022, 12, 31))
    assert len(listing.meetings) == 5
    assert len(http.asked) == 2  # 2018, then 2019 (missing here); 2020 on are not asked for
    assert any("offers years up to 2019" in n for n in listing.notes)


def test_a_range_wholly_after_the_calendar_says_so():
    """Asked only for years past the last one the calendar offers (a body that left IQM2): no meetings, and a note."""
    late = iqm2.calendar_url("olmstedcountymn", date(2026, 6, 1), date(2026, 11, 6))
    http = FakeHttp({late: page(late, "olmsted_calendar_2018.html")})
    listing = iqm2.IQM2(http).meetings("olmstedcountymn", date(2026, 6, 1), date(2026, 11, 6))
    assert listing.meetings == [] and any("offers years up to 2019" in n for n in listing.notes)
