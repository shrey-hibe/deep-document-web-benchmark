"""The Granicus adapter, offline, on trimmed copies of real ViewPublisher views and generated agendas (fixtures under
tests/fixtures/portals/granicus/: rows other than the ones tested and empty tables removed)."""

from datetime import date
from pathlib import Path

import pytest

from ddwb.deep.web import Response
from ddwb.portals import base, granicus, registry

FIX = Path(__file__).parent / "fixtures" / "portals" / "granicus"


class FakeHttp:
    """URL -> Response; anything else answers like a missing Granicus view (404)."""

    def __init__(self, pages: dict):
        self.pages, self.asked = pages, []

    def get(self, url, use_cache=True):
        self.asked.append(url)
        page = self.pages.get(url)
        if page is None:
            return Response(url, final_url=url, status=404, content_type="text/html", body=b"Page not found\n")
        return page

    def post(self, url, data=None, json_body=None, headers=None, use_cache=True):
        return self.get(url)


def html(url, name, final=None):
    return Response(url, final_url=final or url, status=200, content_type="text/html; charset=utf-8",
                    body=(FIX / name).read_bytes())


def view(client, n):
    return granicus.view_url(client, n)


def test_detect_from_portal_urls_and_own_pages():
    a = granicus.Granicus(FakeHttp({}))
    assert a.detect("https://buttecounty.granicus.com/ViewPublisher.php?view_id=2") == "buttecounty"
    assert a.detect("https://www.granicus.com/solutions") is None
    assert a.detect("https://legistar2.granicus.com/x/attachments/a.pdf") is None
    assert a.detect("https://matanuska.legistar1.com/matanuska/meetings/a.pdf") is None
    page = '<p>Powered by <a href="https://www.granicus.com">Granicus</a></p>' \
           '<iframe src="//cityofnsb.granicus.com/ViewPublisher.php?view_id=7" title="Meetings"></iframe>'
    assert a.detect("https://www.cityofnsb.com/agendas", html=page) == "cityofnsb"
    assert 7 in a.hints["cityofnsb"]  # the view the city embeds is probed even beyond the usual range
    assert a.detect("https://www.example.gov/", html='<a href="https://www.granicus.com">Granicus</a>') is None
    assert registry.detect("https://buttecounty.granicus.com/MediaPlayer.php?view_id=2&clip_id=1") == [("granicus", "buttecounty")]


def butte():
    return FakeHttp({view("buttecounty", 2): html(view("buttecounty", 2), "butte_view2.html"),
                     view("buttecounty", 3): html(view("buttecounty", 3), "butte_view3.html")})


def test_meetings_read_every_view_with_boards_from_categories():
    http = butte()
    listing = granicus.Granicus(http).meetings("buttecounty", date(2020, 1, 1), date(2026, 12, 31))
    by = {m.id: m for m in listing.meetings}
    assert set(by) == {"clip-1379", "clip-1398", "clip-1399", "clip-956"}
    m = by["clip-1379"]
    assert (m.date, m.board, m.kind, m.governing) == (date(2024, 5, 28), "Board of Supervisors", "regular", True)
    assert m.url == "https://buttecounty.granicus.com/MediaPlayer.php?view_id=2&clip_id=1379"
    assert (by["clip-1398"].kind, by["clip-1398"].governing) == ("special", True)
    assert (by["clip-956"].board, by["clip-956"].kind, by["clip-956"].governing) == ("Planning Commission", "committee", False)
    assert listing.per_year == {2021: 1, 2024: 3}
    assert any(n.startswith("view 2 (BCC View)") for n in listing.notes)
    assert any("list meetings from 2021-03-25 to 2024-07-30" in n for n in listing.notes)
    # view 1 is missing, 2 and 3 are read, then five misses in a row end the probe
    assert [u.rsplit("=", 1)[1] for u in http.asked] == ["1", "2", "3", "4", "5", "6", "7", "8"]


def test_documents_and_items_from_a_generated_agenda():
    http = butte()
    agenda = "https://buttecounty.granicus.com/AgendaViewer.php?view_id=2&clip_id=1379"
    generated = "https://buttecounty.granicus.com/GeneratedAgendaViewer.php?view_id=2&clip_id=1379"
    minutes = "https://buttecounty.granicus.com/MinutesViewer.php?view_id=2&clip_id=1379&doc_id=88516d8c-29b5-11ef-81ef-005056a89546"
    http.pages[agenda] = html(agenda, "butte_generated_agenda.html", final=generated)
    http.pages[minutes] = Response(minutes, final_url="https://buttecounty.granicus.com/DocumentViewer.php?file=buttecounty_1.pdf",
                                   status=200, content_type="application/pdf", body=b"%PDF-1.7\n")
    a = granicus.Granicus(http)
    m = next(x for x in a.meetings("buttecounty", date(2024, 1, 1), date(2024, 12, 31)).meetings if x.id == "clip-1379")
    docs = {d.kind: d for d in reversed(a.documents("buttecounty", m))}  # the first of each kind
    assert docs["minutes"].url == "https://buttecounty.granicus.com/DocumentViewer.php?file=buttecounty_1.pdf"
    assert docs["minutes"].format == "pdf"
    assert (docs["agenda"].url, docs["agenda"].format) == (generated, "html")
    assert docs["video"].url == "https://buttecounty.granicus.com/MediaPlayer.php?view_id=2&clip_id=1379"
    assert any(d.title == "PDF of Agenda" and "MetaViewer.php" in d.url for d in a.documents("buttecounty", m))

    items = a.items("buttecounty", m)
    by = {i.number: i for i in items if i.number}
    assert by["3.01"].title == "Approval of Board of Supervisors Regular Meeting Minutes"
    assert by["3.01"].section == "3B. ADOPT CONSENT AGENDA"
    assert [d.title for d in by["3.02"].attachments] == ["Staff Report", "Resolution 24-072"]
    assert all(d.kind == "item" and "MetaViewer.php" in d.url for d in by["3.02"].attachments)
    assert by["3B"].outcome and "Motion passed 4-0-1" in by["3B"].outcome
    assert by["3.02"].outcome is None  # background text that mentions an earlier adoption is not an outcome


