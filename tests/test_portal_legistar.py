"""The Legistar adapter, offline, against trimmed answers of real clients: Guilford County, NC (the API and the web
calendar joined; minutes only on the calendar) and Racine, WI (the API refuses the client; the calendar in pages)."""

import json
from datetime import date
from pathlib import Path

import pytest

from ddwb.deep.web import Response
from ddwb.portals import civic_common, registry
from ddwb.portals.base import Meeting, PortalError
from ddwb.portals.legistar import API, Legistar, events_url

FIX = Path(__file__).parent / "fixtures" / "portals" / "legistar"
MARCH = (date(2025, 3, 1), date(2025, 3, 31))
YEAR_BOX = "ctl00$ContentPlaceHolder1$lstYears"


class FakeHttp:
    """GETs by address; calendar POSTs by (year chosen, pager target). Anything else is a 404."""

    def __init__(self, gets: dict[str, tuple[int, bytes]], posts: dict[tuple[str, str, str], tuple[int, bytes]] | None = None):
        self.gets, self.posts, self.asked = gets, posts or {}, []

    def _resp(self, url, status, body, kind):
        return Response(url, final_url=url, status=status, content_type=kind, body=body, bot_wall=status in (403, 429))

    def get(self, url, use_cache=True):
        self.asked.append(("GET", url))
        status, body = self.gets.get(url, (404, b"not found"))
        return self._resp(url, status, body, "application/json" if "webapi" in url else "text/html")

    def post(self, url, data=None, json_body=None, headers=None, use_cache=True):
        key = (url, (data or {}).get(YEAR_BOX, ""), (data or {}).get("__EVENTTARGET", ""))
        self.asked.append(("POST",) + key)
        status, body = self.posts.get(key, (404, b"not found"))
        return self._resp(url, status, body, "text/html")


def guilford() -> FakeHttp:
    cal = "https://guilford.legistar.com/Calendar.aspx"
    return FakeHttp(
        {events_url("guilford", *MARCH): (200, (FIX / "guilford_events_2025-03.json").read_bytes()),
         f"{API}/guilford/bodies": (200, (FIX / "guilford_bodies.json").read_bytes()),
         f"{API}/guilford/events/1498/eventitems?AgendaNote=1&MinutesNote=1&Attachments=1":
             (200, (FIX / "guilford_eventitems_1498.json").read_bytes()),
         cal: (200, (FIX / "guilford_calendar.html").read_bytes())},
        {(cal, "2025", ""): (200, (FIX / "guilford_calendar_2025.html").read_bytes())})


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(civic_common, "BACKOFF", (0.0, 0.0))


def test_detect_from_site_api_and_file_addresses_and_from_an_own_site_page():
    a = Legistar(FakeHttp({}))
    assert a.detect("https://guilford.legistar.com/Calendar.aspx") == "guilford"
    assert a.detect("https://webapi.legistar.com/v1/guilford/events?$top=5") == "guilford"
    assert a.detect("https://guilford.legistar1.com/guilford/meetings/2025/3/1498_A_Board_of_Commissioners.pdf") == "guilford"
    assert a.detect("https://legistar2.granicus.com/sanjose/meetings/2025/6/7001_A_City_Council.pdf") == "sanjose"
    assert a.detect("https://www.legistar.com/") is None
    page = ('<li><a href="https://www.guilfordcountync.gov/">Home</a></li>'
            '<iframe title="Meetings" src="https://guilford.legistar.com/Calendar.aspx"></iframe>')
    assert a.detect("https://www.guilfordcountync.gov/government/board-of-commissioners", html=page) == "guilford"
    assert a.detect("https://www.guilfordcountync.gov/", html="<p>Agendas at the clerk's office</p>") is None
    assert ("legistar", "guilford") in registry.detect("https://guilford.legistar.com/Calendar.aspx")


def test_meetings_join_the_api_and_the_calendar_and_mark_the_board():
    http = guilford()
    listing = Legistar(http).meetings("guilford", *MARCH)
    assert [m.id for m in listing.meetings] == ["1498", "1528", "1529", "1530", "1531"]  # every one on both sides
    assert not listing.notes
    assert ("POST", "https://guilford.legistar.com/Calendar.aspx", "2025", "") in http.asked
    board = next(m for m in listing.meetings if m.id == "1498")
    assert board.governing is True and board.kind == "regular" and board.board == "Board of Commissioners"
    work = next(m for m in listing.meetings if m.id == "1528")
    assert work.governing is True and work.kind == "work_session"
    retreat = next(m for m in listing.meetings if m.id == "1530")
    assert retreat.kind == "work_session" and "http" not in retreat.title  # a dial-in note is not a title


