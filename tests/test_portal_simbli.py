"""The Simbli adapter, offline: saved (trimmed) pages and service answers from Covina-Valley Unified's listing, and a
fake browser in the browser tier's shape. Nothing here reaches the network or starts a browser."""

from datetime import date
from pathlib import Path

import pytest

from ddwb.deep.web import Response
from ddwb.portals import base, registry, simbli
from ddwb.portals.browser import Challenge, Visit

FIX = Path(__file__).parent / "fixtures" / "portals" / "simbli"
VIEWER = ("https://app1.eboardsolutions.com/api/CommonPrint/ViewFile?fid=Minutes_36030744_76523_05Nov2026050100.pdf"
          "&fname=Board%20of%20Education%20Meeting&pdfurl=https%3A%2F%2Fapp1.eboardsolutions.com%2Fapi%2FPrintMinutes%2F"
          "ViewPrintMinutes%3Ffid%3DMinutes_36030744_76523_05Nov2026050100.pdf%26fname%3DBoard%2520of%2520Education%2520Meeting")


def fixture(name: str) -> bytes:
    return (FIX / name).read_bytes()


def ok(url: str, body: bytes) -> Response:
    return Response(url, final_url=url, status=200, content_type="application/json; charset=utf-8", body=body)


class FakeBrowser:
    """open() a page; get/post answered as the district's service would; `challenge` stops at the first page."""

    def __init__(self, challenge: bool = False, data_wall: bool = False) -> None:
        self.challenge, self.data_wall = challenge, data_wall
        self.opened, self.calls, self.closed, self.pages = [], [], False, 0

    def open(self, url, ready=None, ready_ms=15_000, until_url=None, side=False):
        self.opened.append((url, side))
        if self.challenge:
            raise Challenge(f"{url}: a challenge frame; stopped there, not solved")
        if "SB_MeetingListing" in url:
            return Visit(url, url, 200, "Meetings Listing", fixture("listing.html").decode())
        if "PrintMinutesSettingsAnonymous" in url:
            return Visit(url, VIEWER, 200, "Board of Education Meeting - Meeting Minutes", "<html></html>")
        if url.endswith("/csba/covinavalley"):
            return Visit(url, "https://simbli.eboardsolutions.com/Index.aspx?S=36030744", 200, "Home", "<html></html>")
        raise AssertionError(f"unexpected page {url}")

    def evaluate(self, script, arg=None):
        return {"body": {"RecordStart": arg[0], "RecordCount": arg[1]}, "token": ""}

    def post(self, url, data=None, json_body=None, headers=None, use_cache=True):
        self.calls.append(("POST", url, json_body))
        self.pages += 1
        return ok(url, fixture("listing_more.json") if self.pages == 1 else b"[]")

    def get(self, url, use_cache=True):
        self.calls.append(("GET", url, None))
        if self.data_wall:
            return Response(url, final_url=url, status=200, content_type="text/html", body=fixture("challenge.html"))
        if "GetItemsTreeDTO" in url:
            return ok(url, fixture("tree.json"))
        if "GetSupportingDocuments" in url:
            return ok(url, fixture("supporting.json"))
        raise AssertionError(f"unexpected call {url}")

    def close(self):
        self.closed = True


def test_detect_from_simbli_addresses_and_a_districts_own_page():
    a = simbli.Simbli(FakeBrowser())
    assert a.detect("https://simbli.eboardsolutions.com/SB_Meetings/SB_MeetingListing.aspx?S=36030744") == "36030744"
    assert a.detect("https://simbli.eboardsolutions.com/Policy/ViewPolicy.aspx?S=36030345&revid=lnZz") == "36030345"
    assert a.detect("https://simbli.eboardsolutions.com/policy/policylisting.aspx?s=36031056") == "36031056"
    assert a.detect("https://assistive.eboardsolutions.com/Meetings/ViewMeeting.aspx?MTID=4&S=112002") == "112002"
    wrapped = ("https://nam12.safelinks.protection.outlook.com/?url=https%3A%2F%2Fsimbli.eboardsolutions.com%2F"
               "SB_Meetings%2FSB_MeetingListing.aspx%3FS%3D170010&data=x")
    assert a.detect(wrapped) == "170010"
    assert a.detect("https://simbli.eboardsolutions.com/csba/bassettusd") == "csba/bassettusd"
    assert a.detect("https://www.cvusd.k12.ca.us/board") is None
    assert a.detect("https://www.cvusd.k12.ca.us/board", html=fixture("own_site.html").decode()) == "36030744"
    assert registry.detect("https://simbli.eboardsolutions.com/Index.aspx?S=36030664") == [("simbli", "36030664")]