def test_items_with_lettered_sub_items():
    agenda = "https://cityofnewport.granicus.com/AgendaViewer.php?view_id=1&clip_id=1040"
    items, _ = granicus.parse_agenda((FIX / "newport_generated_agenda.html").read_text(), agenda, "clip-1040")
    assert items[0].number == "1" and items[0].title.startswith("PUBLIC HEARING - Proposed amendments")
    assert [(i.number, i.section[:2]) for i in items[1:4]] == [("a", "1."), ("b", "1."), ("c", "1.")]
    assert any(d.title == "Public Hearing" for i in items[:4] for d in i.attachments)


def test_minutes_behind_a_google_viewer_packets_and_recordings():
    v = view("cityofnsb", 1)
    minutes = "https://cityofnsb.granicus.com/MinutesViewer.php?view_id=1&clip_id=915&doc_id=80467afe-4952-11f1-9b4d-005056a89546"
    pdf = "https://cityofnsb.legistar1.com/cityofnsb/meetings/2026/1/AAB_Minutes.pdf"
    http = FakeHttp({v: html(v, "nsb_view1.html"),
                     minutes: Response(minutes, final_url=f"https://docs.google.com/gview?url={pdf}&embedded=true", status=200,
                                       content_type="text/html", body=b"<html>viewer</html>")})
    a = granicus.Granicus(http)
    m = a.meetings("cityofnsb", date(2026, 1, 1), date(2026, 1, 31)).meetings[0]
    assert (m.id, m.board, m.governing) == ("clip-915", "Airport Advisory Board", False)
    docs = a.documents("cityofnsb", m)
    kinds = {(d.kind, d.format): d.url for d in docs}
    assert kinds[("minutes", "pdf")] == pdf
    assert "cloudfront.net/cityofnsb/" in kinds[("packet", "pdf")]
    assert kinds[("video", "mp4")].startswith("https://archive-video.granicus.com/cityofnsb/")
    assert a.items("cityofnsb", m) == []  # no agenda link on this row: the portal publishes no items for it


def test_events_newer_minutes_product_and_an_unopenable_agenda():
    v1, v2 = view("comstock-mi", 1), view("comstock-mi", 2)
    agenda = "https://comstock-mi.granicus.com/AgendaViewer.php?view_id=1&event_id=132"
    http = FakeHttp({v1: html(v1, "comstock_view1.html"), v2: html(v2, "comstock_view2_training.html"),
                     agenda: Response(agenda, error="ConnectError: certificate is not valid for "
                                                    "'granicus_production_attachments.s3.amazonaws.com'")})
    a = granicus.Granicus(http)
    listing = a.meetings("comstock-mi", date(2024, 1, 1), date(2026, 12, 31))
    assert any("Training Videos" in n and "skipped" in n for n in listing.notes)
    by = {m.id: m for m in listing.meetings}
    assert set(by) == {"event-132", "event-565"}
    assert by["event-132"].board == "Planning Commission" and by["event-132"].governing is False
    docs = {d.kind: d for d in a.documents("comstock-mi", by["event-132"])}
    assert docs["agenda"].url == agenda and docs["agenda"].format is None  # kept as it is: the file host refused us
    assert docs["minutes"].url.endswith("/services/minutes/reports/991b4eaa-3121-47df-965c-5b33958634a2/attachment")
    assert docs["packet"].url.endswith(".pdf")
    assert a.items("comstock-mi", by["event-132"]) == []


def test_upcoming_events_and_audio_rows():
    v = view("mukilteo-wa", 2)
    a = granicus.Granicus(FakeHttp({v: html(v, "mukilteo_view2.html")}))
    by = {m.id: m for m in a.meetings("mukilteo-wa", date(2026, 1, 1), date(2026, 12, 31)).meetings}
    assert by["event-1520"].date == date(2026, 11, 5) and by["event-1520"].board == "Civil Service Commission"
    assert by["clip-2066"].date == date(2026, 11, 2) and by["clip-2066"].governing is True
    docs = a.documents("mukilteo-wa", by["clip-2066"])
    assert any(d.kind == "video" and d.format == "mp3" for d in docs)
    assert not any("ASX.php" in d.url for d in docs)


def test_unknown_client_bot_wall_and_no_public_view_are_errors():
    url = view("tuscaloosa", 1)
    gone = FakeHttp({url: Response(url, final_url="https://tuscaloosa.granicus.com/core/error/NotFound.aspx?Url=%2F", status=404,
                                   content_type="text/html", body=(FIX / "notfound.html").read_bytes())})
    with pytest.raises(base.PortalError, match="not a Granicus client"):
        granicus.Granicus(gone).meetings("tuscaloosa", date(2025, 1, 1), date(2025, 12, 31))
    walled = FakeHttp({view("acme", 1): Response(view("acme", 1), status=403, bot_wall=True)})
    with pytest.raises(base.PortalError, match="bot wall"):
        granicus.Granicus(walled).meetings("acme", date(2025, 1, 1), date(2025, 12, 31))
    with pytest.raises(base.PortalError, match="no public ViewPublisher view"):
        granicus.Granicus(FakeHttp({})).meetings("acme", date(2025, 1, 1), date(2025, 12, 31))


def test_registered():
    assert "granicus" in registry.names()
    assert isinstance(registry.adapter("granicus", http=FakeHttp({})), base.Portal)
