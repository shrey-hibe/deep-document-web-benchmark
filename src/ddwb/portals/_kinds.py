"""What a meeting, a board and a document are, from the words a portal uses: shared by the Streamline, eScribe,
PrimeGov and NovusAgenda adapters.

- meeting_kind(title, board): one of base.MEETING_KINDS
- governing(board): True for the body's governing board (council, board of directors, board of education, ...), False for
  committees, commissions and advisory bodies, None when the name does not say
- board_name(title): a meeting type with the meeting words taken off ("City Council Regular Meeting" -> "City Council")
- document_kind(title): one of base.KINDS from a document's label or file name
- dates_named(text): the dates a title or file name names ("Minutes September 30 2026", "2025.12.15 Approved Mins")
- Documents: a list of Document that also carries notes, so documents() can say what is missing (no minutes posted)
"""

from __future__ import annotations

import re
from datetime import date

from .base import Document

_COMMITTEE = re.compile(r"\b(sub-?)?committee\b|\badvisory\b|\btask force\b|\bwork ?group\b|\bworking group\b|\bcaucus\b",
                        re.I)
_GOVERNING = re.compile(
    r"\b(city|town|village|county|borough|township|parish)\s+(council|commission|board)\b|\bcouncil\b|"
    r"\bboard of (supervisors|directors|trustees|education|commissioners|aldermen|selectmen|fire commissioners|governors)\b|"
    r"\bcommissioners'? court\b|\bschool board\b|\bselect ?board\b|\bgoverning board\b|\bboard of the\b|"
    r"^(the )?board( meetings?)?$|^board (meeting|of directors)|\bdistrict board\b|\bfire board\b|\bwater board\b|\blibrary board\b",
    re.I)
_OTHER_BODY = re.compile(r"\bcommission\b|\bauthority\b|\bagency\b|\bboard of (appeals|adjustment|review|ethics)\b|"
                         r"\bdesign review\b|\bplanning\b|\bzoning\b|\bpersonnel board\b|\bpension\b|\bretirement\b", re.I)


def governing(board: str) -> bool | None:
    b = (board or "").strip()
    if not b:
        return None
    if _COMMITTEE.search(b):
        return False
    if _GOVERNING.search(b):
        return True
    if _OTHER_BODY.search(b):
        return False
    return None


_SPECIAL = re.compile(r"\bspecial\b|\bemergency\b", re.I)
_WORK = re.compile(r"\bwork ?shop\b|\bwork session\b|\bstudy session\b|\bretreat\b|\bplanning session\b|\bstrategic planning\b|"
                   r"\bbudget (hearing|session|workshop)\b|\bgoal setting\b", re.I)
_REGULAR = re.compile(r"\bregular\b|\bmeeting\b|\bsession\b", re.I)


def meeting_kind(title: str, board: str = "") -> str:
    text = f"{title or ''} {board or ''}"
    if _COMMITTEE.search(text) or governing(board) is False:
        return "committee"
    if _SPECIAL.search(title or ""):
        return "special"
    if _WORK.search(title or ""):
        return "work_session"
    if _REGULAR.search(title or "") or governing(board):
        return "regular"
    return "other"


_MEETING_WORDS = re.compile(
    r"\b(cancell?ation notice of( an?)?|notice of( an?)?|cancell?ed|cancell?ation( notice)?|postponed|rescheduled|"
    r"regular|special|adjourned|joint|emergency|closed session|open session|executive session|work ?shop|work session|"
    r"study session|meetings?|session|notice|agenda|amended|revised)\b", re.I)


