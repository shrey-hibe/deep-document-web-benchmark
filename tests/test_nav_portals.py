"""The navigator's portal wiring, offline: fake adapters through the registry, a Legistar-style listing mapped into nodes,
documents fetched through an adapter's transport, and the `portal` tools with their error path. No network."""

import argparse
import json
import re
from datetime import date, timedelta

import pytest

from ddwb.deep import state as nav_state
from ddwb.deep import tools, web
from ddwb.portals import base
from ddwb.portals import registry as preg
from ddwb.portals.browser import Challenge

SID = "t1:test:acme-co"
LIST = "https://acme.fakelegistar.example/Calendar.aspx"
FILES = "https://acme.fakelegistar.example/View.ashx"
SOON = date.today() + timedelta(days=10)


class FakeHttp:
    """Stands in for the session's polite GET: pages by URL; anything else is a 404. Records what was asked."""

    def __init__(self, pages: dict | None = None) -> None:
        self.pages, self.calls = pages or {}, []

    def get(self, url, use_cache=True, fresh_since=None):
        self.calls.append(url)
        status, ctype, body = self.pages.get(url, (404, "text/html", b"<html>not found</html>"))
        return web.Response(url, final_url=url, status=status, content_type=ctype, body=body, bot_wall=status in (403, 429))

    def post(self, url, **kw):
        raise AssertionError("no POST expected")


class WithNotes(list):
    notes: list


def meetings_of_acme() -> list:
    return [
        base.Meeting("101", date(2025, 3, 4), "Regular Meeting", board="Board of Commissioners", kind="regular", governing=True,
                     url="https://acme.fakelegistar.example/MeetingDetail.aspx?ID=101"),
        base.Meeting("102", date(2025, 3, 4), "Special Meeting", board="Board of Commissioners", kind="special", governing=True),
        base.Meeting("103", date(2025, 3, 10), "Planning Board", board="Planning Board", kind="committee"),  # governing: the rule
        base.Meeting("104", date(2026, 1, 6), "Regular Meeting", board="Board of Commissioners", kind="regular", governing=True),
        base.Meeting("105", SOON, "Regular Meeting", board="Board of Commissioners", kind="regular", governing=True),
    ]


@pytest.fixture
def fake(monkeypatch):
    """A Legistar-style adapter registered as the only one, and what it was asked."""
    monkeypatch.setattr(preg, "_FACTORIES", {})
    monkeypatch.setattr(preg, "ADAPTERS", ())
    asked = {"meetings": [], "documents": [], "items": [], "fail": None, "items_fail": None, "made_with": []}

    @preg.register
    class FakeLegistar:
        name, transport = "fakelegistar", "http"

        def __init__(self, http):
            self.http = http
            asked["made_with"].append(http)

        def detect(self, url, html=None):
            m = re.search(r"//([a-z]+)\.fakelegistar\.example", url or "") or re.search(r"//([a-z]+)\.fakelegistar\.example", html or "")
            return m.group(1) if m else None

        def meetings(self, client, since, until):
            asked["meetings"].append((client, since, until))
            if asked["fail"]:
                raise asked["fail"]
            return base.Listing([m for m in meetings_of_acme() if since <= m.date <= until],
                                notes=["the web calendar lists meetings the API does not (kept): Board of Commissioners 1"])

        def documents(self, client, meeting):
            asked["documents"].append(meeting.id)
            if asked.get("costly"):  # a portal whose every meeting costs a request
                self.http.get(f"https://acme.fakelegistar.example/MeetingDetail.aspx?ID={meeting.id}")
            docs = WithNotes([base.Document(f"{FILES}?M=A&ID={meeting.id}", "agenda", "Agenda", meeting.id, format="pdf")])
            docs.notes = []
            if meeting.id == "101":
                docs.append(base.Document(f"{FILES}?M=M&ID=101", "minutes", "Minutes", "101", format="pdf"))
                docs.append(base.Document("https://acme.granicus.example/MediaPlayer.php?clip_id=9", "video", "Video", "101"))
            if meeting.id == "104":
                docs.notes = ["no minutes posted for this meeting: only agenda"]
            return docs

        def items(self, client, meeting):
            asked["items"].append(meeting.id)
            if asked["items_fail"]:
                raise asked["items_fail"]
            if meeting.id != "101":
                return []
            return [base.Item("101", "7.a", "Contract with Acme Paving LLC for road resurfacing", section="7. CONSENT",
                              attachments=[base.Document(f"{FILES}?M=F&ID=7001", "item", "Contract", "101", "7.a")],
                              outcome="approved"),
                    base.Item("101", "3", "Approval of the minutes of February 18, 2025",
                              attachments=[base.Document(f"{FILES}?M=F&ID=3001", "minutes", "Minutes 2025-02-18", "101", "3")])]

    return asked