def test_the_listing_page_carries_its_rows_and_session():
    rows, token, district = simbli.parse_listing_page(fixture("listing.html").decode())
    assert len(rows) == 5 and (token, district) == ("PAGE-SESSION", "DISTRICT-ID")
    assert rows[1]["Master_MeetingID"] == 76523 and rows[1]["MinutesStatus"] == "PUBLISHED"
    assert simbli.parse_listing_page(fixture("challenge.html").decode()) == ([], None, None)


def test_meetings_page_back_through_older_rows_and_mark_whose_meeting_it_is():
    browser = FakeBrowser()
    listing = simbli.Simbli(browser).meetings("36030744", date(2024, 1, 1), date(2026, 12, 31))
    assert listing.per_year == {2024: 3, 2025: 1, 2026: 3} and listing.notes == []
    posted = [c for c in browser.calls if c[0] == "POST"]
    assert posted[0][2] == {"RecordStart": 6, "RecordCount": 50}  # the next page after the grid's own rows
    byid = {m.id: m for m in listing.meetings}
    assert "7508" not in byid  # 2021: outside the window
    regular, committee, special = byid["76523"], byid["48954"], byid["43826"]
    assert (regular.kind, regular.governing, regular.board) == ("regular", True, "Board Meeting")
    assert (committee.kind, committee.governing) == ("committee", False)
    assert (special.kind, special.governing) == ("special", True)
    assert regular.url == "https://simbli.eboardsolutions.com/SB_Meetings/ViewMeeting.aspx?S=36030744&MID=76523"


def test_a_short_link_is_followed_to_the_district_number():
    browser = FakeBrowser()
    listing = simbli.Simbli(browser).meetings("csba/covinavalley", date(2026, 1, 1), date(2026, 12, 31))
    assert browser.opened[0] == ("https://simbli.eboardsolutions.com/csba/covinavalley", True)
    assert all("S=36030744" in m.url for m in listing.meetings) and listing.per_year == {2026: 3}


def test_documents_make_the_minutes_pdf_and_keep_minutes_filed_here_as_the_items_files():
    browser = FakeBrowser()
    a = simbli.Simbli(browser)
    meeting = base.Meeting("76523", date(2026, 9, 10), "Board of Education Meeting")
    docs = a.documents("36030744", meeting)  # not listed by this adapter yet: it reads the listing first
    assert [d.kind for d in docs] == ["agenda", "minutes", "item"]
    agenda, minutes, filed = docs
    assert agenda.url.endswith("ViewMeeting.aspx?S=36030744&MID=76523") and agenda.format == "html"
    assert minutes.url.startswith("https://app1.eboardsolutions.com/api/PrintMinutes/ViewPrintMinutes?fid=Minutes_36030744_76523_")
    assert minutes.format == "pdf" and minutes.meeting_id == "76523"
    assert ("https://simbli.eboardsolutions.com/SB_Meetings/PrintMinutesSettingsAnonymous.aspx?S=36030744"
            "&enmid=UAhZbK9kHacLwRLs9Cz3nw%3D%3D", True) in browser.opened  # a side tab, as "Print Minutes" opens
    # the minutes filed on this agenda are the 13 August meeting's, approved here
    assert filed.url == "https://simbli.eboardsolutions.com/Meetings/Attachment.aspx?S=36030744&AID=1701840&MID=76523"
    assert (filed.title, filed.item_no, filed.format) == ("8.13.26 - Meeting Minutes", "XV.A", "pdf")
    unpublished = a.documents("36030744", base.Meeting("76524", date(2026, 10, 8), "Board of Education Meeting"))
    assert "app1" not in " ".join(d.url for d in unpublished)  # minutes not published: none made


