"""The CivicWeb adapter, offline, on trimmed copies of a real tenant's answers (cityofwasilla; fixtures under
tests/fixtures/portals/civicweb/: six of its meetings, its meeting types, one meeting's documents with the agenda cut
to its first entries, and Document Center folders reduced to their links). wasilla_documents_4860_minutes_removed.json
is meeting 4861's answer with its minutes taken out, to exercise the Document Center lookup."""

import json
from datetime import date
from pathlib import Path

import pytest

from ddwb.deep.web import Response
from ddwb.portals import base, civicweb, registry

FIX = Path(__file__).parent / "fixtures" / "portals" / "civicweb"
O = "https://cityofwasilla.civicweb.net"
SERVICE = f"{O}/Services/MeetingsService.svc/meetings"


class FakeHttp:
    def __init__(self, pages: dict):
        self.pages, self.asked = pages, []

    def get(self, url, use_cache=True):
        self.asked.append(url)
        return self.pages.get(url) or Response(url, final_url=url, status=404, content_type="text/html", body=b"")

    def post(self, url, data=None, json_body=None, headers=None, use_cache=True):
        return self.get(url)


def ok(url, name, ctype):
    return Response(url, final_url=url, status=200, content_type=ctype, body=(FIX / name).read_bytes())


def wasilla(**extra):
    pages = {
        f"{SERVICE}?from=1990-01-01&to=2100-12-31": ok(civicweb.meetings_url("cityofwasilla"), "wasilla_meetings.json", "application/json"),
        f"{O}/Portal/MeetingTypeList.aspx": ok(f"{O}/Portal/MeetingTypeList.aspx", "wasilla_meetingtypes.html", "text/html"),
        f"{SERVICE}/4861/meetingDocuments": ok(f"{SERVICE}/4861/meetingDocuments", "wasilla_documents_4861.json", "application/json"),
        f"{SERVICE}/4861/meetingData": ok(f"{SERVICE}/4861/meetingData", "wasilla_data_4861.json", "application/json"),
        f"{SERVICE}/4860/meetingDocuments": ok(f"{SERVICE}/4860/meetingDocuments", "wasilla_documents_4860_minutes_removed.json",
                                              "application/json"),
        f"{O}/filepro/documents/": ok(f"{O}/filepro/documents/", "wasilla_center_root.html", "text/html"),
        f"{O}/filepro/documents/3537/": ok(f"{O}/filepro/documents/3537/", "wasilla_center_3537_minutes.html", "text/html"),
        f"{O}/filepro/documents/3538/": ok(f"{O}/filepro/documents/3538/", "wasilla_center_3538_council_regular.html", "text/html"),
        f"{O}/filepro/documents/133912/": ok(f"{O}/filepro/documents/133912/", "wasilla_center_133912_2025.html", "text/html"),
    }
    pages.update(extra)
    return FakeHttp(pages)


def test_detect():
    a = civicweb.CivicWeb(FakeHttp({}))
    assert a.detect("https://cityofwasilla.civicweb.net/Portal/MeetingTypeList.aspx") == "cityofwasilla"
    assert a.detect("https://i.civicweb.net/Global/Images/icon.png") is None
    own = '<a href="https://nngov.civicweb.net/Portal/MeetingTypeList.aspx">Agendas and Minutes</a>' \
          '<img src="https://i.civicweb.net/Global/Images/icon.png">'
    assert a.detect("https://www.nnva.gov/", html=own) == "nngov"
    assert a.detect("https://www.example.gov/", html='<img src="https://i.civicweb.net/x.png">') is None
    assert registry.detect("https://largo.civicweb.net/filepro/documents/4280") == [("civicweb", "largo")]


def test_meetings_with_boards_from_types_and_the_dates_the_tenant_covers():
    listing = civicweb.CivicWeb(wasilla()).meetings("cityofwasilla", date(2025, 1, 1), date(2026, 12, 31))
    by = {m.id: m for m in listing.meetings}
    assert set(by) == {"4925", "4861", "4903", "4860", "5003"}
    council = by["4861"]
    assert (council.date, council.board, council.kind, council.governing) == (date(2025, 12, 15), "City Council", "regular", True)
    assert council.url == f"{O}/Portal/MeetingInformation.aspx?Org=Cal&Id=4861"
    assert (by["4903"].board, by["4903"].kind, by["4903"].governing) == ("Planning Commission", "committee", False)
    assert by["4925"].title.startswith("CANCELED")
    assert listing.per_year == {2025: 4, 2026: 1}
    assert "the portal lists 6 meetings from 2013-01-07 to 2026-12-29" in listing.notes