@pytest.fixture
def cx(tmp_path, monkeypatch):
    monkeypatch.setattr(nav_state, "NAV", tmp_path)
    c = tools.Cx(SID)
    c.st.start(c.sid, run_id="t1", arm="test", body="acme-co", body_id="b1", query="q1", since="2025-01-01",
               started_at=nav_state.now(), card={"name": "COUNTY OF ACME", "kind": "county", "state": "NC"},
               homepage="https://acmecounty.gov", status="started")
    c.sess = c.st.session(c.sid)
    c.st.put(c.sid, "B", "body", "started", data={})
    c._http = FakeHttp()
    return c


def map_args(target, **kw):
    return argparse.Namespace(**({"target": target, "rebuild": False, "plain": False, "page": 1, "meeting": None, "kind": None,
                                  "min_p": None, "max_p": None, "status": "undecided"} | kw))


def accepted(cx, url) -> str:
    g = nav_state.nid("G", url)
    cx.st.put(cx.sid, g, "page", "accepted", url=url, data={"title": "Meetings"})
    return g


def link(cx, url) -> dict:
    return cx.st.get(cx.sid, nav_state.nid("K", url))


# ------------------------------------------------------------------------------------------- map
def test_a_portal_list_is_mapped_through_its_adapter_into_the_maps_nodes(cx, fake, capsys):
    g = accepted(cx, LIST)
    tools.t_map(cx, map_args(g))
    out = capsys.readouterr().out
    assert cx._http.calls == []  # the list is the portal: read through its service, never as a page
    assert fake["meetings"] and fake["meetings"][0][1:] == (tools.SINCE, date.today() + timedelta(days=tools.PORTAL_AHEAD))
    assert "PORTAL fakelegistar (acme): 5 meetings" in out and "(2025: 3, 2026: 2)" in out and "4 of the governing board" in out
    assert "PORTAL NOTES" in out and "the web calendar lists meetings the API does not" in out

    agenda = link(cx, f"{FILES}?M=A&ID=101")["data"]
    assert agenda["date"] == "2025-03-04" and agenda["rule_kind"] == "agenda" and agenda["rule_governing"] is True
    assert (agenda["portal"], agenda["client"], agenda["meeting_id"], agenda["transport"]) == ("fakelegistar", "acme", "101", "http")
    assert "Board of Commissioners" in agenda["row"] and "Regular Meeting" in agenda["row"]
    assert link(cx, f"{FILES}?M=M&ID=101")["data"]["rule_kind"] == "minutes"
    assert link(cx, f"{FILES}?M=A&ID=103")["data"]["rule_governing"] is False  # Planning Board: the plain rule, the portal said nothing
    assert link(cx, f"{FILES}?M=A&ID=102")["status"] == "listed"

    # items of the governing board's meetings held so far, with the portal's own numbers and files
    assert sorted(fake["items"]) == ["101", "102", "104"]  # not the committee's, not the meeting still ahead
    m101 = cx.st.get(cx.sid, nav_state.nid("M", "fakelegistar|acme|101"))
    assert m101["data"]["items"][0]["no"] == "7.a" and m101["data"]["items"][0]["outcome"] == "approved"
    assert m101["data"]["items"][0]["files"][0]["id"] == nav_state.nid("K", f"{FILES}?M=F&ID=7001")
    assert link(cx, f"{FILES}?M=F&ID=7001") is None  # an item's files join the map when `portal documents` asks
    # two meetings on one date stay two meetings
    assert cx.st.get(cx.sid, nav_state.nid("M", "fakelegistar|acme|102"))["data"]["title"].endswith("Special Meeting")
    assert tools.meeting_types(link(cx, f"{FILES}?M=A&ID=102")) != tools.meeting_types(link(cx, f"{FILES}?M=A&ID=101"))

    page = cx.st.get(cx.sid, g)
    assert page["status"] == "mapped" and page["data"]["notes"] == [] and page["data"]["portals_read"] == [["fakelegistar", "acme", True]]
    assert any("no minutes posted" in x for x in page["data"]["portal_notes"])  # the portal's remark, not a gap
    c = tools.coverage(cx)
    assert c["governing_meetings"] == 2 and c["map_notes"] == [] and c["map_portal_notes"]


