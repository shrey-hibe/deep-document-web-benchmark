"""The AgendaOnline adapter, offline, on trimmed pages saved from San Jacinto USD and Chico USD on 2026-11-05 (their
__VIEWSTATE cut to a placeholder)."""

from datetime import date
from pathlib import Path

import pytest

from ddwb.deep.web import Response
from ddwb.portals import base, registry
from ddwb.portals.agendaonline import AgendaOnline, client_of

FIX = Path(__file__).parent / "fixtures" / "portals" / "agendaonline"
SJ = "https://sanjacinto.agendaonline.net/public/"
MEETING = "Meeting.aspx?AgencyID=122&MeetingID=26448&AgencyTypeID=1"


def page(url, name, status=200, final=None):
    return Response(url, final_url=final or url, status=status, content_type="text/html; charset=utf-8",
                    body=(FIX / name).read_bytes() if name.endswith(".html") else name.encode())


class Fake:
    """GETs from saved pages; a POST answered by `opens` (the grid target -> the response)."""

    def __init__(self, pages, opens=None):
        self.pages, self.opens, self.posts = pages, opens or {}, []

    def get(self, url, use_cache=True):
        if url not in self.pages:
            raise AssertionError(f"unexpected request {url}")
        return self.pages[url]

    def post(self, url, data=None, json_body=None, headers=None, use_cache=True):
        self.posts.append((url, data["__EVENTTARGET"], data["__EVENTARGUMENT"], use_cache))
        assert data["__VIEWSTATE"] == "VIEWSTATE-TRIMMED"
        return self.opens[data["__EVENTTARGET"]]


def test_detect():
    a = AgendaOnline(Fake({}))
    assert a.detect("https://agendaonline.net/public/chicounified") == "chicounified"
    assert a.detect("https://www.agendaonline.net/public/washington") == "washington"
    assert a.detect(SJ) == "sanjacinto.agendaonline.net"
    assert a.detect(f"{SJ}{MEETING}&IsArchived=True") == "sanjacinto.agendaonline.net"
    assert a.detect("https://agendaonline.net/public/Meeting.aspx?AgencyID=1306") is None  # no tenant in it
    assert a.detect("https://tampagov.hylandcloud.com/221agendaonline/") is None  # OnBase's Agenda Online: not this one
    assert client_of("https://agendaonline.net/") is None
    own = '<a href="https://agendaonline.net/public/desertsands" target="_blank">Board Agendas (archive)</a>'
    assert a.detect("https://www.dsusd.us/board", own) == "desertsands"


def test_meetings_need_no_post_and_say_the_archive_ended():
    a = AgendaOnline(Fake({SJ: page(SJ, "listing_sanjacinto.html")}))
    listing = a.meetings("sanjacinto.agendaonline.net", date(2021, 1, 1), date(2021, 12, 31))
    assert [m.id for m in listing.meetings] == ["row-3", "row-2", "row-1", "row-0"]  # by date
    first = listing.meetings[-1]
    assert first.date == date(2021, 6, 15) and first.kind == "regular" and first.board == "San Jacinto USD"
    assert any(m.kind == "special" for m in listing.meetings)
    recent = a.meetings("sanjacinto.agendaonline.net", date(2024, 1, 1), date(2026, 12, 31))
    assert recent.meetings == [] and any("archive" in n and "2021-06-15" in n for n in recent.notes)


def test_documents_open_the_meeting_by_post_and_give_its_page_and_attachments():
    opened = page(f"{SJ}agency.aspx", "meeting_sanjacinto.html", final=f"{SJ}{MEETING}&IsArchived=True")
    fake = Fake({SJ: page(SJ, "listing_sanjacinto.html")}, {"ctl00$cphMain$AgencyPublic2$gviMeetings": opened})
    a = AgendaOnline(fake)
    meeting = base.Meeting("row-0", date(2021, 6, 15), "Regular School Board Meeting")
    docs = a.documents("sanjacinto.agendaonline.net", meeting)
    assert docs[0].kind == "minutes" and docs[0].format == "html" and docs[0].url == f"{SJ}{MEETING}&IsArchived=True"
    attachment = next(d for d in docs if d.kind == "item")
    assert attachment.url == f"{SJ}Meeting/Attachments/DisplayAttachment.aspx?AttachmentID=557067&IsArchive=1"
    assert attachment.item_no == "4.1" and attachment.format == "pdf"
    assert fake.posts == [(f"{SJ}agency.aspx", "ctl00$cphMain$AgencyPublic2$gviMeetings", "Select$0", False)]
    a.items("sanjacinto.agendaonline.net", meeting)
    assert len(fake.posts) == 1  # the opened page is kept: no second post


