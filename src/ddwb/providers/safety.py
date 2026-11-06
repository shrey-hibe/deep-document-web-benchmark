"""How the provider layer touches the web: our User-Agent on every request we make, no key in any log or error, and
scrape never runs against a host already stopped for a bot wall nor passes on a page that is one."""

from __future__ import annotations

import re
from typing import Protocol, Sequence
from urllib.parse import urlsplit

USER_AGENT = "public-signals-research/0.1"  # no name, email or handle
# The live check's bot-wall signs (ddwb.fetch.live_check.BOT_WALL) plus the challenge pages' own markers.
BOT_WALL = re.compile(r"just a moment|attention required|cf-chl|captcha|are you a robot|access denied|request unsuccessful|"
                      r"verify you are human|ddos protection|challenge-platform|incapsula|imperva", re.I)

_SECRETISH = re.compile(r"(?i)(bearer\s+|x-api-key[\"']?\s*[:=]\s*[\"']?|api[_-]?key[\"']?\s*[:=]\s*[\"']?|exaApiKey=)"
                        r"[^\s\"',}&]+")
_TOKENISH = re.compile(r"\b(?:sk|pk|rk|key|tok|vck|fc)[-_][A-Za-z0-9_-]{16,}")


def host_of(url: str) -> str:
    """The host as the deep state store keys it (ddwb.deep.web.host_of): lower case, no www."""
    return (urlsplit(url or "").hostname or "").lower().removeprefix("www.")


def looks_like_wall(text: str) -> bool:
    return bool(BOT_WALL.search((text or "")[:5000]))


def page_is_wall(page: dict) -> str | None:
    """Why a scraped page is a bot wall, or None. 403 and 429 count, as they do for our own GETs."""
    if page.get("status") in (403, 429):
        return f"HTTP {page['status']} (scrape)"
    if looks_like_wall((page.get("markdown") or "")[:3000]) or looks_like_wall(str(page.get("error") or "")):
        return "bot-check page (scrape)"
    return None


def redact(text: str, secrets: Sequence[str] = ()) -> str:
    """Strip bearer tokens, key parameters, key-shaped tokens and every secret value used by the call."""
    text = _SECRETISH.sub(r"\1[redacted]", text or "")
    text = _TOKENISH.sub("[redacted]", text)
    for s in secrets:
        if s and len(s) >= 6:
            text = text.replace(s, "[redacted]")
    return text


class HostGuard(Protocol):
    """The deep state store's host clock (ddwb.deep.state.HostClock) or anything shaped like it."""

    def blocked(self, host: str) -> str | None: ...

    def block(self, host: str, why: str) -> None: ...


def default_guard() -> HostGuard:
    """The shared host clock (data/nav/shared.sqlite): bot walls seen by any tool process, in any run."""
    from ..deep.state import HostClock

    return HostClock()
