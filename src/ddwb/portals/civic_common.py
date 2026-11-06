"""Small helpers shared by the Legistar, CivicClerk and AgendaCenter adapters: polite retries, and what a board's name
says about the meeting (its kind, and whether the board is the body's governing board).

Retries: ddwb.deep.web.Http paces each host and stops a host at a bot wall (403, 429, a challenge page) but never
retries. These adapters retry a 5xx or a dropped connection twice, after BACKOFF seconds, and never retry a bot wall.

Names: CivicClerk and AgendaCenter do not say which board governs; Legistar says it only through its body types. These
rules read the board's name (and the meeting's title): a council, commission of the county or city, board of
supervisors, commissioners, aldermen, trustees, selectmen... governs; a committee, an advisory board, a planning
commission, an authority... does not. A name that says neither gives None, never a guess.
"""

from __future__ import annotations

import re
import time

from ..deep.web import Response

BACKOFF = (10.0, 30.0)  # seconds to wait before the second and third try after a 5xx or a dropped connection


def retryable(resp: Response) -> bool:
    if resp.bot_wall:
        return False
    return (resp.status is None and bool(resp.error)) or (resp.status or 0) >= 500


def fetch(http, url: str, *, data: dict | None = None, headers: dict | None = None, post: bool = False,
          use_cache: bool = True) -> Response:
    """One GET (or form POST), tried again after a pause on a 5xx or a dropped connection, never around a bot wall.
    A cached 5xx is not trusted: the retry skips the cache."""
    def once(cache: bool) -> Response:
        if post:
            return http.post(url, data=data, headers=headers, use_cache=cache)
        return http.get(url, use_cache=cache)

    resp = once(use_cache)
    for wait in BACKOFF:
        if not retryable(resp):
            break
        if not resp.from_cache:
            time.sleep(wait)
        resp = once(False)
    return resp


def refusal(resp: Response) -> str:
    """Why a response is not usable, in a few words for a note or an error."""
    if resp.bot_wall:
        return f"bot wall ({f'HTTP {resp.status}' if resp.status in (403, 429) else resp.error or 'challenge page'}); stopped, not solved"
    if resp.error:
        return resp.error[:200]
    snippet = re.sub(r"\s+", " ", resp.body[:200].decode("utf-8", errors="replace")).strip()
    return f"HTTP {resp.status}" + (f": {snippet}" if snippet else "")


# ------------------------------------------------------------------------------------------- board names
_NOT_GOVERNING = re.compile(
    r"committee|subcommittee|advisory|authority|agency|task ?force|planning|zoning|adjustment|appeals|library|pension|"
    r"retirement|historic|preservation|\bparks?\b|recreation|housing|redevelopment|development|ethics|civil service|"
    r"personnel|audit|\btrees?\b|\barts?\b|cultural|aging|status of women|disabilit|mosquito|council on|commission on|"
    r"museum|cemetery|airport|\bport\b|transit|utilit|water|sewer|\bfire\b|police|"
    r"health|human|youth|senior|veteran|tourism|convention|beautification|design review|review board|oversight|"
    r"successor|foundation|corporation|value adjustment|canvass|election|liquor|licens|pension|employees|trust fund|"
    r"\bmpo\b|metropolitan planning|code enforcement|magistrate|hearing officer|examiner|bid opening|"
    r"(?<!county )(?<!city )(?<!town )(?<!village )(?<!borough )(?<!township )(?<!parish )\bcommission\b", re.I)
_GOVERNING = re.compile(
    r"\b(city|town|village|borough|township|county|parish|common|metro|metropolitan)\s+(council|commission|board|legislature)\b|"
    r"\bboard of (county |city |town |village |parish )?(commissioners|supervisors|aldermen|alderpersons|trustees|selectmen|"
    r"chosen freeholders|legislators|directors|mayor and aldermen|education)\b|"
    r"\bselect ?board\b|\bboard of selectmen\b|\b(fiscal|commissioners'?) court\b|\bcounty legislature\b|"
    r"\bcity commission\b|^\W*(the\s+)?council\b|\bcouncil meeting\b|\bcommissioners\b|\bpolice jury\b|\bboard of mayor\b|\btown meeting\b", re.I)


def governing_of(name: str) -> bool | None:
    """Whether a board's name is the body's governing board: True, False, or None when the name does not say."""
    name = re.sub(r"[-_]+", " ", name or "")
    if _NOT_GOVERNING.search(name):
        return False
    if _GOVERNING.search(name):
        return True
    return None


_SPECIAL = re.compile(r"\bspecial\b|emergency meeting|budget hearing|public hearing", re.I)
_WORK = re.compile(r"work ?session|workshop|study session|retreat|goal setting|strategic planning|planning session", re.I)


def kind_of(*texts: str, governing: bool | None = None) -> str:
    """The meeting's kind (base.MEETING_KINDS) from its own title and its board's name: special or work session when
    they say so; else the governing board's meeting is regular and any other board's a committee's."""
    text = re.sub(r"[-_]+", " ", " ".join(t for t in texts if t))
    if _SPECIAL.search(text):
        return "special"
    if _WORK.search(text):
        return "work_session"
    if governing is True:
        return "regular"
    if governing is False or re.search(r"committee", text, re.I):
        return "committee"
    return "regular" if re.search(r"\bregular\b", text, re.I) else "other"


def document_kind(label: str) -> str:
    """A document's kind (base.KINDS) from the portal's own label for it (file type, column, link text)."""
    low = (label or "").lower()
    if "minute" in low:
        return "minutes"
    if "packet" in low:
        return "packet"
    if "agenda" in low:
        return "agenda"
    if "notice" in low:
        return "notice"
    if re.search(r"video|media|audio|recording", low):
        return "video"
    return "other"