def test_a_page_that_links_a_portal_is_read_through_the_portal_and_as_a_page(cx, fake, capsys):
    url = "https://acmecounty.gov/agendas"
    html = (f'<html><body><h2>Board meetings</h2><a href="{LIST}">Meetings portal</a>'
            '<ul><li><a href="https://acmecounty.gov/files/2025-05-01-budget-hearing-agenda.pdf">Budget hearing agenda '
            'May 1, 2025</a></li></ul></body></html>')
    cx._http = FakeHttp({url: (200, "text/html", html.encode())})
    tools.t_map(cx, map_args(accepted(cx, url)))
    out = capsys.readouterr().out
    assert "PORTAL fakelegistar (acme)" in out
    assert link(cx, f"{FILES}?M=A&ID=101")["data"]["portal"] == "fakelegistar"  # the portal's meetings
    own = link(cx, "https://acmecounty.gov/files/2025-05-01-budget-hearing-agenda.pdf")  # and the page's own documents
    assert own and own["data"]["date"] == "2025-05-01" and "portal" not in own["data"]


def test_an_unknown_site_keeps_the_plain_path(cx, fake, capsys):
    url = "https://acmecounty.gov/minutes"
    html = '<html><body><a href="https://acmecounty.gov/files/2025-06-10-minutes.pdf">Minutes June 10, 2025</a></body></html>'
    cx._http = FakeHttp({url: (200, "text/html", html.encode())})
    tools.t_map(cx, map_args(accepted(cx, url)))
    assert fake["meetings"] == [] and "PORTAL" not in capsys.readouterr().out
    assert link(cx, "https://acmecounty.gov/files/2025-06-10-minutes.pdf")["data"]["rule_kind"] == "minutes"


def test_a_portal_that_fails_is_said_and_the_list_is_read_as_a_page(cx, fake, capsys):
    fake["fail"] = base.PortalError("acme: HTTP 500 from the events service")
    cx._http = FakeHttp({LIST: (200, "text/html", b"<html><body>a shell</body></html>")})
    tools.t_map(cx, map_args(accepted(cx, LIST)))
    out = capsys.readouterr().out
    assert "PORTAL ERROR at fakelegistar (acme): acme: HTTP 500" in out
    notes = cx.st.get(cx.sid, nav_state.nid("G", LIST))["data"]["notes"]
    assert any("PORTAL ERROR" in x for x in notes) and any("read as a plain page instead" in x for x in notes)
    assert cx._http.calls == [LIST]  # the plain path ran on the list itself
    assert tools.coverage(cx)["map_notes"]  # a gap: the answer is incomplete


def test_a_bot_wall_at_the_portal_stops_there(cx, fake, capsys):
    fake["fail"] = Challenge("https://acme.fakelegistar.example: a challenge page; stopped there, not solved")
    tools.t_map(cx, map_args(accepted(cx, LIST)))
    assert "BOT WALL at fakelegistar (acme)" in capsys.readouterr().out and "never solved" in \
        cx.st.get(cx.sid, nav_state.nid("G", LIST))["data"]["notes"][0]
    assert cx._http.calls == []  # never around it by another road


def test_the_runaway_guard_counts_requests_and_is_said(cx, fake, monkeypatch, capsys):
    monkeypatch.setattr(tools, "MAX_VIEWS", 2)
    tools.t_map(cx, map_args(accepted(cx, LIST)))  # documents served from the listing cost nothing: all five read
    assert len(fake["documents"]) == 5 and cx.st.get(cx.sid, nav_state.nid("G", LIST))["data"]["notes"] == []
    fake.update(costly=True, documents=[])
    tools.t_map(cx, map_args(nav_state.nid("G", LIST), rebuild=True))
    assert fake["documents"] == ["105", "104"]  # the governing board's first, newest first; then the guard
    notes = cx.st.get(cx.sid, nav_state.nid("G", LIST))["data"]["notes"]
    assert any("documents of 2 governing-board meetings not listed (stopped at 2 requests" in x for x in notes)