def test_a_tenant_that_stopped_listing_meetings_is_flagged():
    old = [m for m in json.loads((FIX / "wasilla_meetings.json").read_text()) if m["MeetingDate"] < "2014"]
    url = civicweb.meetings_url("cityofwasilla")
    http = wasilla(**{url: Response(url, final_url=url, status=200, content_type="application/json", body=json.dumps(old).encode())})
    listing = civicweb.CivicWeb(http).meetings("cityofwasilla", date(2012, 1, 1), date(2026, 12, 31))
    assert any("no meeting listed after 2013-01-07" in n and "moved" in n for n in listing.notes)


def test_documents_agenda_package_and_adopted_minutes():
    a = civicweb.CivicWeb(wasilla())
    m = next(x for x in a.meetings("cityofwasilla", date(2025, 12, 1), date(2025, 12, 31)).meetings if x.id == "4861")
    docs = a.documents("cityofwasilla", m)
    assert [(d.kind, d.format, d.url) for d in docs] == [
        ("agenda", "html", f"{O}/document/141063"), ("packet", "pdf", f"{O}/document/141062"), ("minutes", None, f"{O}/document/141858")]
    assert docs[2].title.endswith("(adopted)")
    assert not any("/filepro/" in u for u in a.http.asked)  # minutes attached: the Document Center is not opened


def test_minutes_found_in_the_document_center_when_none_is_attached():
    a = civicweb.CivicWeb(wasilla())
    m = next(x for x in a.meetings("cityofwasilla", date(2025, 11, 1), date(2025, 11, 30)).meetings if x.id == "4860")
    docs = a.documents("cityofwasilla", m)
    minutes = [d for d in docs if d.kind == "minutes"]
    assert [(d.url, d.title) for d in minutes] == [(f"{O}/document/141324", "City Council Regular - 24 Nov 2025 - Minutes - Adopted")]


def test_items_from_the_inline_agenda():
    a = civicweb.CivicWeb(wasilla())
    m = base.Meeting("4861", date(2025, 12, 15), "City Council Regular - 15 Dec 2025", "City Council")
    items = a.items("cityofwasilla", m)
    assert [(i.number, i.title) for i in items[:3]] == [("1", "CALL TO ORDER"), ("2", "ROLL CALL"), ("3", "PLEDGE OF ALLEGIANCE")]
    assert (items[6].number, items[6].section) == ("6.1", "6. COMMISSION AND AGENCY REPORTS")
    with_files = [i for i in items if i.attachments]
    assert with_files and all("/document/" in d.url and d.kind == "item" for i in with_files for d in i.attachments)


def test_sign_in_and_service_errors_are_errors():
    signin = civicweb.meetings_url("renton")
    http = FakeHttp({"https://renton.civicweb.net/Portal/MeetingTypeList.aspx": Response(
        "https://renton.civicweb.net/Portal/MeetingTypeList.aspx", final_url="https://renton.civicweb.net/user/signin/?url=%2fGlobal%2fError.aspx",
        status=200, content_type=None, body=b"")})
    with pytest.raises(base.PortalError, match="sign-in"):
        civicweb.CivicWeb(http).meetings("renton", date(2025, 1, 1), date(2025, 12, 31))
    bad = wasilla(**{civicweb.meetings_url("cityofwasilla"): Response(signin, status=400, content_type="text/html", body=b"Bad Request")})
    with pytest.raises(base.PortalError, match="HTTP 400"):
        civicweb.CivicWeb(bad).meetings("cityofwasilla", date(2025, 1, 1), date(2025, 12, 31))
    walled = wasilla(**{civicweb.meetings_url("cityofwasilla"): Response(signin, status=429, bot_wall=True)})
    with pytest.raises(base.PortalError, match="bot wall"):
        civicweb.CivicWeb(walled).meetings("cityofwasilla", date(2025, 1, 1), date(2025, 12, 31))
