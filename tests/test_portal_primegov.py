"""PrimeGov adapter, offline. Fixtures are East Bay Regional Park District's (ebparks.primegov.com) and Peoria AZ's
(peoriaaz.primegov.com) own responses, saved 2026-11-05 and trimmed: three 2025 meetings, the committees they name,
a template-layout HTML agenda (sections 4-5) and a Word-built one (three consent items)."""

from datetime import date
from pathlib import Path

import pytest

from ddwb.deep.web import Response
from ddwb.portals import base, registry
from ddwb.portals.primegov import PrimeGov, parse_agenda

FIX = Path(__file__).parent / "fixtures" / "portals" / "primegov"
B = "https://ebparks.primegov.com"


class FakeHttp:
    def __init__(self, routes: dict[str, tuple[int, str, str]]):
        self.routes, self.calls = routes, []

    def get(self, url, use_cache=True):
        self.calls.append(url)
        status, ctype, name = self.routes.get(url, (404, "text/html", None))
        body = (FIX / name).read_bytes() if name else b"not found"
        return Response(url, final_url=url, status=status, content_type=ctype, body=body, bot_wall=status in (403, 429))

    def post(self, url, data=None, json_body=None, headers=None, use_cache=True):
        raise AssertionError("PrimeGov needs no POST")


def routes():
    j = "application/json; charset=utf-8"
    return {f"{B}/api/v2/PublicPortal/GetArchivedMeetingYears": (200, j, "years.json"),
            f"{B}/api/committee/GetCommitteeesListByShowInPublicPortal": (200, j, "committees.json"),
            f"{B}/api/v2/PublicPortal/ListArchivedMeetings?year=2025": (200, j, "meetings_2025.json"),
            f"{B}/Portal/Meeting?meetingTemplateId=5392": (200, "text/html", "agenda_template.html")}


def test_detect_from_url_and_own_page():
    a = PrimeGov(FakeHttp({}))
    assert a.detect("https://ebparks.primegov.com/public/portal") == "ebparks"
    assert a.detect("https://www.primegov.com/") is None
    assert a.detect("https://www.ebparks.org/meetings", html='<iframe src="https://ebparks.primegov.com/public/portal"></iframe>') == "ebparks"
    assert a.detect("https://legistar.example.gov/") is None
    assert "primegov" in registry.names()


def test_meetings_boards_and_minutes_share():
    http = FakeHttp(routes())
    a = PrimeGov(http)
    listing = a.meetings("ebparks", date(2025, 1, 1), date(2025, 12, 31))
    assert listing.per_year == {2025: 3}
    by_id = {m.id: m for m in listing.meetings}
    board, committee = by_id["1164"], by_id["1166"]
    assert board.date == date(2025, 1, 14) and board.governing is True and board.kind == "regular"
    assert committee.governing is False and committee.kind == "committee" and "Legislative" in committee.board
    assert any("minutes listed for 2 of 3" in n for n in listing.notes)
    assert not any("ListUpcoming" in c for c in http.calls)  # a past window does not ask for upcoming meetings


def test_documents_point_at_files_and_say_when_minutes_are_missing():
    a = PrimeGov(FakeHttp(routes()))
    listing = a.meetings("ebparks", date(2025, 1, 1), date(2025, 12, 31))
    by_id = {m.id: m for m in listing.meetings}
    docs = a.documents("ebparks", by_id["1164"])
    minutes = [d for d in docs if d.kind == "minutes"]
    assert minutes and minutes[0].url == f"{B}/Public/CompiledDocument?meetingTemplateId=5381&compileOutputType=1"
    assert minutes[0].format == "pdf"
    assert any(d.kind == "video" for d in docs) and any(d.kind == "packet" for d in docs)
    assert any(d.kind == "agenda" and d.format == "html" and "Portal/Meeting?meetingTemplateId=" in d.url for d in docs)
    assert docs.notes == []
    without = next(m for m in listing.meetings if m.id not in ("1164", "1166"))
    docs = a.documents("ebparks", without)
    assert "minutes" not in {d.kind for d in docs}
    assert docs.notes and "no minutes posted" in docs.notes[0]


def test_items_from_the_html_agenda():
    a = PrimeGov(FakeHttp(routes()))
    listing = a.meetings("ebparks", date(2025, 1, 1), date(2025, 12, 31))
    items = a.items("ebparks", next(m for m in listing.meetings if m.id == "1166"))
    assert [i.number for i in items] == ["a", "a", "b", "c"]
    assert items[0].section == "4. Action Items" and items[1].section == "5. Informational Items"
    assert items[1].attachments[1].title == "Grants Staff Report 12.13.2024"
    assert "historyattachment/?historyId=" in items[1].attachments[1].url and items[1].attachments[1].item_no == "a"


def test_items_from_a_word_built_agenda():
    items = parse_agenda((FIX / "agenda_docx.html").read_text(), "https://peoriaaz.primegov.com", "290")
    assert [i.number for i in items] == ["1C", "2C", "3C"]
    assert items[0].title == "Minutes" and items[0].section == "Consent Agenda"
    assert items[0].text.startswith("Discussion and possible action to approve the March 3, 2026")
    assert [d.title for d in items[0].attachments] == ["Council Communication Background Document", "March 3, 2026 Minutes"]


def test_a_refusal_is_an_error_not_an_empty_listing():
    url = f"{B}/api/v2/PublicPortal/GetArchivedMeetingYears"
    a = PrimeGov(FakeHttp({url: (403, "text/html", "years.json")}))
    with pytest.raises(base.PortalError, match="bot wall"):
        a.meetings("ebparks", date(2025, 1, 1), date(2025, 12, 31))


def test_a_missing_year_is_a_note():
    r = routes()
    del r[f"{B}/api/v2/PublicPortal/ListArchivedMeetings?year=2025"]
    listing = PrimeGov(FakeHttp(r)).meetings("ebparks", date(2024, 1, 1), date(2025, 12, 31))
    assert any("2025" in n and "not read" in n for n in listing.notes)