def test_map_plain_reads_the_list_as_a_page(cx, fake):
    cx._http = FakeHttp({LIST: (200, "text/html", b"<html><body>a shell</body></html>")})
    tools.t_map(cx, map_args(accepted(cx, LIST), plain=True))
    assert fake["meetings"] == [] and cx._http.calls == [LIST]


def test_adapters_read_fresh_once_per_run(cx, fake):
    ad = cx.adapter("fakelegistar")
    assert isinstance(ad.http, tools.Fallback) and isinstance(ad.http.http, tools.RunFetch) and cx.adapter("fakelegistar") is ad
    seen = []

    def get(url, use_cache=True, fresh_since=None):
        seen.append(fresh_since)
        return web.Response(url, status=200, from_cache=len(seen) > 1)
    cx._http.get = get
    ad.http.get("https://acme.fakelegistar.example/x")
    ad.http.get("https://acme.fakelegistar.example/x")
    assert seen == [cx.run_started()] * 2 and ad.http.sent == 1  # the guard counts requests sent, not cached answers


def test_a_post_cached_before_the_run_is_sent_again(tmp_path, monkeypatch):
    monkeypatch.setattr(nav_state, "NAV", tmp_path / "nav")
    monkeypatch.setattr(web, "CACHE", tmp_path / "cache")
    http = tools.Http()
    sent = []
    monkeypatch.setattr(web.Http, "request", lambda self, method, url, **kw: sent.append(kw["use_cache"]) or web.Response(url, status=200))
    payload = json.dumps({"data": {"y": "2025"}, "json": None, "headers": None}, sort_keys=True)
    key = http._key(f"POST https://p.example/list {payload}")
    key.with_suffix(".json").write_text(json.dumps({"url": "u", "fetched_at": "2026-01-01T00:00:00+00:00"}))
    http.post("https://p.example/list", data={"y": "2025"}, fresh_since="2026-11-06T00:00:00.000+00:00")
    http.post("https://p.example/list", data={"y": "2025"}, fresh_since="2025-12-01T00:00:00.000+00:00")
    assert sent == [False, True]


# ------------------------------------------------------------------------------------------- downloads
def portal_link_node(cx, url, **data) -> dict:
    k = nav_state.nid("K", url)
    cx.st.put(cx.sid, k, "link", "listed", url=url, data={"text": "Minutes", "row": "Regular Meeting", "date": "2025-03-04",
                                                          "rule_kind": "minutes", "rule_governing": True} | data)
    return cx.st.get(cx.sid, k)


class Transport:
    def __init__(self, body=b"%PDF-1.4 minutes"):
        self.body, self.got, self.opened = body, [], []

    def get(self, url, use_cache=True):
        self.got.append(url)
        return web.Response(url, final_url=url, status=200, content_type="application/pdf", body=self.body)

    def open(self, url, **kw):
        self.opened.append((url, len(self.got)))


def test_a_walled_portals_document_is_fetched_through_the_adapters_transport(cx, monkeypatch):
    monkeypatch.setattr(preg, "_FACTORIES", {})
    monkeypatch.setattr(preg, "ADAPTERS", ())
    walled = Transport()

    @preg.register
    class FakeDocs:  # BoardDocs-like: plain HTTP first, the browser once CloudFront refuses it
        name, transport = "fakedocs", "http"

        def __init__(self, http):
            self.http = http

        def detect(self, url, html=None):
            return None

        def fetch_for(self, client):
            assert client == "ca/acme"
            return self.http if self.http is not None else walled

    url = "https://go.fakedocs.example/ca/acme/Board.nsf/files/X1/$file/minutes.pdf"
    k = portal_link_node(cx, url, portal="fakedocs", client="ca/acme", transport="browser")
    path, fmt, err, resp = tools.download(cx, k)
    assert err is None and fmt == "pdf" and walled.got == [url] and cx._http.calls == []
    assert path in (tools.paths.MANIFEST.read_text())  # logged in the data manifest
    plain = portal_link_node(cx, url.replace("minutes", "agenda"), portal="fakedocs", client="ca/acme", transport="http")
    tools.download(cx, plain)
    assert cx._http.calls == [plain["url"]]  # read over plain HTTP at map time: fetched the same way


