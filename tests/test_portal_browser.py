"""The browser tier's helper, offline: how it recognises a wall, the user agent it presents, its pace, and the cache it
shares with the plain HTTP client. No browser is started here."""

import json
from pathlib import Path

from ddwb.deep import web
from ddwb.portals import browser

FIX = Path(__file__).parent / "fixtures" / "portals"


def test_challenge_pages_are_recognised_and_ordinary_pages_are_not():
    assert browser.looks_like_challenge("Just a moment...", "")
    assert browser.looks_like_challenge("", "Please verify you are human to continue")
    assert browser.looks_like_challenge("Meetings Listing", "", has_frame=True)
    assert browser.looks_like_challenge("ERROR: The request could not be satisfied", "403 ERROR Request blocked.")
    long_page = "Board meetings and agendas. " * 100 + "Our contact form uses a captcha."
    assert not browser.looks_like_challenge("Board of Education", long_page)
    assert not browser.looks_like_challenge("Meetings Listing | Covina-Valley Unified School District", "Meetings")


def test_walls_in_answers_where_data_was_expected():
    assert browser.walled(403, "text/html", (FIX / "boarddocs" / "blocked.html").read_bytes())
    assert browser.walled(200, "text/html", (FIX / "simbli" / "challenge.html").read_bytes())
    assert browser.walled(429, "application/json", b"{}")
    assert not browser.walled(200, "application/json", b'[{"unique": "DSRLUF583E5B"}]')
    assert not browser.walled(200, "text/html", (FIX / "boarddocs" / "print_agenda.html").read_bytes())


def test_a_normal_chrome_user_agent_without_the_headless_marker():
    agent = browser.person_agent("156.0.8078.4")
    assert "Chrome/156.0.0.0" in agent and "Headless" not in agent and agent.startswith("Mozilla/5.0 (")


def test_pace_is_per_host_and_shared(monkeypatch):
    slept = []
    monkeypatch.setattr(browser.time, "sleep", slept.append)
    monkeypatch.setattr(browser.random, "uniform", lambda a, b: 0.0)
    monkeypatch.setattr(browser, "_due", {})
    assert browser.pace("go.boarddocs.com", gap=4.0) == 0.0
    assert browser.pace("simbli.eboardsolutions.com", gap=4.0) == 0.0  # another host: no wait
    waited = browser.pace("go.boarddocs.com", gap=4.0)
    assert 3.5 < waited <= 4.0 and slept and 3.5 < slept[-1] <= 4.0


def test_the_cache_is_shared_with_the_plain_http_client_and_nothing_starts_for_a_cached_answer():
    http, b = web.Http(), browser.Browser()
    assert b.cache == http.cache
    url = "https://go.boarddocs.com/ca/orangeusd/Board.nsf/PRINT-AgendaDetailed?open&id=DSRLUF583E5B"
    assert b._key("GET", url, None, None, None) == http._key(url)
    key = http._key(url)
    key.with_suffix(".bin").write_bytes(b"<div>agenda</div>")
    key.with_suffix(".json").write_text(json.dumps({"url": url, "final_url": url, "status": 200, "content_type": "text/html",
                                                    "error": None, "elapsed_ms": 5, "bot_wall": False,
                                                    "fetched_at": "2026-11-05T00:00:00+00:00"}))
    got = b.get(url)
    assert got.from_cache and got.ok and got.text == "<div>agenda</div>"
    assert b._worker is None  # no browser was started for it
    b.close()  # closing a browser that never started is fine


def test_which_browser_comes_from_the_setting(monkeypatch):
    monkeypatch.delenv("DDWB_BROWSER", raising=False)
    assert browser.Browser().attach is None
    monkeypatch.setenv("DDWB_BROWSER", "playwright")
    assert browser.Browser().attach is None
    monkeypatch.setenv("DDWB_BROWSER", "openclaw")
    assert browser.Browser().attach == "openclaw"
    assert browser.Browser(attach="http://127.0.0.1:9222").attach == "http://127.0.0.1:9222"  # given beats the setting
    assert browser.Browser(attach="").attach is None


class Ran:
    def __init__(self, stdout):
        self.stdout, self.returncode = stdout, 0


def test_openclaw_browser_is_started_when_not_running(monkeypatch):
    calls, ready = [], []

    def run(args, **kw):
        calls.append(args[1:])
        if args[1:] == ["browser", "start"]:
            ready.append(True)
            return Ran("started")
        return Ran("banner line\n" + json.dumps({"enabled": True, "cdpReady": bool(ready), "cdpUrl": "http://127.0.0.1:18800"}))

    monkeypatch.setattr(browser.shutil, "which", lambda name: "/usr/local/bin/openclaw")
    monkeypatch.setattr(browser.subprocess, "run", run)
    assert browser.openclaw_cdp() == "http://127.0.0.1:18800"
    assert calls == [["browser", "--json", "status"], ["browser", "start"], ["browser", "--json", "status"]]


def test_openclaw_missing_or_switched_off_is_a_portal_error(monkeypatch):
    import pytest
    from ddwb.portals.base import PortalError
    monkeypatch.setattr(browser.shutil, "which", lambda name: None)
    with pytest.raises(PortalError, match="not installed"):
        browser.openclaw_cdp()
    monkeypatch.setattr(browser.shutil, "which", lambda name: "/usr/local/bin/openclaw")
    monkeypatch.setattr(browser.subprocess, "run", lambda args, **kw: Ran(json.dumps({"enabled": False})))
    with pytest.raises(PortalError, match="switched off"):
        browser.openclaw_cdp()


class Closable:
    def __init__(self, log, name):
        self.log, self.name = log, name

    def close(self):
        self.log.append(self.name)

    def stop(self):
        self.log.append(self.name + " stopped")


