"""The CivicClerk adapter, offline, against trimmed answers of a real tenant's service (Ukiah, CA)."""

from datetime import date
from pathlib import Path

import pytest

from ddwb.deep.web import Response
from ddwb.portals import civic_common, registry
from ddwb.portals.base import Meeting, PortalError
from ddwb.portals.civicclerk import CivicClerk, events_url

FIX = Path(__file__).parent / "fixtures" / "portals" / "civicclerk"
API = "https://ukiahca.api.civicclerk.com"


class FakeHttp:
    """Answers from a dict of URL -> (status, body); anything else is a 404. Records what was asked."""

    def __init__(self, pages: dict[str, tuple[int, bytes]]):
        self.pages, self.asked = pages, []

    def get(self, url, use_cache=True):
        self.asked.append(url)
        status, body = self.pages.get(url, (404, b"not found"))
        return Response(url, final_url=url, status=status, content_type="application/json", body=body,
                        bot_wall=status in (403, 429))

    def post(self, url, data=None, json_body=None, headers=None, use_cache=True):
        raise AssertionError("CivicClerk needs no POST")


def _pages():
    return {
        events_url("ukiahca", date(2025, 1, 1), date(2025, 1, 31), 0): (200, (FIX / "events_2025-01_skip0.json").read_bytes()),
        events_url("ukiahca", date(2025, 1, 1), date(2025, 1, 31), 15): (200, (FIX / "events_2025-01_skip15.json").read_bytes()),
        f"{API}/v1/Events/1965": (200, (FIX / "event_1965.json").read_bytes()),
        f"{API}/v1/Meetings/1336": (200, (FIX / "meeting_1336.json").read_bytes()),
    }


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(civic_common, "BACKOFF", (0.0, 0.0))


def test_detect_from_portal_api_and_legacy_urls_and_from_an_own_site_page():
    a = CivicClerk(FakeHttp({}))
    assert a.detect("https://ukiahca.portal.civicclerk.com/event/1965/files") == "ukiahca"
    assert a.detect("https://ukiahca.api.civicclerk.com/v1/Events/1965") == "ukiahca"
    assert a.detect("https://redmondor.civicclerk.com/") == "redmondor"
    assert a.detect("https://www.civicclerk.com/") is None
    page = '<p>Agendas: <iframe src="https://ukiahca.portal.civicclerk.com/?embed=1"></iframe> <a href="https://www.civicclerk.com">CivicClerk</a></p>'
    assert a.detect("https://www.cityofukiah.com/meetings/", html=page) == "ukiahca"
    assert a.detect("https://www.cityofukiah.com/", html="<p>no portal here</p>") is None
    assert ("civicclerk", "ukiahca") in registry.detect("https://ukiahca.portal.civicclerk.com/")


def test_meetings_page_through_the_list_and_mark_the_council():
    http = FakeHttp(_pages())
    listing = CivicClerk(http).meetings("ukiahca", date(2025, 1, 1), date(2025, 1, 31))
    assert len(listing.meetings) == 16 and listing.per_year == {2025: 16}  # 15 on the first page, 1 on the second
    assert not listing.notes
    assert sum(u.endswith("skip=15,sortDescending=false,cursorId=0)") for u in http.asked) == 1
    council = [m for m in listing.meetings if m.board == "City Council"]
    assert council and all(m.governing for m in council)
    authority = next(m for m in listing.meetings if "Water Authority" in m.board)
    assert authority.governing is False and authority.kind == "committee"
    assert council[0].url == f"https://ukiahca.portal.civicclerk.com/event/{council[0].id}"


def test_documents_give_the_file_streams_with_minutes():
    a = CivicClerk(FakeHttp(_pages()))
    meeting = Meeting("1965", date(2025, 3, 5), "City Council Regular Meeting", board="City Council")
    docs = a.documents("ukiahca", meeting)  # not in the listing: read from /v1/Events/<id>
    kinds = {d.kind: d for d in docs}
    assert set(kinds) >= {"agenda", "packet", "minutes"}
    assert kinds["minutes"].url == f"{API}/v1/Meetings/GetMeetingFileStream(fileId=4971,plainText=false)"
    assert kinds["video"].url.endswith("/event/1965/media")


def test_items_follow_the_outline_with_sections_and_attachments():
    a = CivicClerk(FakeHttp(_pages()))
    items = a.items("ukiahca", Meeting("1965", date(2025, 3, 5), "City Council Regular Meeting"))
    numbers = [i.number for i in items]
    assert numbers[:3] == ["1", "2", "3"] and "8.a" in numbers
    consent = next(i for i in items if i.number == "8.a")
    assert consent.section == "8. CONSENT CALENDAR"
    assert all(d.url.startswith(f"{API}/v1/Meetings/GetAttachmentFile(fileId=") for i in items for d in i.attachments)
    assert sum(len(i.attachments) for i in items) > 10


def test_a_refusal_is_an_error_and_a_late_failure_a_note():
    with pytest.raises(PortalError, match="HTTP 429|bot wall"):
        CivicClerk(FakeHttp({events_url("ukiahca", date(2025, 1, 1), date(2025, 1, 31), 0): (429, b"")})).meetings(
            "ukiahca", date(2025, 1, 1), date(2025, 1, 31))
    pages = _pages()
    pages[events_url("ukiahca", date(2025, 1, 1), date(2025, 1, 31), 15)] = (503, b"busy")
    listing = CivicClerk(FakeHttp(pages)).meetings("ukiahca", date(2025, 1, 1), date(2025, 1, 31))
    assert len(listing.meetings) == 15 and any("HTTP 503" in n for n in listing.notes)