def test_a_browser_portals_document_opens_the_meeting_page_first(cx, monkeypatch):
    from ddwb.portals import browser
    monkeypatch.setattr(preg, "_FACTORIES", {})
    monkeypatch.setattr(preg, "ADAPTERS", ())
    made = []
    monkeypatch.setattr(browser, "Browser", lambda **kw: made.append(Transport()) or made[-1])

    @preg.register
    class FakeSimbli:
        name, transport = "fakesimbli", "browser"

        def __init__(self, browser=None):
            self.browser = browser

        def detect(self, url, html=None):
            return None

    url = "https://simbli.fake.example/Meetings/Attachment.aspx?S=1&AID=2&MID=3"
    meeting = "https://simbli.fake.example/SB_Meetings/ViewMeeting.aspx?S=1&MID=3"
    k = portal_link_node(cx, url, portal="fakesimbli", client="1", transport="browser", meeting_url=meeting)
    _, fmt, err, _ = tools.download(cx, k)
    assert err is None and fmt == "pdf" and made[0].opened == [(meeting, 0)] and made[0].got == [url]


def test_a_wall_on_the_portals_transport_is_said_never_read_around(cx, monkeypatch):
    monkeypatch.setattr(preg, "_FACTORIES", {})
    monkeypatch.setattr(preg, "ADAPTERS", ())

    @preg.register
    class FakeWalled:
        name, transport = "fakewalled", "http"

        def __init__(self, http):
            self.http = http

        def detect(self, url, html=None):
            return None

        def fetch_for(self, client):
            raise Challenge("go.fakewalled.example: HTTP 403; stopped there, not solved")

    k = portal_link_node(cx, "https://go.fakewalled.example/f.pdf", portal="fakewalled", client="x", transport="browser")
    with pytest.raises(tools.Stop, match="BOT WALL at fakewalled"):
        tools.t_peek(cx, argparse.Namespace(target=k["node"], pages=None))
    assert cx._http.calls == [] and cx.st.get(cx.sid, k["node"])["status"] == "failed"


def test_a_portal_document_whose_address_expired_is_looked_up_again(cx, fake):
    tools.t_map(cx, map_args(accepted(cx, LIST)))
    old = f"{FILES}?M=M&ID=101"
    k = link(cx, old)
    cx.st.put(cx.sid, k["node"], "link", k["status"], data={"text": "Minutes"})
    new = f"{FILES}?M=M&ID=101&v=2"
    cls = preg._FACTORIES["fakelegistar"]
    original = cls.documents
    cls.documents = lambda self, client, m: [base.Document(new, "minutes", "Minutes", m.id)] + original(self, client, m)[:1]
    cx._http = FakeHttp({new: (200, "application/pdf", b"%PDF-1.4 the minutes")})  # the old address: a 404 now
    _, fmt, err, _ = tools.download(cx, link(cx, old))
    assert err is None and fmt == "pdf" and cx._http.calls == [old, new]
    assert link(cx, old)["data"]["refreshed_url"] == new


# ------------------------------------------------------------------------------------------- the portal tools
def portal_args(tool, **kw):
    return argparse.Namespace(**({"portal_tool": tool, "target": None, "meeting": None, "board": None, "page": 1} | kw))


def test_portal_meetings_lists_the_meetings_with_ids_for_documents(cx, fake, capsys):
    tools.t_portal(cx, portal_args("meetings", target="fakelegistar:acme"))
    out = capsys.readouterr().out
    m101 = nav_state.nid("M", "fakelegistar|acme|101")
    assert "PORTAL fakelegistar (acme, by its name): 5 meetings" in out and f"{m101}  2025-03-04  gov  regular" in out
    assert "Planning Board" in out and "NEXT:" in out
    assert cx.st.get(cx.sid, m101)["data"]["meeting_id"] == "101"
    tools.t_portal(cx, portal_args("meetings", target="fakelegistar:acme", board=""))
    assert "Planning Board" not in capsys.readouterr().out.split("governing board: ")[1]


