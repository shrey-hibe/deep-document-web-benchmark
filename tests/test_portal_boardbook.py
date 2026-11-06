"""The BoardBook adapter, offline, on trimmed pages saved from Rains ISD TX (644), Northwest ISD TX (1405) and
Grayslake CHSD 127 IL (1736) on 2026-11-05."""

from datetime import date
from pathlib import Path

import pytest

from ddwb.deep.web import Response
from ddwb.portals import base, registry
from ddwb.portals.boardbook import BoardBook, org_of, pick_minutes

FIX = Path(__file__).parent / "fixtures" / "portals" / "boardbook"
BB = "https://meetings.boardbook.org"
VIEWER = '<h3>Public Notice</h3><input type="hidden" id="NewDocumentViewerDocumentID" value="{}" />'


class Fake:
    """A Fetch that answers from saved pages and fails the test on any other URL."""

    def __init__(self, pages: dict[str, object]) -> None:
        self.pages, self.asked = pages, []

    def get(self, url, use_cache=True):
        self.asked.append(url)
        page = self.pages.get(url)
        if page is None:
            raise AssertionError(f"unexpected request {url}")
        if isinstance(page, Response):
            return page
        final, text = page if isinstance(page, tuple) else (url, page)
        body = (FIX / text).read_bytes() if text.endswith(".html") and "<" not in text else text.encode()
        return Response(url, final_url=final, status=200, content_type="text/html; charset=utf-8", body=body)

    def post(self, url, data=None, json_body=None, headers=None, use_cache=True):
        raise AssertionError("BoardBook needs no POST")


def test_detect_from_urls_and_a_bodys_page():
    a = BoardBook(Fake({}))
    assert a.detect(f"{BB}/Public/Organization/644") == "644"
    assert a.detect(f"{BB}/Public/Agenda/1352?meeting=695476") == "1352"
    assert a.detect("https://meetings.boardbook.org/public/organization/2366  ") == "2366"
    assert a.detect(f"{BB}/Documents/DownloadPDF/14544506?org=644") == "644"
    assert a.detect(f"{BB}/Documents/FileViewerOrPublic/1405?file=14488268") == "1405"
    assert a.detect("https://www.rainsisd.org/board") is None
    page = ('<p>Agendas are posted on <a href="https://meetings.boardbook.org/Public/Organization/644">BoardBook</a></p>')
    assert a.detect("https://www.rainsisd.org/board", page) == "644"
    framed = '<iframe src="https://meetings.boardbook.org/Public/Organization/1405" width="100%"></iframe>'
    assert a.detect("https://www.nisdtx.org/board", framed) == "1405"
    assert org_of(f"{BB}/Public/Organization/") is None


def test_meetings_kinds_boards_and_the_retention_note():
    a = BoardBook(Fake({f"{BB}/Public/Organization/644": "org_rains.html",
                        f"{BB}/Public/Organization/1736": "org_grayslake_committee.html"}))
    listing = a.meetings("644", date(2020, 1, 1), date(2026, 12, 31))
    by_id = {m.id: m for m in listing.meetings}
    assert set(by_id) == {"766487", "760951", "760929"}
    assert by_id["760929"].kind == "special" and by_id["760929"].governing  # typed "Regular", titled "Special Meeting"
    assert by_id["760951"].kind == "regular" and by_id["760951"].board == "Rains ISD"
    assert by_id["760951"].url == f"{BB}/Public/Agenda/644?meeting=760951"
    assert listing.per_year == {2026: 3}
    assert any("starts at 2026-08-24" in n for n in listing.notes)  # nothing older is listed: said, not silent
    committee = a.meetings("1736", date(2026, 1, 1), date(2026, 12, 31)).meetings
    assert [(m.kind, m.governing, m.board) for m in committee] == [("committee", False, "Committee of the Whole")]


def test_documents_find_the_minutes_pdf_behind_the_viewer():
    a = BoardBook(Fake({
        f"{BB}/Public/Organization/644": "org_rains.html",
        f"{BB}/Public/Minutes/644?meeting=760951": (f"{BB}/Documents/CustomMinutesForMeeting/644?meeting=760951",
                                                    "minutes_viewer_rains.html"),
        f"{BB}/Public/Agenda/644?meeting=760951": "agenda_rains.html",
    }))
    meeting = next(m for m in a.meetings("644", date(2026, 1, 1), date(2026, 12, 31)).meetings if m.id == "760951")
    docs = a.documents("644", meeting)
    kinds = {d.kind for d in docs}
    assert kinds == {"agenda", "minutes", "item"}
    minutes = next(d for d in docs if d.kind == "minutes")
    assert minutes.url == f"{BB}/Documents/DownloadPDF/14544506?org=644" and minutes.format == "pdf"
    assert "Minutes" in minutes.title
    agenda = next(d for d in docs if d.kind == "agenda")
    assert agenda.url == f"{BB}/Public/DownloadAgenda/644?meeting=760951"
    assert all(d.url.startswith(f"{BB}/Documents/DownloadPDF/") for d in docs if d.kind == "item")