def test_items_carry_abstracts_motions_and_minutes():
    opened = page(f"{SJ}agency.aspx", "meeting_sanjacinto.html", final=f"{SJ}{MEETING}&IsArchived=True")
    a = AgendaOnline(Fake({SJ: page(SJ, "listing_sanjacinto.html")}, {"ctl00$cphMain$AgencyPublic2$gviMeetings": opened}))
    items = a.items("sanjacinto.agendaonline.net", base.Meeting("row-0", date(2021, 6, 15), ""))
    by_no = {i.number: i for i in items}
    assert by_no["1.1"].title == "Call to Order" and by_no["1.1"].section == "1. CALL TO ORDER-PUBLIC SESSION"
    assert by_no["1.1"].text and by_no["1.1"].outcome  # its abstract, and what the minutes say happened
    assert by_no["2.1"].outcome.startswith("Passed: It was moved to approve the agenda")
    assert len(by_no["4.1"].attachments) == 1


def test_an_archived_meeting_sent_to_isarchived_false_is_read_as_archived():
    """Chico's grid answers its own target with IsArchived=False and HTTP 500; the other target opens it."""
    chico = "https://agendaonline.net/public/chicounified"
    broken = Response(chico, status=500, content_type="text/html",
                      final_url="https://agendaonline.net/public/Meeting.aspx?AgencyID=1306&MeetingID=32295&AgencyTypeID=1&IsArchived=False")
    fixed = "https://agendaonline.net/public/Meeting.aspx?AgencyID=1306&MeetingID=32295&AgencyTypeID=1&IsArchived=True"
    back_to_list = page(chico, "listing_chico.html")
    fake = Fake({chico: page(chico, "listing_chico.html"), fixed: page(fixed, "meeting_sanjacinto.html")},
                {"ctl00$cphMain$AgencyPublic3$gviMeetings": back_to_list, "ctl00$cphMain$AgencyPublic2$gviMeetings": broken})
    docs = AgendaOnline(fake).documents("chicounified", base.Meeting("row-0", date(2019, 12, 18), "Special Meeting"))
    assert docs[0].url == fixed
    assert [p[1] for p in fake.posts] == ["ctl00$cphMain$AgencyPublic3$gviMeetings", "ctl00$cphMain$AgencyPublic2$gviMeetings"]


def test_walls_and_dead_ends_are_portal_errors():
    wall = Fake({SJ: Response(SJ, status=403, bot_wall=True)})
    with pytest.raises(base.PortalError):
        AgendaOnline(wall).meetings("sanjacinto.agendaonline.net", date(2021, 1, 1), date(2021, 12, 31))
    nowhere = page(f"{SJ}agency.aspx", "listing_sanjacinto.html", final=f"{SJ}Agency.aspx?PublicAgencyID=122")
    stuck = Fake({SJ: page(SJ, "listing_sanjacinto.html")},
                 {"ctl00$cphMain$AgencyPublic2$gviMeetings": nowhere, "ctl00$cphMain$AgencyPublic3$gviMeetings": nowhere})
    with pytest.raises(base.PortalError):
        AgendaOnline(stuck).documents("sanjacinto.agendaonline.net", base.Meeting("row-1", date(2021, 6, 8), ""))


def test_registered():
    assert ("agendaonline", "chicounified") in registry.detect("https://agendaonline.net/public/chicounified", http=Fake({}))