def test_unpublished_minutes_are_found_filed_on_the_next_meetings_agenda():
    a = simbli.Simbli(FakeBrowser())
    a.meetings("36030744", date(2024, 1, 1), date(2026, 12, 31))
    a._rows[("36030744", "76000")] = {"Master_MeetingID": 76000, "MM_DateTime": "2026-08-13T18:00:00",
                                      "MM_MeetingTitle": "Board of Education Meeting", "ML_TypeTitle": "Board Meeting",
                                      "MinutesStatus": "UNPUBLISHED", "EncrMeetingId": "AUG13=="}
    august = base.Meeting("76000", date(2026, 8, 13), "Board of Education Meeting")
    assert [m.id for m in a.following("36030744", august)] == ["76523", "76524"]
    minutes = [d for d in a.documents("36030744", august) if d.kind == "minutes"]
    assert len(minutes) == 1 and minutes[0].meeting_id == "76000"
    assert minutes[0].url == "https://simbli.eboardsolutions.com/Meetings/Attachment.aspx?S=36030744&AID=1701840&MID=76523"
    assert "filed with item XV.A of the 2026-09-10 meeting" in minutes[0].title


def test_items_number_by_bullet_path_with_each_items_files():
    a = simbli.Simbli(FakeBrowser())
    items = a.items("36030744", base.Meeting("76523", date(2026, 9, 10), "Board of Education Meeting"))
    numbers = [i.number for i in items]
    assert numbers == ["I", "V", "V.A", "V.A.i", "V.B", "XV", "XV.A", "XV.B"]
    assert items[0].title == "Meeting Called to Order" and items[0].section is None
    assert items[3].title == "Case No. 2026060752 (OAH)" and items[3].section == "V. Closed Session"
    minutes_item = items[6]
    assert minutes_item.title.startswith("Approval of Minutes") and minutes_item.attachments[0].kind == "minutes"
    assert items[1].attachments == []  # no files flagged: no request made for them
    assert len([c for c in a.browser.calls if "GetSupportingDocuments" in c[1]]) == 2  # one per item with files


def test_a_challenge_stops_with_a_portal_error_never_an_empty_answer():
    with pytest.raises(base.PortalError, match="not solved"):
        simbli.Simbli(FakeBrowser(challenge=True)).meetings("36030744", date(2025, 1, 1), date(2026, 12, 31))
    a = simbli.Simbli(FakeBrowser(data_wall=True))
    with pytest.raises(base.PortalError, match="stopped"):
        a.items("36030744", base.Meeting("76523", date(2026, 9, 10), "Board of Education Meeting"))


def test_small_parsers():
    assert simbli.minutes_pdf_url(VIEWER).startswith("https://app1.eboardsolutions.com/api/PrintMinutes/ViewPrintMinutes?fid=")
    assert simbli.minutes_pdf_url("https://simbli.eboardsolutions.com/SB_Meetings/PrintMinutesSettingsAnonymous.aspx?S=1") is None
    assert simbli._true("True") and not simbli._true("False") and simbli._true(True) and not simbli._true(None)
    assert simbli.meeting_kind("Board of Education Meeting", "Board Meeting") == "regular"
    assert simbli.governing("Board Meeting", "Board of Education Meeting") is True
    tree = {"Items": [{"Title": "Dr. Lisa Simon, Superintendent", "bulletLabel": "", "SeqUnderParent": "1"},
                      {"Title": "2. Old item", "bulletLabel": "2", "SeqUnderParent": "2", "Deleted": "True"}]}
    parsed = simbli.parse_items_tree(tree, "1")
    assert [(i.number, i.title) for i, _ in parsed] == [(None, "Dr. Lisa Simon, Superintendent")]
