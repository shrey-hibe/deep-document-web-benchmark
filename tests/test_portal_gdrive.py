"""The Google Drive adapter, offline, on trimmed pages saved from Richland GSD 88A IL (d88a.org's minutes folders) and
the City of Vernonia OR (its council folder) on 2026-11-05."""

from datetime import date
from pathlib import Path

import pytest

from ddwb.deep.web import Response
from ddwb.portals import base, registry
from ddwb.portals.gdrive import GoogleDrive, file_date, folder_id

FIX = Path(__file__).parent / "fixtures" / "portals" / "gdrive"
VIEW = "https://drive.google.com/embeddedfolderview?id="
D88A = "https://www.d88a.org/district-information/board-of-education/board-minutes"
VERNONIA = "https://www.vernonia-or.gov/documents/"
EMPTY_FOLDER = '<html><head><title>Empty</title></head><body><div class="flip-list-header"></div><div class="flip-entries"></div></body></html>'


def page(url, name, status=200, final=None):
    body = (FIX / name).read_bytes() if name.endswith(".html") else name.encode()
    return Response(url, final_url=final or url, status=status, content_type="text/html; charset=utf-8", body=body)


class Fake:
    def __init__(self, pages):
        self.pages, self.asked = pages, []

    def get(self, url, use_cache=True):
        self.asked.append(url)
        if url not in self.pages:
            raise AssertionError(f"unexpected request {url}")
        return self.pages[url]

    def post(self, *a, **k):
        raise AssertionError("Drive needs no POST")


def d88a():
    return Fake({
        D88A: page(D88A, "page_d88a.html"),
        VIEW + "1Q0YopiDI55v5ptdTpvprI33nLRmOTdhi": page("v", "view_d88a_2026.html"),
        VIEW + "1g8zAsFy-p1MMCWMYi6UjE35ObnJb0SUJ": page("v", "view_d88a_2025.html"),
        VIEW + "1PPk2OyZTRhRFNA1l3EnNYIz1ruhNjXNq": Response("v", status=404, content_type="text/html", body=b"Not Found"),
    })


def test_folder_ids_and_file_dates():
    for url in ("https://drive.google.com/drive/folders/1g8zAsFy-p1MMCWMYi6UjE35ObnJb0SUJ?usp=sharing",
                "https://drive.google.com/drive/u/0/folders/1g8zAsFy-p1MMCWMYi6UjE35ObnJb0SUJ",
                "https://drive.google.com/embeddedfolderview?id=1g8zAsFy-p1MMCWMYi6UjE35ObnJb0SUJ#list",
                "https://drive.google.com/open?id=1g8zAsFy-p1MMCWMYi6UjE35ObnJb0SUJ"):
        assert folder_id(url) == "1g8zAsFy-p1MMCWMYi6UjE35ObnJb0SUJ"
    assert folder_id("https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUv/edit") is None
    assert file_date("BOE MINUTES 2025_07_16 .pdf JULY REGULAR MEETING") == date(2025, 7, 16)
    assert file_date("CC Agenda 2026.10.19.pdf") == date(2026, 10, 19)
    assert file_date("Minutes_for_August_12_2026.pdf") == date(2026, 8, 12)
    assert file_date("05272026_Regular_Agenda.pdf") == date(2026, 5, 27)
    assert file_date("2026-agn-071426.pdf") == date(2026, 7, 14)
    assert file_date("Resolution 104523.pdf") is None


def test_detect_a_folder_url_and_a_bodys_page():
    a = GoogleDrive(Fake({}))
    assert a.detect("https://drive.google.com/drive/folders/1g8zAsFy-p1MMCWMYi6UjE35ObnJb0SUJ") == "1g8zAsFy-p1MMCWMYi6UjE35ObnJb0SUJ"
    assert a.detect("https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUv/edit") is None  # one document
    assert a.detect(D88A, (FIX / "page_d88a.html").read_text()) == f"page:{D88A}"
    assert a.detect(VERNONIA, (FIX / "page_vernonia.html").read_text()) == f"page:{VERNONIA}"
    forms_only = '<h3><a href="https://drive.google.com/open?id=1H0osX8anVq7Go8yf8Tk0MJwwhYDN5KL3">Forms</a></h3>'
    assert a.detect("https://city.example/forms", forms_only) is None


def test_meetings_from_school_year_folders_named_by_the_page():
    a = GoogleDrive(d88a())
    listing = a.meetings(f"page:{D88A}", date(2025, 7, 1), date(2026, 12, 31))
    got = {(m.date, m.kind, m.governing) for m in listing.meetings}
    assert got == {(date(2025, 7, 16), "regular", True), (date(2025, 10, 15), "committee", False),
                   (date(2025, 10, 15), "regular", True), (date(2026, 6, 17), "regular", True),
                   (date(2026, 10, 21), "committee", False), (date(2026, 10, 21), "regular", True)}
    assert all(m.board == "BOARD" for m in listing.meetings)  # from the heading over "2025 - 2026"
    assert any("1PPk2OyZTRhRFNA1l3EnNYIz1ruhNjXNq" in n and "404" in n for n in listing.notes)  # said, not skipped
    assert not any("12Hxq02S" in u for u in a.http.asked)  # "2023 - 2024" is outside the window: not read
    synopsis = a.documents(f"page:{D88A}", next(m for m in listing.meetings if m.date == date(2026, 10, 21) and m.kind == "committee"))
    assert [(d.kind, d.format) for d in synopsis] == [("minutes", "pdf")]  # a "SYNOPSIS" in the minutes folder
    july = a.documents(f"page:{D88A}", next(m for m in listing.meetings if m.date == date(2025, 7, 16)))
    assert july[0].url == "https://drive.google.com/uc?export=download&id=1KSZDP_chaXh8UTaFQf2H2wtXp_wd7NSN"
    assert a.items(f"page:{D88A}", listing.meetings[0]) == []


