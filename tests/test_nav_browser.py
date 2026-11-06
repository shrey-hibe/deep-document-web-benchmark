"""Nav's own HTTP client moves a host that refuses plain HTTP to the browser tier (decisions #82, #103, #123): pages,
lists and documents alike, the host remembered across tool calls, a challenge in the browser still a stop. No browser
starts here: the browser tier is a stand-in."""

from ddwb.deep import tools, web


class Tab:
    """The Fallback stand-in: answers from 'the browser', or a challenge, or no browser at all."""

    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def get(self, url, use_cache=True):
        self.asked.append(url)
        return self.answer(url)

    def close(self):
        pass


def wall(self, url, use_cache=True):
    return web.Response(url, status=403, content_type="text/html", body=b"<html>Access denied</html>", bot_wall=True)


def page(url):
    return web.Response(url, final_url=url, status=200, content_type="text/html", body=b"<html><a href='/m.pdf'>Minutes</a></html>")


def test_a_walled_page_is_read_through_the_browser_and_the_host_remembered(monkeypatch):
    monkeypatch.setenv("DDWB_BROWSER", "playwright")
    monkeypatch.setattr(web.Http, "get", wall)
    http = tools.Http()
    tab = http._fallback = Tab(page)
    got = http.get("https://www.jccal.org/")
    assert got.ok and got.via == "browser" and tab.asked == ["https://www.jccal.org/"]
    later = tools.Http()  # the next tool call: no plain try, straight to the browser
    later._fallback = Tab(page)
    monkeypatch.setattr(web.Http, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("plain HTTP tried again")))
    assert later.get("https://www.jccal.org/agendas").ok


def test_with_the_browser_off_or_missing_the_plain_answer_stands_with_the_reason(monkeypatch):
    monkeypatch.setattr(web.Http, "get", wall)
    assert tools.Http().get("https://a.example/").bot_wall  # DDWB_BROWSER=off (the tests' default)
    monkeypatch.setenv("DDWB_BROWSER", "playwright")
    http = tools.Http()
    http._fallback = Tab(lambda url: web.Response(url, error="the browser tier needs Playwright: run ... once"))
    got = http.get("https://b.example/")
    assert got.bot_wall and "the browser tier needs Playwright" in got.error


def test_a_challenge_in_the_browser_is_still_a_stop(monkeypatch):
    monkeypatch.setenv("DDWB_BROWSER", "playwright")
    monkeypatch.setattr(web.Http, "get", wall)
    http = tools.Http()
    http._fallback = Tab(lambda url: web.Response(url, error="a challenge frame; stopped there, not solved", bot_wall=True))
    got = http.get("https://c.example/")
    assert got.bot_wall and "refused to plain HTTP" in got.error and "not solved" in got.error


def test_the_host_clock_works_from_the_browsers_worker_thread():
    import threading
    from ddwb.deep.state import HostClock
    clock, errors = HostClock(), []
    clock.wait("example.test", 0.0)

    def worker():
        try:
            clock.wait("example.test", 0.0)
            clock.needs_browser("example.test", "test")
        except Exception as error:  # a SQLite connection used outside its thread
            errors.append(error)
    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert errors == [] and clock.browser_only("example.test") == "test"


def test_an_http_address_is_tried_over_https_first_in_the_browser(monkeypatch):
    monkeypatch.setenv("DDWB_BROWSER", "playwright")
    monkeypatch.setattr(web.Http, "get", wall)
    http = tools.Http()
    tab = http._fallback = Tab(lambda url: page(url) if url.startswith("https://") else web.Response(url, status=403, bot_wall=True))
    got = http.get("http://www.jccal.org")
    assert got.ok and tab.asked == ["https://www.jccal.org"]