def board_name(title: str) -> str:
    """The board a meeting type names: the meeting words, times and dates taken off. "" when nothing is left (a
    "Regular Meeting" names no board)."""
    t = re.sub(r"@.*$|\b\d{1,2}(:\d{2})?\s*[ap]\.?\s?m\.?.*$", "", title or "", flags=re.I)  # "Board Meeting @ 1:00 p.m."
    t = re.sub(r"\b\d{1,4}[-/.]\d{1,2}[-/.]\d{2,4}\b", "", t)
    t = _MEETING_WORDS.sub(" ", t)
    t = re.sub(r"\s*[-–—:|,(]+\s*$|^\s*[-–—:|,)]+\s*", "", re.sub(r"\s+", " ", t)).strip(" -–—:|,()")
    return re.sub(r"\s+", " ", t).strip()


_VIDEO = re.compile(r"\bvideo\b|youtube|vimeo|\bstream\b|\bwatch\b|swagit|granicus\.com/MediaPlayer|ISIStandAlonePlayer", re.I)
_CANCEL = re.compile(r"cancel|postpon|reschedul", re.I)
_MINUTES = re.compile(r"\bmin(ute)?s\b|\bminutes\b|\bminute\b|\brecap\b|\bmin\.", re.I)
_PACKET = re.compile(r"packet|package|\bagenda book|board book|supporting|backup|back-up|staff report|supplement", re.I)
_AGENDA = re.compile(r"agenda", re.I)
_NOTICE = re.compile(r"\bnotice\b|\bhearing\b|\bnotification\b", re.I)


def document_kind(title: str) -> str:
    t = title or ""
    if _VIDEO.search(t):
        return "video"
    if _CANCEL.search(t):
        return "notice"
    if _MINUTES.search(t):
        return "minutes"
    if _PACKET.search(t):
        return "packet"
    if _AGENDA.search(t):
        return "agenda"
    if _NOTICE.search(t):
        return "notice"
    return "other"


_MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1)}
_MONTH_WORD = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_NAMED = re.compile(rf"\b{_MONTH_WORD}\.?[\s+_-]*(\d{{1,2}})(?:st|nd|rd|th)?[,\s+_-]*(\d{{4}})\b", re.I)
_YMD = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_./ ](\d{1,2})[-_./ ](\d{1,2})(?!\d)")
_YMD_TIGHT = re.compile(r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})(?!\d)")
_MDY = re.compile(r"(?<!\d)(\d{1,2})[-_./ ](\d{1,2})[-_./ ](\d{4}|\d{2})(?!\d)")


def _ok(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d) if 1990 <= y <= 2100 else None
    except ValueError:
        return None


def dates_named(text: str) -> set[date]:
    """Every date a title or file name names, in the usual US spellings (file names with + or _ for spaces too)."""
    t = (text or "").replace("%2C", ",").replace("+", " ")
    found: set[date] = set()
    for mon, d, y in _NAMED.findall(t):
        found.add(_ok(int(y), _MONTHS[mon[:3].lower()], int(d)))
    for y, m, d in _YMD.findall(t) + _YMD_TIGHT.findall(t):
        found.add(_ok(int(y), int(m), int(d)))
    for m, d, y in _MDY.findall(t):
        found.add(_ok(int(y) + (2000 if len(y) == 2 else 0), int(m), int(d)))
    found.discard(None)
    return found


class Documents(list):
    """A meeting's documents (a plain list of Document) with notes on what the portal did not post or could not be read:
    `docs.notes` -> ["no minutes posted for this meeting: only an agenda"]."""

    def __init__(self, docs: list[Document] | None = None, notes: list[str] | None = None) -> None:
        super().__init__(docs or [])
        self.notes: list[str] = list(notes or [])

    def kinds(self) -> set[str]:
        return {d.kind for d in self}


def missing_note(docs: Documents, portal_says: str = "") -> None:
    """Say plainly when a meeting has no minutes (and what it has instead)."""
    kinds = docs.kinds()
    if "minutes" in kinds:
        return
    has = sorted(k for k in kinds if k != "other") or (["no documents"] if not docs else ["other files only"])
    docs.notes.append(f"no minutes posted for this meeting{portal_says}: only {', '.join(has)}")