def test_meetings_from_dated_meeting_folders_and_minutes_by_year():
    a = GoogleDrive(Fake({
        VERNONIA: page(VERNONIA, "page_vernonia.html"),
        VIEW + "15O8CAVSESSJBU22P5fHfPcVrJqhoCFqh": page("v", "view_vernonia_council.html"),
        VIEW + "1NvHjbdaegsWUntqCieXJ44JofteQhEFa": page("v", "login", final="https://accounts.google.com/ServiceLogin?continue=x"),
        VIEW + "1b_csFn3ieoSztMU-lKZmi-kcqefZsB_D": page("v", "view_vernonia_minutes.html"),
        VIEW + "1YqeH_a9AuytmCGJJOpqvi6KqRzugxfbp": page("v", EMPTY_FOLDER),
        VIEW + "1EC-dRvMH41UV6-jKB3_Ow93e7m2aWvot": page("v", "view_vernonia_2026-10-19.html"),
        VIEW + "1lkPXJtxofPIp6Xb3nKv1Jug7ot6MJRd4": page("v", "view_vernonia_minutes_2025.html"),
    }))
    listing = a.meetings(f"page:{VERNONIA}", date(2025, 1, 1), date(2026, 12, 31))
    assert [m.date for m in listing.meetings] == [date(2025, 1, 6), date(2025, 1, 13), date(2026, 10, 19)]
    assert {m.board for m in listing.meetings} == {"City Council"}
    assert any("private" in n for n in listing.notes)  # the planning commission's folder asks for a sign-in
    assert not any("14q9XuXq9AE" in u for u in a.http.asked)  # "2019 - Council Meeting Packets": outside the window
    oct19 = a.documents(f"page:{VERNONIA}", listing.meetings[-1])
    assert sorted(d.kind for d in oct19) == ["agenda", "packet"]
    jan6 = a.documents(f"page:{VERNONIA}", listing.meetings[0])
    assert [d.kind for d in jan6] == ["minutes"]  # its name says so; its folder does too


def test_an_open_id_link_to_a_file_is_kept_as_a_document():
    fid = "1KSZDP_chaXh8UTaFQf2H2wtXp_wd7NSN"
    a = GoogleDrive(Fake({VIEW + fid: page("v", "view_file_not_folder.html")}))
    listing = a.meetings(fid, date(2025, 1, 1), date(2025, 12, 31))
    assert [m.date for m in listing.meetings] == [date(2025, 7, 16)]
    docs = a.documents(fid, listing.meetings[0])
    assert docs[0].kind == "minutes" and docs[0].url == f"https://drive.google.com/uc?export=download&id={fid}"


def test_google_docs_linked_from_a_page_are_meetings_by_the_row_date():
    url = "https://smalltown.example/council"
    html = ('<table><tr><th>Date</th><th>Documents</th></tr><tr><td>September 8, 2026</td><td>'
            '<a href="https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUv/edit?usp=sharing">Minutes</a> '
            '<a href="https://drive.google.com/file/d/1ZyXwVuTsRqPoNmLkJiHgF/view">Agenda</a></td></tr></table>')
    a = GoogleDrive(Fake({url: page(url, html)}))
    assert a.detect(url, html) == f"page:{url}"
    listing = a.meetings(f"page:{url}", date(2026, 1, 1), date(2026, 12, 31))
    assert [m.date for m in listing.meetings] == [date(2026, 9, 8)]
    docs = {d.kind: d for d in a.documents(f"page:{url}", listing.meetings[0])}
    assert docs["minutes"].url == "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUv/export?format=txt"
    assert docs["minutes"].format == "txt"
    assert docs["agenda"].url == "https://drive.google.com/uc?export=download&id=1ZyXwVuTsRqPoNmLkJiHgF"


def test_walls_and_errors_are_portal_errors():
    fid = "1g8zAsFy-p1MMCWMYi6UjE35ObnJb0SUJ"
    with pytest.raises(base.PortalError):
        GoogleDrive(Fake({VIEW + fid: Response("v", status=429, bot_wall=True)})).meetings(fid, date(2025, 1, 1), date(2026, 12, 31))
    with pytest.raises(base.PortalError):
        GoogleDrive(Fake({D88A: Response(D88A, status=500, body=b"x")})).meetings(f"page:{D88A}", date(2025, 1, 1), date(2026, 12, 31))


def test_registered():
    assert ("gdrive", "1g8zAsFy-p1MMCWMYi6UjE35ObnJb0SUJ") in registry.detect(
        "https://drive.google.com/drive/folders/1g8zAsFy-p1MMCWMYi6UjE35ObnJb0SUJ", http=Fake({}))