def test_documents_find_the_minutes_the_api_leaves_out():
    a = Legistar(guilford())
    meeting = next(m for m in a.meetings("guilford", *MARCH).meetings if m.id == "1498")
    events = json.loads((FIX / "guilford_events_2025-03.json").read_text())
    assert next(e for e in events if e["EventId"] == 1498)["EventMinutesFile"] is None  # the API has no minutes
    docs = a.documents("guilford", meeting)
    kinds = [d.kind for d in docs]
    assert kinds.count("agenda") == 1 and docs[0].url.endswith(".pdf")  # the API's agenda file, not the same file twice
    minutes = next(d for d in docs if d.kind == "minutes")
    assert minutes.url == "https://guilford.legistar.com/View.ashx?M=M&ID=1264439&GUID=58F20083-1780-4703-ACED-6683AF1AB9B8"
    assert any(d.kind == "video" and "Video.aspx" in d.url for d in docs)


def test_items_in_printed_order_with_sections_attachments_and_matter():
    a = Legistar(guilford())
    items = a.items("guilford", Meeting("1498", date(2025, 3, 6), "Board of Commissioners"))
    assert [i.number for i in items[:3]] == ["I.", "II.", "III."]
    with_files = [i for i in items if i.attachments]
    assert with_files and all(d.kind == "item" and d.url.startswith("http") for i in with_files for d in i.attachments)
    assert all(i.section for i in with_files)  # a matter sits under the heading item before it
    assert any(i.text and i.text.startswith("File ") for i in with_files)


def test_nothing_answering_is_an_error_not_an_empty_listing():
    walls = FakeHttp({events_url("guilford", *MARCH): (429, b""),
                      "https://guilford.legistar.com/Calendar.aspx": (403, b"Forbidden")})
    with pytest.raises(PortalError, match="neither the API nor the web calendar"):
        Legistar(walls).meetings("guilford", *MARCH)


RACINE_ERROR = (b"'Agenda Draft Status' or 'Agenda Status Not Vievable By The Public' is not setup in settings. "
                b"Value should be greater than 0.")
PAGER = "ctl00$ContentPlaceHolder1$gridCalendar$ctl00$ctl02$ctl00$ctl0"


def racine() -> FakeHttp:
    cal = "https://cityofracine.legistar.com/Calendar.aspx"
    year = (date(2025, 1, 1), date(2025, 12, 31))
    return FakeHttp(
        {events_url("cityofracine", *year): (400, RACINE_ERROR),
         cal: (200, (FIX / "racine_calendar.html").read_bytes()),
         "https://cityofracine.legistar.com/MeetingDetail.aspx?ID=1252993&GUID=F317A26C-3A6A-486A-9A6A-332BBC5BB5AD&Options=info|&Search=":
             (200, (FIX / "racine_meetingdetail_1252993.html").read_bytes())},
        {(cal, "2025", ""): (200, (FIX / "racine_calendar_2025_p1.html").read_bytes()),
         (cal, "2025", PAGER + "4"): (200, (FIX / "racine_calendar_2025_p2.html").read_bytes()),
         (cal, "2025", PAGER + "6"): (200, (FIX / "racine_calendar_2025_p3.html").read_bytes())})


def test_a_client_the_api_refuses_is_read_from_every_page_of_the_calendar():
    http = racine()
    listing = Legistar(http).meetings("cityofracine", date(2025, 1, 1), date(2025, 12, 31))
    assert len(listing.meetings) == 6  # two rows on each of the three pages (the fixture's count is cut to match)
    assert [k[3] for k in http.asked if k[0] == "POST"] == ["", PAGER + "4", PAGER + "6"]
    assert any("HTTP 400" in n and "not setup in settings" in n for n in listing.notes)
    assert any("from the web calendar" in n for n in listing.notes)
    council = next(m for m in listing.meetings if m.id == "web-1252993")
    assert council.governing is True and council.board == "Common Council" and council.date == date(2025, 3, 4)


def test_a_calendar_only_meeting_gives_documents_and_items_from_its_pages():
    a = Legistar(racine())
    council = next(m for m in a.meetings("cityofracine", date(2025, 1, 1), date(2025, 12, 31)).meetings if m.id == "web-1252993")
    docs = {d.kind: d for d in a.documents("cityofracine", council)}
    assert docs["minutes"].url == "https://cityofracine.legistar.com/View.ashx?M=M&ID=1252993&GUID=F317A26C-3A6A-486A-9A6A-332BBC5BB5AD"
    assert docs["agenda"].format == "pdf"
    fresh = Legistar(racine())  # a later run with only the meeting: its page gives the same documents
    assert {d.url for d in fresh.documents("cityofracine", council)} >= {d.url for d in docs.values()}
    items = a.items("cityofracine", council)
    assert len(items) == 4 and all(i.text and i.text.startswith("File ") for i in items)
    assert any(i.outcome for i in items)


def test_a_late_page_failure_is_a_note_with_the_count():
    http = racine()
    http.posts[("https://cityofracine.legistar.com/Calendar.aspx", "2025", PAGER + "6")] = (500, b"Server Error")
    listing = Legistar(http).meetings("cityofracine", date(2025, 1, 1), date(2025, 12, 31))
    assert len(listing.meetings) == 4
    assert any("rows after 4 not read: HTTP 500" in n for n in listing.notes)