def test_closing_an_attached_browser_closes_our_tab_and_disconnects_only():
    """Someone else's browser (OpenClaw's): its context and other tabs stay; our tab closes and we disconnect."""
    log = []
    b = browser.Browser(attach="openclaw")
    b._worker = browser._Worker()
    b._page, b._context, b._browser, b._pw = (Closable(log, n) for n in ("our tab", "their context", "connection", "playwright"))
    b.close()
    assert log == ["our tab", "connection", "playwright stopped"] and b._worker is None


class Plain:
    """A plain Fetch: answers by host, counts what it sends."""

    def __init__(self, answers):
        self.answers, self.asked, self.sent = answers, [], 0

    def get(self, url, use_cache=True):
        self.asked.append(url)
        self.sent += 1
        return self.answers.get(web.host_of(url)) or web.Response(url, status=200, body=b"plain")

    def post(self, url, data=None, json_body=None, headers=None, use_cache=True):
        return self.get(url)


class FakeTab:
    def __init__(self, fail=None):
        self.fail, self.opened, self.asked = fail, [], []

    def open(self, url, **kw):
        if isinstance(self.fail, browser.NoBrowser) or isinstance(self.fail, browser.Challenge):
            raise self.fail
        self.opened.append(url)

    def cached(self, method, url, data=None, json_body=None, headers=None, fresh_since=None):
        return None

    def request(self, method, url, data=None, json_body=None, headers=None, use_cache=True, fresh_since=None):
        self.asked.append((method, url))
        return web.Response(url, status=200, body=b"from the browser")


WALL = web.Response("x", status=403, bot_wall=True)
HUNG = web.Response("x", error="ReadTimeout: The read operation timed out")
DOWN = web.Response("x", error="ConnectTimeout: timed out")


def test_fallback_stays_plain_until_a_host_walls_it_then_that_host_goes_through_the_browser():
    plain, tab = Plain({"pub-x.escribemeetings.com": WALL}), FakeTab()
    fb = browser.Fallback(plain, browser=tab)
    assert fb.get("https://other.example/a").body == b"plain" and fb.via("https://other.example/a") == "http"
    got = fb.post("https://pub-x.escribemeetings.com/MeetingsCalendarView.aspx/GetCalendarMeetings", json_body={})
    assert got.body == b"from the browser" and fb.moved == {"pub-x.escribemeetings.com": "HTTP 403"}
    assert tab.opened == ["https://pub-x.escribemeetings.com/"]  # its front page first, as a visitor arrives
    fb.get("https://pub-x.escribemeetings.com/FileStream.ashx?DocumentId=1")
    assert len(tab.opened) == 1 and len(tab.asked) == 2 and len(plain.asked) == 2  # no more plain tries on that host
    assert fb.via("https://pub-x.escribemeetings.com/y") == "browser" and fb.sent == 2 + 2


def test_fallback_moves_a_host_that_never_answers_but_not_one_that_is_down():
    tab = FakeTab()
    fb = browser.Fallback(Plain({"x.iqm2.com": HUNG, "gone.example": DOWN}), browser=tab)
    assert fb.get("https://x.iqm2.com/Citizens/Calendar.aspx").body == b"from the browser"
    assert fb.get("https://gone.example/").error.startswith("ConnectTimeout") and fb.via("https://gone.example/") == "http"


def test_fallback_without_a_browser_returns_the_plain_answer_and_a_wall_in_the_browser_stops():
    fb = browser.Fallback(Plain({"x.iqm2.com": HUNG}), browser=FakeTab(fail=browser.NoBrowser("no Playwright")))
    assert fb.get("https://x.iqm2.com/a").error.startswith("ReadTimeout")
    fb = browser.Fallback(Plain({"pub-x.escribemeetings.com": WALL}), browser=FakeTab(fail=browser.Challenge("a challenge frame")))
    got = fb.get("https://pub-x.escribemeetings.com/")
    assert got.bot_wall and "challenge" in got.error
    assert browser.Fallback(None, browser=FakeTab()).via("https://anything.example/") == "browser"  # http=None: all browser


class Memory:
    def __init__(self, known=None):
        self.known = dict(known or {})

    def browser_only(self, host):
        return self.known.get(host)

    def needs_browser(self, host, why):
        self.known[host] = why


def test_fallback_remembers_moved_hosts_across_processes():
    memory, tab = Memory(), FakeTab()
    browser.Fallback(Plain({"x.iqm2.com": HUNG}), browser=tab, remember=memory).get("https://x.iqm2.com/a")
    assert memory.known == {"x.iqm2.com": HUNG.error[:80]}
    plain = Plain({"x.iqm2.com": HUNG})
    later = browser.Fallback(plain, browser=FakeTab(), remember=memory)  # the next tool call: no plain try, no timeout
    assert later.get("https://x.iqm2.com/b").body == b"from the browser" and plain.asked == []


def test_a_moved_host_answered_before_starts_no_browser(tmp_path):
    url = "https://x.iqm2.com/Citizens/Calendar.aspx?From=1/1/2026"
    b = browser.Browser(cache=tmp_path, attach="")
    key = b._key("GET", url, None, None, None)
    key.with_suffix(".bin").write_bytes(b"<table>calendar</table>")
    key.with_suffix(".json").write_text(json.dumps({"url": url, "final_url": url, "status": 200, "content_type": "text/html",
                                                    "error": None, "elapsed_ms": 5, "bot_wall": False,
                                                    "fetched_at": "2026-11-06T10:00:00+00:00"}))
    fb = browser.Fallback(None, browser=b)
    assert fb.get(url).from_cache and b._worker is None and b.log == []
    assert b.cached("GET", url, fresh_since="2026-11-06T11:00:00") is None  # older than the run: read fresh
