"""The BoardOnTrack adapter, offline, on trimmed pages saved from Vertus Charter School NY (0wwusX) and Golden Charter
Academy CA (K8WqfA) on 2026-11-05."""

from datetime import date
from pathlib import Path

import pytest

from ddwb.deep.web import Response
from ddwb.portals import base, registry
from ddwb.portals.boardontrack import BoardOnTrack, slug_of

FIX = Path(__file__).parent / "fixtures" / "portals" / "boardontrack"
B = "https://app2.boardontrack.com"
EMPTY = "<html><body><h1>Board Meetings</h1></body></html>"


class Fake:
    def __init__(self, pages):
        self.pages, self.asked = pages, []

    def get(self, url, use_cache=True):
        self.asked.append(url)
        page = self.pages.get(url)
        if page is None:
            raise AssertionError(f"unexpected request {url}")
        if isinstance(page, Response):
            return page
        body = (FIX / page).read_bytes() if page.endswith(".html") else page.encode()
        return Response(url, final_url=url, status=200, content_type="text/html;charset=UTF-8", body=body)

    def post(self, *a, **k):
        raise AssertionError("BoardOnTrack needs no POST")


def vertus(client="0wwusX"):
    return Fake({
        f"{B}/public/{client}/year?et=b": "year_vertus_2026_b.html",
        f"{B}/public/0wwusX/year?syid=2026-07&et=b": "year_vertus_2026_b.html",
        f"{B}/public/0wwusX/year?syid=2026-07&et=c": "year_vertus_2026_c.html",
        f"{B}/public/0wwusX/year?syid=2025-07&et=b": "year_vertus_2025_b.html",
        f"{B}/public/0wwusX/year?syid=2025-07&et=c": EMPTY,
        f"{B}/public/0wwusX/year?syid=2024-07&et=b": EMPTY,
        f"{B}/public/0wwusX/year?syid=2024-07&et=c": EMPTY,
        f"{B}/public/0wwusX/year?syid=2023-07&et=b": EMPTY,
        f"{B}/public/0wwusX/year?syid=2023-07&et=c": EMPTY,
        f"{B}/public/0wwusX/agenda/120791": "agenda_vertus.html",
        f"{B}/public/0wwusX/minutes/120792": "minutes_vertus.html",
        f"{B}/public/0wwusX/meeting/99027": "meeting_vertus.html",
    })


def test_detect_from_urls_and_a_bodys_page():
    a = BoardOnTrack(Fake({}))
    assert a.detect(f"{B}/public/K8WqfA/year?syid=2026-07") == "K8WqfA"
    assert a.detect(f"{B}/public/zF0vk3/meeting/88411") == "zF0vk3"
    assert a.detect(f"{B}/org/qQOXq0/publicPortal/meetingsList") == "qQOXq0"
    assert a.detect(f"{B}/public/year") is None and slug_of("https://www.vertusschool.org/board") is None
    page = '<li><a href="https://app2.boardontrack.com/public/0wwusX/home">Board meetings</a></li>'
    assert a.detect("https://www.vertusschool.org/board", page) == "0wwusX"


def test_meetings_read_board_and_committee_views_back_to_the_window():
    a = BoardOnTrack(vertus("0wwusx"))  # the registry lower-cases slugs; the portal answers either
    listing = a.meetings("0wwusx", date(2024, 1, 1), date(2027, 6, 30))
    by_id = {m.id: m for m in listing.meetings}
    assert set(by_id) == {"92218", "92219", "99027", "94782", "83004", "83003"}
    assert by_id["92218"].kind == "regular" and by_id["92218"].governing and by_id["92218"].board == "Vertus High School"
    assert by_id["99027"].kind == "committee" and by_id["99027"].governing is False
    assert by_id["92218"].url == f"{B}/public/0wwusX/meeting/92218"
    assert listing.per_year == {2026: 6}
    assert any("stopped at school year 2023-07" in n for n in listing.notes)


def test_documents_are_the_pdfs_by_meeting_id():
    a = BoardOnTrack(vertus("0wwusx"))
    meetings = {m.id: m for m in a.meetings("0wwusx", date(2024, 1, 1), date(2027, 6, 30)).meetings}
    docs = a.documents("0wwusx", meetings["92218"])
    assert [(d.kind, d.url, d.format) for d in docs] == [
        ("agenda", f"{B}/org/0wwusX/agenda/downloadAgendaPdf/92218", "pdf"),
        ("minutes", f"{B}/org/0wwusX/minutes/downloadPdf/92218", "pdf")]
    assert [d.kind for d in a.documents("0wwusx", meetings["92219"])] == ["agenda"]  # minutes not posted yet


def test_documents_for_a_meeting_not_listed_this_session_read_its_page():
    a = BoardOnTrack(vertus())
    docs = a.documents("0wwusX", base.Meeting("99027", date(2026, 9, 30), "Accountability Committee Committee Meeting"))
    assert {d.kind for d in docs} == {"agenda", "minutes"}


def test_items_with_sections_and_what_the_minutes_record():
    a = BoardOnTrack(vertus())
    meeting = base.Meeting("99027", date(2026, 9, 30), "Accountability Committee Committee Meeting", kind="committee")
    items = a.items("0wwusX", meeting)
    assert [i.number for i in items] == ["I.A", "I.B", "I.C", "II.A", "II.B", "IV.A"]
    assert items[3].section == "II. Accountability Committee" and items[3].title == "Academic Year in Review"
    assert items[1].outcome and "called a meeting" in items[1].outcome
    assert items[-1].outcome and "adjourned" in items[-1].outcome  # "IV." on the agenda is "III." in the minutes


def test_item_descriptions_become_text():
    a = BoardOnTrack(Fake({f"{B}/public/K8WqfA/meeting/99096": '<a href="/public/K8WqfA/agenda/120939">Agenda</a>',
                           f"{B}/public/K8WqfA/agenda/120939": "agenda_golden.html"}))
    items = a.items("K8WqfA", base.Meeting("99096", date(2026, 10, 22), "Golden Charter Academy"))
    roll = next(i for i in items if i.title == "Roll Call")
    assert roll.number == "I.B" and roll.text and "Board of Trustees Members" in roll.text


@pytest.mark.parametrize("response", [
    Response(f"{B}/public/0wwusX/year?et=b", status=429, bot_wall=True),
    Response(f"{B}/public/0wwusX/year?et=b", status=502, body=b"bad gateway"),
])
def test_a_wall_or_an_error_is_a_portal_error(response):
    with pytest.raises(base.PortalError):
        BoardOnTrack(Fake({f"{B}/public/0wwusX/year?et=b": response})).meetings("0wwusX", date(2026, 1, 1), date(2026, 12, 31))


def test_a_changed_page_is_a_portal_error_not_an_empty_listing():
    with pytest.raises(base.PortalError):
        BoardOnTrack(Fake({f"{B}/public/0wwusX/year?et=b": EMPTY})).meetings("0wwusX", date(2026, 1, 1), date(2026, 12, 31))


def test_registered():
    assert ("boardontrack", "K8WqfA") in registry.detect(f"{B}/public/K8WqfA/home", http=Fake({}))