def test_portal_documents_lists_one_meetings_documents_and_items_into_the_map(cx, fake, capsys):
    tools.t_portal(cx, portal_args("meetings", target="fakelegistar:acme"))
    capsys.readouterr()
    m101 = nav_state.nid("M", "fakelegistar|acme|101")
    tools.t_portal(cx, portal_args("documents", meeting=m101))
    out = capsys.readouterr().out
    assert "PORTAL DOCUMENTS" in out and "3 documents, 2 items with 2 files" in out
    assert "item 7.a" in out and "[approved]" in out and nav_state.nid("K", f"{FILES}?M=F&ID=7001") in out
    k = link(cx, f"{FILES}?M=M&ID=101")
    assert k["parent"] == m101 and k["data"]["rule_kind"] == "minutes" and k["data"]["transport"] == "http"
    contract = link(cx, f"{FILES}?M=F&ID=7001")["data"]
    assert contract["rule_kind"] == "item" and contract["item_no"] == "7.a" and "item 7.a: Contract with Acme Paving" in contract["row"]
    filed = link(cx, f"{FILES}?M=F&ID=3001")["data"]  # minutes filed on this agenda are an earlier meeting's: an item's file
    assert filed["rule_kind"] == "item" and filed["portal_kind"] == "minutes"
    assert cx.st.get(cx.sid, m101)["data"]["items"][1]["no"] == "3"
    with pytest.raises(tools.Stop, match="2 portal meetings on 2025-03-04"):
        tools.t_portal(cx, portal_args("documents", meeting="2025-03-04"))
    fake["items_fail"] = base.PortalError("items of 104 refused: HTTP 500")
    tools.t_portal(cx, portal_args("documents", meeting="2026-01-06"))
    assert "ITEMS NOT READ: PORTAL ERROR at fakelegistar (acme): items of 104 refused" in capsys.readouterr().out


def test_portal_errors_are_printed_as_errors_never_an_empty_result(cx, fake, capsys):
    fake["fail"] = base.PortalError("acme: the events service answered HTTP 500")
    assert tools.main([SID, "portal", "meetings", "fakelegistar:acme"]) == 2
    out = capsys.readouterr().out
    assert "PORTAL ERROR at fakelegistar (acme): acme: the events service answered HTTP 500" in out
    assert "not an empty result" in out and "REFUSED" in out and "0 meetings" not in out
    fake["fail"] = Challenge("acme: a challenge frame; stopped there, not solved")
    assert tools.main([SID, "portal", "meetings", "fakelegistar:acme"]) == 2
    assert "BOT WALL at fakelegistar (acme)" in capsys.readouterr().out
    assert tools.main([SID, "portal", "meetings", "nosuchportal:x"]) == 2
    assert "the portals: fakelegistar" in capsys.readouterr().out


def test_portal_detect_by_address_by_page_and_a_page_that_cannot_be_read(cx, fake, capsys):
    tools.t_portal(cx, portal_args("detect", target=LIST))
    assert "by its address" in capsys.readouterr().out
    url = "https://acmecounty.gov/government/meetings"
    cx._http = FakeHttp({url: (200, "text/html", f'<iframe src="{LIST}"></iframe>'.encode()),
                         "https://walled.example/m": (403, "text/html", b"blocked")})
    tools.t_portal(cx, portal_args("detect", target=url))
    out = capsys.readouterr().out
    assert "by its page" in out and "fakelegistar  client acme" in out
    assert cx.st.get(cx.sid, nav_state.nid("G", url))["data"]["portals"] == [["fakelegistar", "acme"]]
    with pytest.raises(tools.Stop, match="bot wall: stopped there, never solved"):
        tools.t_portal(cx, portal_args("detect", target="https://walled.example/m"))


def test_fetch_and_check_say_which_portal_a_page_links(cx, fake, capsys):
    url = "https://acmecounty.gov/agendas"
    html = f'<html><body><a href="{LIST}">Agendas and minutes</a><a href="https://acmecounty.gov/parks">Parks</a></body></html>'
    cx._http = FakeHttp({url: (200, "text/html", html.encode())})
    tools.t_fetch(cx, argparse.Namespace(target=url, all_links=False))
    out = capsys.readouterr().out
    g_portal = nav_state.nid("G", LIST)
    assert f"PORTAL ON THIS PAGE: it links or embeds fakelegistar (client acme). Its link {g_portal} goes there" in out
    tools.t_check(cx, argparse.Namespace(target=g_portal, via=None))
    out = capsys.readouterr().out
    facts = cx.st.get(cx.sid, g_portal)["data"]
    assert facts["via"] == "portal:fakelegistar" and facts["meetings_in_window"] == 5 and facts["governing_meetings"] == 4
    assert facts["portals"] == [["fakelegistar", "acme"]] and "PORTAL: this page is on fakelegistar" in out
