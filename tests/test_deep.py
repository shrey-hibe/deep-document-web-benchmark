"""The deep chain's plain rules and readers (no network)."""

from datetime import date

from ddwb import catalogue
from ddwb.deep import chain, diligent, store, text, web


def test_dates_from_links_urls_and_rows():
    assert text.find_date("Agenda 10-27-2026.pdf") == date(2026, 10, 27)
    assert text.find_date("/Archive/2025_01_13_minutes.pdf") == date(2025, 1, 13)
    assert text.find_date("Regular Meeting - October 27, 2026") == date(2026, 10, 27)
    assert text.find_date("22 Oct 2026 board packet") == date(2026, 10, 22)
    assert text.find_date("Oct 27", default_year=2026) == date(2026, 10, 27)
    assert text.find_date("Oct 27") is None
    assert text.find_date("Item 2024") is None
    assert text.find_date("13/45/2026") is None  # not a real date


def test_amounts_vendors_and_terms():
    passage = "Approve a contract with Tyler Technologies, Inc. for $102,505 and River Reach Construction LLC for $1.99 million"
    assert text.amounts(passage) == [102505.0, 1990000.0]
    assert text.vendor_candidates(passage)[:2] == ["Tyler Technologies", "River Reach Construction LLC"]
    assert text.term("Lease with the TSA through June 30, 2031.") == ("through June 30, 2031", 2031)
    assert text.term("no term here") == (None, None)


def test_segments_keep_buying_items_only():
    doc = "1. Call to order\n2. Pledge of allegiance\n3. Approve agreement with Esri for GIS licences, $102,505\n" \
          "4. Proclamation honouring the high school band for its state title this year\n5. Adjourn"
    kept = text.segments(doc)
    assert len(kept) == 1 and "Esri" in kept[0]


def test_link_filters_and_listing_fingerprint():
    assert text.keep_for_locate("https://x.gov/agendas", "Agendas & Minutes")
    assert text.keep_for_locate("https://public.destinyhosted.com/agenda_publish.cfm?id=1", "City Council")
    assert not text.keep_for_locate("mailto:clerk@x.gov", "Agendas")
    links = [("https://x.gov/a/2026-10-27.pdf", "Agenda"), ("https://x.gov/about", "About us")]
    assert text.listing_hash(links) == text.listing_hash(list(reversed(links)) + [("https://x.gov/contact", "Contact")])
    assert text.listing_hash(links) != text.listing_hash(links + [("https://x.gov/a/2026-11-10.pdf", "Agenda")])


def test_html_reader_keeps_row_and_heading():
    html = """<h2>2026 Meetings</h2><table><tr><td>October 27, 2026</td><td><a href="/a.pdf">Agenda</a></td>
              <td><a href="/m.pdf">Minutes</a></td></tr></table><ul><li>Planning Commission <a href="/p.pdf">Agenda</a></li></ul>"""
    page = web.read_html(html, "https://x.gov/meetings")
    first = page.links[0]
    assert first.href == "https://x.gov/a.pdf" and first.heading == "2026 Meetings" and "October 27, 2026" in first.row
    assert "Planning Commission" in page.links[2].row


def test_markdown_reader_rows():
    md = "## Council Agendas\n| Oct 27, 2026 | [Agenda](/a.pdf) | [Packet](https://cdn.x.gov/p.pdf) |\n- [About](#top)"
    page = web.read_markdown(md, "https://x.gov/meetings")
    assert [l.href for l in page.links] == ["https://x.gov/a.pdf", "https://cdn.x.gov/p.pdf"]
    assert page.links[0].heading == "Council Agendas" and "Oct 27, 2026" in page.links[0].row


def test_site_of():
    assert web.site_of("https://council.summitoh.net/pages/x.html") == "summitoh.net"
    assert web.site_of("https://www.co.summit.oh.us/") == "summit.oh.us"
    assert web.site_of("https://pylusd.org/x") == "pylusd.org"
    assert web.site_of("https://www.pylusd.k12.ca.us/") == "pylusd.k12.ca.us"


