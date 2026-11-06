"""A browser for portals that refuse plain HTTP clients, with a person's settings (decision #123).

BoardDocs (CloudFront) and Simbli (Imperva) answer a script's request with a block page but serve a visitor's browser.
`Browser` opens the portal as a visitor would, then sends the same requests the page's own scripts send, from inside the
page (same origin, the page's cookies, the browser's own network stack). It has the Fetch shape (get, post returning
ddwb.deep.web.Response), so an adapter written against Fetch runs unchanged over plain HTTP or through the browser.

Person-like (#123): Chromium without automation flags (no --enable-automation, AutomationControlled off,
navigator.webdriver unset), a normal Chrome user agent (the headless marker removed), a desktop window and en-US locale,
and a person's pace: one request at a time per host, 4 to 7 s apart (a little random), shared by every Browser in the
process; a 5xx is tried again after 10 s and 30 s. It stops at any CAPTCHA, challenge or block page (a challenge
frame, a challenge title, a short page in a challenge's words, a 403 or a 429): `open` raises Challenge (a
PortalError), and get/post answer with `bot_wall` set and send nothing more to that host. It never solves a challenge
and never calls a solving service. Plain HTTP scripts keep the honest research user agent (ddwb.deep.web.Http).

Responses to get/post are kept in the same cache as ddwb.deep.web.Http (data/cache/deep/, the same keys), so a re-run
reads them again for free and either transport reuses the other's answers. Page visits (`open`) are always live. A GET on another site (a file
on a document host) is a link opened in a new tab, as a visitor would; a POST is only sent from a page on its own site.

Which browser (`DDWB_BROWSER`, or `attach=`): unset or "playwright" launches its own Chromium; "openclaw" attaches to the
browser OpenClaw runs (a real Chrome with OpenClaw's own profile, started through `openclaw browser start` when it is not
running); a CDP address (http://127.0.0.1:9222) attaches to any Chrome started with remote debugging. Attached, it works
in a tab of its own, leaves the browser's settings and other tabs alone, and only disconnects at close. Pace, walls and
the cache are the same either way.

Playwright is optional: `uv sync --extra browser`, then `uv run playwright install chromium` once (attaching needs no
Chromium of its own). Its sync API runs in one worker thread this class owns, so it works from plain code and from inside
an event loop alike.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from ..deep import web
from .base import PortalError

MIN_GAP, JITTER = 4.0, 3.0  # seconds between requests to one host: a person reading, clicking
RETRY_WAITS = (10.0, 30.0)  # after a 5xx
VIEWPORT = {"width": 1440, "height": 900}
CHALLENGE_TITLES = ("just a moment", "attention required", "access denied", "request unsuccessful", "security check",
                    "are you a robot", "request blocked", "the request could not be satisfied", "pardon our interruption")
CHALLENGE_WORDS = ("verify you are human", "checking your browser", "complete the security check", "captcha",
                   "incapsula incident", "request unsuccessful", "access denied", "request blocked",
                   "enable javascript and cookies to continue", "press & hold", "are you a robot")
# A challenge widget in the page; a contact form's reCAPTCHA script alone is not one, so only frames count.
CHALLENGE_FRAMES = ("iframe[src*='hcaptcha'], iframe[src*='challenges.cloudflare.com'], iframe[src*='_Incapsula_Resource'], "
                    "iframe[src*='recaptcha/api2/bframe'], iframe[src*='captcha-delivery'], iframe[src*='geo.captcha']")
_WALL_MARKERS = ("_incapsula_resource", "request blocked", "the request could not be satisfied", "captcha", "cf-chl",
                 "challenge-platform", "attention required", "just a moment", "access denied", "request unsuccessful")


class Challenge(PortalError):
    """A CAPTCHA, challenge or block page: the run stopped there. It is never solved."""


class NoBrowser(PortalError):
    """The browser tier is not available here: Playwright missing, Chromium not installed, or the browser to attach to
    (OpenClaw's) not running."""


def looks_like_challenge(title: str, text: str, has_frame: bool = False) -> bool:
    """A challenge frame, a challenge title, or a short page whose text is a challenge's (not a long page that
    mentions one)."""
    title, text = (title or "").casefold(), " ".join((text or "").split()).casefold()
    return has_frame or any(w in title for w in CHALLENGE_TITLES) or (len(text) < 1500 and any(w in text for w in CHALLENGE_WORDS))


def walled(status: int | None, content_type: str | None, body: bytes) -> bool:
    """A response that is a block page rather than the data asked for: a 403 or 429, or a whole HTML page in a wall's
    words (the endpoints the adapters call answer with JSON, fragments or files)."""
    if status in (403, 429):
        return True
    head = body[:6000].decode("utf-8", errors="ignore").casefold()
    whole_page = "<html" in head or "<!doctype" in head
    return whole_page and any(m in head for m in _WALL_MARKERS)


def openclaw_cdp(start: bool = True, timeout: int = 60) -> str:
    """The CDP address of the browser OpenClaw runs (`openclaw browser status`), started first when it is not running."""
    exe = shutil.which("openclaw")
    if not exe:
        raise NoBrowser("DDWB_BROWSER=openclaw but the `openclaw` command is not installed")

    def status() -> dict:
        try:
            out = subprocess.run([exe, "browser", "--json", "status"], capture_output=True, text=True, timeout=timeout)
            return json.loads(out.stdout[out.stdout.index("{"):])
        except (ValueError, subprocess.SubprocessError) as error:
            raise NoBrowser(f"`openclaw browser status` gave no answer ({str(error)[:120]}): is the gateway running?") from error

    st = status()
    if not st.get("enabled", True):
        raise NoBrowser("OpenClaw's browser is switched off in its settings")
    if not st.get("cdpReady") and start:
        subprocess.run([exe, "browser", "start"], capture_output=True, text=True, timeout=timeout)
        st = status()
    if not st.get("cdpReady") or not st.get("cdpUrl"):
        raise NoBrowser("OpenClaw's browser is not running (`openclaw browser start`)")
    return st["cdpUrl"]


def setup_hint() -> str:
    """How to get the browser tier here: Chromium for Playwright once, or the browser OpenClaw runs, when it is installed."""
    from .. import paths
    hint = f"run `uv run --project {paths.REPO} --extra browser playwright install chromium` once"
    if shutil.which("openclaw"):
        hint += (", or use the browser your OpenClaw runs: DDWB_BROWSER=openclaw (in the Claude Code plugin: "
                 "/plugin configure public-records@public-signals, Browser = openclaw)")
    return hint


def person_agent(version: str) -> str:
    """A normal desktop Chrome user agent for this browser's version, as Chrome itself reports it (reduced form)."""
    major = (version or "").split(".")[0] or "140"
    system = {"darwin": "Macintosh; Intel Mac OS X 10_15_7", "win32": "Windows NT 10.0; Win64; x64"}.get(sys.platform,
                                                                                                          "X11; Linux x86_64")
    return f"Mozilla/5.0 ({system}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"


# ------------------------------------------------------------------------------------------- pace, shared per host
_clock = threading.Lock()
_due: dict[str, float] = {}


def pace(host: str, gap: float = MIN_GAP, jitter: float = JITTER) -> float:
    """Wait for this host's next turn (every Browser in the process shares the clock). Returns the seconds waited."""
    with _clock:
        now = time.monotonic()
        start = max(now, _due.get(host, 0.0))
        _due[host] = start + gap + random.uniform(0, jitter)
    if start > now:
        time.sleep(start - now)
    return max(0.0, start - now)


# ------------------------------------------------------------------------------------------- the worker thread
class _Worker(threading.Thread):
    """Playwright's sync API in one thread of its own: every call on the browser runs here."""

    def __init__(self) -> None:
        super().__init__(daemon=True, name="portal-browser")
        self.jobs: queue.Queue = queue.Queue()
        self.start()

    def run(self) -> None:
        while (job := self.jobs.get()) is not None:
            fn, args, box, done = job
            try:
                box.append((True, fn(*args)))
            except BaseException as error:  # handed back to the caller's thread
                box.append((False, error))
            done.set()

    def call(self, fn, *args):
        if threading.current_thread() is self:
            return fn(*args)
        box, done = [], threading.Event()
        self.jobs.put((fn, args, box, done))
        done.wait()
        ok, value = box[0]
        if ok:
            return value
        raise value


@dataclass
class Visit:
    url: str
    final_url: str
    status: int
    title: str
    html: str


_FETCH = """async ([url, method, body, headers]) => {
  const r = await fetch(url, {method, body: body === null ? undefined : body, headers, credentials: "same-origin"});
  const bytes = new Uint8Array(await r.arrayBuffer());
  let bin = "";
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return {status: r.status, url: r.url, type: r.headers.get("content-type") || "", body: btoa(bin)};
}"""


class Browser:
    """One Chromium window (headless unless asked), used one request at a time at a person's pace."""

    transport = "browser"

    def __init__(self, *, headless: bool = True, gap: float = MIN_GAP, jitter: float = JITTER, settle_ms: int = 4000,
                 timeout_ms: int = 30_000, cache: Path | None = None, attach: str | None = None, clock=None) -> None:
        self.headless, self.gap, self.jitter, self.settle_ms, self.timeout_ms = headless, gap, jitter, settle_ms, timeout_ms
        attach = os.environ.get("DDWB_BROWSER", "") if attach is None else attach
        self.attach = None if attach.strip().lower() in ("", "playwright") else attach.strip()  # "openclaw" or a CDP address
        self.cache = cache or web.CACHE  # read when made, so a test can point it elsewhere
        self.clock = clock  # a host clock shared across processes (nav's HostClock.wait); else this process's pace()
        self.requests = 0  # what we sent: page visits and in-page calls (not the page's own loading)
        self.log: list[tuple[str, str, int | str | None]] = []  # (method, url, status or error) of each, in order
        self.blocked: dict[str, str] = {}  # host -> why it was stopped
        self._worker: _Worker | None = None
        self._pw = self._browser = self._context = self._page = None

    # -------------------------------------------------------------------------------- lifecycle
    def _pace(self, host: str) -> None:
        if self.clock is not None:
            self.clock.wait(host, self.gap + random.uniform(0, self.jitter))
        else:
            pace(host, self.gap, self.jitter)

    def _call(self, fn, *args):
        if self._worker is None:
            self._worker = _Worker()
        return self._worker.call(fn, *args)

    def _start(self) -> None:
        if self._page is not None:
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise NoBrowser(f"the browser tier needs Playwright (`uv sync --extra browser`): then {setup_hint()}") from error
        self._pw = sync_playwright().start()
        if self.attach:
            self._attach()
            return
        try:  # "chromium": the full browser in its new headless mode, not the stripped headless shell
            self._browser = self._pw.chromium.launch(headless=self.headless, channel="chromium",
                                                     ignore_default_args=["--enable-automation"],
                                                     args=["--disable-blink-features=AutomationControlled"])
        except Exception as error:
            self._pw.stop()
            self._pw = None
            raise NoBrowser(f"Chromium did not start ({str(error).splitlines()[0][:160]}): {setup_hint()}") from error
        self._context = self._browser.new_context(user_agent=person_agent(self._browser.version), viewport=VIEWPORT,
                                                  locale="en-US", accept_downloads=False)
        self._context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        self._page = self._context.new_page()
        self._page.set_default_timeout(self.timeout_ms)

    def _attach(self) -> None:
        """A tab of our own in a browser someone else runs (OpenClaw's, or any Chrome with remote debugging)."""
        where = openclaw_cdp() if self.attach.lower() == "openclaw" else self.attach
        try:
            self._browser = self._pw.chromium.connect_over_cdp(where, timeout=self.timeout_ms)
        except Exception as error:
            self._pw.stop()
            self._pw = None
            raise NoBrowser(f"could not attach to the browser at {where} ({str(error).splitlines()[0][:160]})") from error
        # its own profile, cookies and user agent, as the person who runs it would have them; nothing of ours is set on it
        self._context = self._browser.contexts[0] if self._browser.contexts else self._browser.new_context()
        self._page = self._context.new_page()
        self._page.set_default_timeout(self.timeout_ms)

    def close(self) -> None:
        if self._worker is None:
            return

        def stop():
            if self.attach:  # someone else's browser: close our tab and disconnect, never the browser or its other tabs
                for thing in (self._page, self._browser):
                    try:
                        if thing is not None:
                            thing.close()
                    except Exception:
                        pass
                if self._pw is not None:
                    try:
                        self._pw.stop()
                    except Exception:
                        pass
                self._pw = self._browser = self._context = self._page = None
                return
            for thing in (self._context, self._browser):
                try:
                    if thing is not None:
                        thing.close()
                except Exception:  # already gone
                    pass
            if self._pw is not None:
                try:
                    self._pw.stop()
                except Exception:
                    pass
            self._pw = self._browser = self._context = self._page = None

        self._worker.call(stop)
        self._worker.jobs.put(None)
        self._worker = None

    def __enter__(self) -> Browser:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -------------------------------------------------------------------------------- visiting pages
    @property
    def url(self) -> str:
        """The address of the main page (where requests are sent from)."""
        return self._call(lambda: self._page.url if self._page is not None else "")

    def open(self, url: str, ready: str | None = None, ready_ms: int = 15_000, until_url: str | None = None,
             side: bool = False) -> Visit:
        """Go to a page as a visitor does and let its scripts settle: until `ready` (a JS condition) or `until_url` (a
        regex the address reaches, e.g. after the page's own redirect) when given. `side`: in a new tab, closed after,
        so the main page (where requests are sent from) stays where it is. Raises Challenge at a block or challenge
        page, PortalError when the page fails or never reaches `until_url`."""
        return self._call(self._open, url, ready, ready_ms, until_url, side)

    def _open(self, url: str, ready: str | None, ready_ms: int, until_url: str | None, side: bool) -> Visit:
        self._start()
        host = web.host_of(url)
        if host in self.blocked:
            raise Challenge(f"{host} stopped earlier in this run: {self.blocked[host]}")
        self._pace(host)
        self.requests += 1
        page = self._context.new_page() if side else self._page
        try:
            return self._visit(page, url, ready, ready_ms, until_url)
        finally:
            if side:
                page.close()

    def _visit(self, page, url: str, ready: str | None, ready_ms: int, until_url: str | None) -> Visit:
        try:
            response = page.goto(url, wait_until="domcontentloaded")
        except Exception as error:  # DNS, refused, timeout: the page failed, not a wall
            raise PortalError(f"{url}: {str(error).splitlines()[0][:160]}") from error
        reached = True
        try:
            if until_url:
                reached = False
                page.wait_for_url(re.compile(until_url), timeout=ready_ms, wait_until="commit")
                reached = True
            elif ready:
                page.wait_for_function(ready, timeout=ready_ms)
            else:
                page.wait_for_load_state("networkidle", timeout=self.settle_ms)
        except Exception:  # pages that keep polling never go idle, and a wall never gets ready: checked below
            pass
        status = response.status if response is not None else 0
        try:
            title, text = page.title(), page.inner_text("body", timeout=2_000)
        except Exception:
            title, text = "", ""
        try:
            has_frame = bool(page.query_selector(CHALLENGE_FRAMES))
        except Exception:  # navigating
            has_frame = False
        self.log.append(("OPEN", url, status))
        if status in (403, 429) or looks_like_challenge(title, text, has_frame):
            why = f"HTTP {status}" if status in (403, 429) else ("a challenge frame" if has_frame else f"a challenge page ({title[:60]!r})")
            self.blocked[web.host_of(url)] = why
            raise Challenge(f"{url}: {why}; stopped there, not solved")
        if status >= 400:
            raise PortalError(f"{url}: HTTP {status}")
        if not reached:
            raise PortalError(f"{url}: the page did not reach {until_url} within {ready_ms // 1000} s (it is at {page.url[:120]})")
        try:
            html = page.content()
        except Exception:  # still navigating
            html = ""
        return Visit(url, page.url, status, title, html)

    def evaluate(self, script: str, arg=None):
        """Run a script in the open page (reading its state; it sends nothing of ours)."""
        def run():
            if self._page is None:
                raise PortalError("no page open: call open() first")
            return self._page.evaluate(script, arg)
        return self._call(run)

    # -------------------------------------------------------------------------------- Fetch: requests from the page
    def get(self, url: str, use_cache: bool = True) -> web.Response:
        return self.request("GET", url, use_cache=use_cache)

    def post(self, url: str, data: dict | None = None, json_body: dict | None = None, headers: dict | None = None,
             use_cache: bool = True) -> web.Response:
        return self.request("POST", url, data=data, json_body=json_body, headers=headers, use_cache=use_cache)

    def _key(self, method: str, url: str, data, json_body, headers) -> Path:
        """The same key ddwb.deep.web.Http uses, so the two transports share one cache."""
        payload = json.dumps({"data": data, "json": json_body, "headers": headers}, sort_keys=True) if method != "GET" else ""
        return self.cache / hashlib.sha1((url if method == "GET" else f"{method} {url} {payload}").encode()).hexdigest()

    def cached(self, method: str, url: str, data: dict | None = None, json_body: dict | None = None,
               headers: dict | None = None, fresh_since: str | None = None) -> web.Response | None:
        """The cached answer, if there is one (fetched at or after `fresh_since`, when given); nothing is started."""
        key = self._key(method, url, data, json_body, headers)
        if not (key.with_suffix(".json").exists() and key.with_suffix(".bin").exists()):
            return None
        meta = json.loads(key.with_suffix(".json").read_text())
        if fresh_since and (meta.get("fetched_at") or "")[:19] < fresh_since[:19]:
            return None
        return web.Response(**meta, body=key.with_suffix(".bin").read_bytes(), from_cache=True)

    def request(self, method: str, url: str, data: dict | None = None, json_body: dict | None = None,
                headers: dict | None = None, use_cache: bool = True, fresh_since: str | None = None) -> web.Response:
        key = self._key(method, url, data, json_body, headers)
        hit = self.cached(method, url, data, json_body, headers, fresh_since) if use_cache else None
        if hit is not None:
            return hit
        out = self._call(self._request, method, url, data, json_body, headers)
        if out.status is not None and not out.bot_wall and not out.error and 200 <= out.status < 400:
            self.cache.mkdir(parents=True, exist_ok=True)
            key.with_suffix(".bin").write_bytes(out.body)
            meta = {k: getattr(out, k) for k in ("url", "final_url", "status", "content_type", "error", "elapsed_ms", "bot_wall", "fetched_at")}
            key.with_suffix(".json").write_text(json.dumps(meta))
        return out

    def _tab_get(self, url: str, host: str) -> web.Response:
        """A link on another site, opened in a new tab as a visitor would (a file shows in the browser's viewer)."""
        self._start()
        self._pace(host)
        self.requests += 1
        out, started, tab, challenged = web.Response(url), time.monotonic(), self._context.new_page(), False
        try:
            response = tab.goto(url, wait_until="load")
            if response is not None:
                out.status, out.final_url, out.content_type = response.status, response.url, response.headers.get("content-type")
                out.body = response.body()
                if "html" in (out.content_type or ""):  # a page: judged as it shows, like open()
                    try:
                        tab.wait_for_load_state("networkidle", timeout=self.settle_ms)
                    except Exception:
                        pass
                    challenged = looks_like_challenge(tab.title(), tab.inner_text("body", timeout=2_000),
                                                      bool(tab.query_selector(CHALLENGE_FRAMES)))
                elif out.body.lstrip()[:1] == b"<":
                    # the browser's file viewer answered instead of the file: ask for the file from its own page
                    self._pace(host)
                    self.requests += 1
                    got = tab.evaluate(_FETCH, [url, "GET", None, {}])
                    out.status, out.content_type, out.body = int(got["status"]), got["type"], base64.b64decode(got["body"])
        except Exception as error:
            out.error = f"{type(error).__name__}: {str(error).splitlines()[0][:200]}"
        finally:
            tab.close()
        out.elapsed_ms = int((time.monotonic() - started) * 1000)
        self.log.append(("GET", url, out.error or out.status))
        if out.error is None and (out.status in (403, 429) or challenged):
            out.bot_wall = True
            self.blocked[host] = f"HTTP {out.status}" if out.status in (403, 429) else "a challenge page"
        return out

    def _request(self, method, url, data, json_body, headers) -> web.Response:
        self._start()
        host = web.host_of(url)
        if host in self.blocked:
            return web.Response(url, error=f"host stopped earlier in this run: {self.blocked[host]}", bot_wall=True)
        here = urlsplit(self.url)
        there = urlsplit(url)
        if self._page is None or (here.scheme, here.netloc) != (there.scheme, there.netloc):
            if method != "GET":
                return web.Response(url, error=f"open a page on {there.scheme}://{there.netloc} first: requests are sent from it")
            return self._tab_get(url, host)
        sent = dict(headers or {})
        body = None
        if json_body is not None:
            body = json.dumps(json_body)
            sent.setdefault("Content-Type", "application/json")
        elif data is not None:
            body = urlencode(data)
            sent.setdefault("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
        out = web.Response(url)
        for wait in (0.0, *RETRY_WAITS):
            if wait:
                time.sleep(wait)
            self._pace(host)
            self.requests += 1
            started = time.monotonic()
            out = web.Response(url)
            try:
                got = self._page.evaluate(_FETCH, [url, method, body, sent])
                out.status, out.final_url, out.content_type = int(got["status"]), got["url"], got["type"]
                out.body = base64.b64decode(got["body"])
            except Exception as error:  # the page's fetch failed (network, navigation)
                out.error = f"{type(error).__name__}: {str(error).splitlines()[0][:200]}"
            out.elapsed_ms = int((time.monotonic() - started) * 1000)
            self.log.append((method, url, out.error or out.status))
            if out.error is None and walled(out.status, out.content_type, out.body):
                out.bot_wall = True
                self.blocked[host] = f"HTTP {out.status}" if out.status in (403, 429) else "a block or challenge page"
                return out
            if not (out.error or (out.status or 0) >= 500):
                return out
        return out


# ------------------------------------------------------------------------------------------- plain HTTP, then the browser
def never_answered(r: web.Response) -> bool:
    """A host that took the connection and never answered (IQM2's hosts, to a script), as opposed to one that is down
    (refused, no such name, a connect timeout): only the first is worth a browser."""
    return bool(r.error) and r.error.startswith("ReadTimeout")


def front_page(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/"


class Fallback:
    """The Fetch of a portal adapter that has no browser path of its own (IQM2, eScribe, every plain-HTTP adapter;
    BoardDocs and Simbli keep theirs): plain HTTP, until a host walls it (a 403, 429 or block page) or never answers it;
    from then on that host is read through the browser, its front page opened first as a visitor arrives, and every
    request after goes from that page. A wall in the browser stops there (bot_wall set, never solved). Without the
    browser tier (Playwright missing, or OpenClaw's browser not running) the plain answer is returned as it was.
    `http=None` reads every host through the browser (a link the map recorded as read through one). `remember`: where
    hosts moved to the browser are kept across processes (nav's HostClock: browser_only, needs_browser), so a later tool
    call does not wait out the same timeout again."""

    def __init__(self, http=None, browser: Browser | None = None, remember=None) -> None:
        self.http, self.browser, self.remember = http, browser, remember
        self.moved: dict[str, str] = {}  # host -> why plain HTTP was given up there
        self._opened: set[str] = set()

    @property
    def sent(self) -> int:
        """Requests sent, plain and through the browser (the plain Fetch's count, when it keeps one)."""
        return getattr(self.http, "sent", 0)

    def via(self, url: str) -> str:
        """How this URL's host is read now: "browser" or "http"."""
        host = web.host_of(url)
        if self.http is None or host in self.moved:
            return "browser"
        why = self.remember.browser_only(host) if self.remember is not None else None
        if why:
            self.moved[host] = why
        return "browser" if why else "http"

    def get(self, url: str, use_cache: bool = True) -> web.Response:
        return self.request("GET", url, use_cache=use_cache)

    def post(self, url: str, data: dict | None = None, json_body: dict | None = None, headers: dict | None = None,
             use_cache: bool = True) -> web.Response:
        return self.request("POST", url, data=data, json_body=json_body, headers=headers, use_cache=use_cache)

    def request(self, method: str, url: str, data: dict | None = None, json_body: dict | None = None,
                headers: dict | None = None, use_cache: bool = True) -> web.Response:
        host, plain = web.host_of(url), None
        if self.via(url) == "http":
            plain = (self.http.get(url, use_cache=use_cache) if method == "GET" else
                     self.http.post(url, data=data, json_body=json_body, headers=headers, use_cache=use_cache))
            if not (plain.bot_wall or never_answered(plain)):
                return plain
            self.moved[host] = f"HTTP {plain.status}" if plain.bot_wall and plain.status else (plain.error or "a bot wall")[:80]
            if self.remember is not None:
                self.remember.needs_browser(host, self.moved[host])
        if self.browser is None:
            self.browser = Browser(clock=self.remember if callable(getattr(self.remember, "wait", None)) else None)
        since = self.http.since() if callable(getattr(self.http, "since", None)) else None  # lists read fresh once per run
        hit = self.browser.cached(method, url, data, json_body, headers, since) if use_cache else None
        if hit is not None:  # answered before: no browser started
            return hit
        try:
            if host not in self._opened:
                self._opened.add(host)
                try:
                    self.browser.open(front_page(url))
                except (Challenge, NoBrowser):
                    raise
                except PortalError:  # the front page failed (not a wall): the request is sent anyway
                    pass
            got = self.browser.request(method, url, data=data, json_body=json_body, headers=headers, use_cache=use_cache,
                                       fresh_since=since)
        except Challenge as error:
            return web.Response(url, error=str(error), bot_wall=True)
        except NoBrowser as error:  # no browser tier here: the plain answer stands
            return plain if plain is not None else web.Response(url, error=str(error))
        if hasattr(self.http, "sent"):  # the run's count of requests sent (the map's runaway guard) includes these
            self.http.sent += not got.from_cache
        return got

    def close(self) -> None:
        if self.browser is not None:
            self.browser.close()