def test_supplementals_and_notice_are_listed_as_files():
    a = BoardBook(Fake({
        f"{BB}/Public/Organization/644": "org_rains.html",
        f"{BB}/Public/PublicNotice/644?meeting=760929": VIEWER.format(14400000),
        f"{BB}/Meeting/Supplementals/644?meeting=760929": "supplementals_rains.html",
        f"{BB}/Public/Minutes/644?meeting=760929": VIEWER.format(14423789),
        f"{BB}/Public/Agenda/644?meeting=760929": "<html><table></table></html>",
    }))
    meeting = next(m for m in a.meetings("644", date(2026, 1, 1), date(2026, 12, 31)).meetings if m.id == "760929")
    docs = a.documents("644", meeting)
    assert any(d.kind == "notice" and d.url == f"{BB}/Documents/DownloadPDF/14400000?org=644" for d in docs)
    assert any(d.kind == "other" and d.url == f"{BB}/Documents/DownloadPDF/14329865?org=644" for d in docs)


def test_minutes_found_on_a_later_agenda_when_the_meeting_links_none():
    """Northwest ISD links no minutes: September's are attachments of the October 12 agenda's minutes item."""
    pages = {f"{BB}/Public/Organization/1405": "org_northwest.html",
             f"{BB}/Public/Agenda/1405?meeting=763316": "agenda_northwest_later.html",
             f"{BB}/Public/Agenda/1405?meeting=763314": "<html><table></table></html>",
             f"{BB}/Public/Agenda/1405?meeting=757550": "<html><table></table></html>",
             f"{BB}/Public/Agenda/1405?meeting=763312": "<html><table></table></html>"}
    for m in ("763316", "757550", "763312", "763314"):
        pages[f"{BB}/Public/PublicNotice/1405?meeting={m}"] = VIEWER.format(1)
    a = BoardBook(Fake(pages))
    listing = a.meetings("1405", date(2026, 9, 1), date(2026, 10, 31))
    assert any("later agendas" in n for n in listing.notes)
    by_date = {(m.date, m.kind): m for m in listing.meetings}
    regular = a.documents("1405", by_date[(date(2026, 9, 28), "regular")])
    minutes = [d for d in regular if d.kind == "minutes"]
    assert [d.url for d in minutes] == [f"{BB}/Documents/DownloadPDF/14488268?org=1405"]  # a .doc, served as PDF
    assert minutes[0].title.startswith("Minutes - September 28 2026") and minutes[0].meeting_id == "757550"
    special = a.documents("1405", by_date[(date(2026, 9, 22), "special")])
    assert [d.title.split(" (")[0] for d in special if d.kind == "minutes"] == ["Minutes - September 22 2026 - Special Meeting"]
    later = a.documents("1405", by_date[(date(2026, 10, 12), "regular")])
    assert not [d for d in later if d.kind == "minutes"]  # earlier meetings' minutes on its agenda stay items here
    assert sum(d.kind == "item" for d in later) == 5


def test_pick_minutes_tells_special_from_regular():
    docs = [(base.Document("u1", "item", "Minutes - September 14 2026 - Special Meeting"), ""),
            (base.Document("u2", "item", "Minutes - September 14 2026"), "")]
    assert [d.url for d, _ in pick_minutes("Special Meeting", docs)] == ["u1"]
    assert [d.url for d, _ in pick_minutes("Regular Meeting", docs)] == ["u2"]


def test_items_numbered_as_printed_with_sections_and_attachments():
    a = BoardBook(Fake({f"{BB}/Public/Organization/644": "org_rains.html",
                        f"{BB}/Public/Agenda/644?meeting=760951": "agenda_rains.html"}))
    meeting = next(m for m in a.meetings("644", date(2026, 1, 1), date(2026, 12, 31)).meetings if m.id == "760951")
    items = a.items("644", meeting)
    assert [i.number for i in items] == ["I", "II", "III", "III.A", "III.A.1", "III.A.2"]
    finance = items[-1]
    assert finance.title == "Financial Reports" and finance.section == "III. ITEMS FOR DISCUSSION AND/OR ACTION"
    assert len(finance.attachments) == 4 and finance.attachments[0].item_no == "III.A.2"
    assert finance.text and "Financial reports" in finance.text


@pytest.mark.parametrize("response", [
    Response(f"{BB}/Public/Organization/644", status=403, bot_wall=True, content_type="text/html"),
    Response(f"{BB}/Public/Organization/644", status=500, content_type="text/html", body=b"error"),
])
def test_a_wall_or_an_error_is_a_portal_error_not_an_empty_listing(response):
    a = BoardBook(Fake({f"{BB}/Public/Organization/644": response}))
    with pytest.raises(base.PortalError):
        a.meetings("644", date(2026, 1, 1), date(2026, 12, 31))


def test_registered():
    assert "boardbook" in registry.names()
    assert ("boardbook", "644") in registry.detect(f"{BB}/Public/Organization/644", http=Fake({}))