def test_link_kinds_and_governing_rule():
    link = web.Link("https://x.gov/docs/2026-10-27.pdf", "Minutes", "October 27, 2026 Minutes")
    assert chain.classify_link(link, date(2026, 10, 27))[0] == "minutes"
    assert chain.classify_link(web.Link("https://x.gov/meetings/123", "Regular Meeting"), date(2026, 10, 27))[0] == "meeting_page"
    assert chain.classify_link(web.Link("https://x.gov/meetings?year=2025", "2025"), None)[0] == "listing_page"
    assert chain.is_governing(web.Link("u", "Agenda", "City Council Regular Meeting"))
    assert not chain.is_governing(web.Link("u", "Agenda", "Planning and Zoning Commission"))


def test_candidate_score_prefers_portals_and_own_meeting_pages():
    portal, _ = chain.score_candidate("https://pylusd.diligent.community/Portal/", "Board meetings", "pylusd.org")
    own, _ = chain.score_candidate("https://www.pylusd.org/board/agendas", "Board Agendas", "pylusd.org")
    news, _ = chain.score_candidate("https://news.example.com/council-meeting", "Council meeting recap", "pylusd.org")
    assert portal >= 3 and own >= 3 and news < 3


def test_event_rules():
    assert chain.event_type("Renewal of the AVEVA licence for $108,450") == "renewal"
    assert chain.event_type("Authorize advertising for bids for road salt") == "bid"
    assert chain.event_type("Approve agreement with SE Group for $44,050") == "contract"
    assert chain.action("Motion carried unanimously to approve the agreement", "minutes") == "approved"
    assert chain.action("Approve the agreement", "agenda") == "to_consider"


def test_diligent_agenda_parser():
    html = """<span>1.</span><h2>Consent</h2><a name="AgendaItem7"></a><span>1.1</span><h3>Approve Esri agreement</h3>
              <a href="/document/55">Esri quote</a><span>1.2</span><h3>Warrants</h3>"""
    items, attachments = diligent.parse_agenda(html, "https://pylusd.diligent.community")
    assert [i["title"] for i in items] == ["Approve Esri agreement", "Warrants"]
    assert attachments == [{"item": 1, "title": "Esri quote", "url": "https://pylusd.diligent.community/document/55"}]
    assert diligent.is_portal("https://pylusd.diligent.community/Portal/") and not diligent.is_portal("https://pylusd.org")


def test_deep_tables_are_log_tables():
    assert all(catalogue.TABLES[t.key].log for t, _ in store.ALL)


def test_underscore_dates_and_files_are_not_lists():
    assert text.find_date("October 13_ 2026 Board Meeting Agenda and Materials.pdf") == date(2026, 10, 13)
    assert chain.is_file("https://www.pueblolibrary.org/media/167930")
    assert chain.is_file("https://x.org/sites/default/files/2026-09/minutes.pdf")
    assert not chain.is_file("http://www.pueblolibrary.org/boardoftrustees")


def test_listing_fixes_from_the_first_scorecard():
    md = "- [October 13_ 2026 Board Meeting Agenda](/ourpages/board-agendas/2026/984 October 13_ 2026 Board Meeting Agenda.pdf)"
    page = web.read_markdown(md, "https://www.pylusd.org/apps/pages/index.jsp")
    assert page.links[0].href == "https://www.pylusd.org/ourpages/board-agendas/2026/984%20October%2013_%202026%20Board%20Meeting%20Agenda.pdf"
    here = "https://www.pueblolibrary.org/boardoftrustees"
    assert chain.classify_link(web.Link("https://www.pueblolibrary.org/boardpackets", "Board Packets"), None, here)[0] == "listing_page"
    assert chain.classify_link(web.Link("https://pueblolibrary.libnet.info/events?d=2026-03-12", "Events"), date(2026, 3, 12), here)[0] == "other"
    assert chain.classify_link(web.Link("https://www.pueblolibrary.org/obituaries-search", "Search past obituaries"), None, here)[0] == "other"
