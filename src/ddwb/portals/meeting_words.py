"""Words shared by the Granicus, IQM2 and CivicWeb adapters: dates as portals print them, what kind of meeting a title
names, whether a board is the body's governing board, and what kind of document a link is.

Plain rules, no network. Each returns None (or "other") rather than a guess when the words do not say.
"""

from __future__ import annotations

import re
from datetime import date

MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_MONTH = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_DATES = (
    (re.compile(rf"\b{_MONTH}\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I), lambda m: (m[3], m[1], m[2])),  # Oct 20, 2026
    (re.compile(rf"\b(\d{{1,2}})\s+{_MONTH},?\s+(\d{{4}})\b", re.I), lambda m: (m[3], m[2], m[1])),  # 15 Dec 2025
    (re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"), lambda m: (m[1], m[2], m[3])),  # 2026-10-20
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"), lambda m: (m[3], m[1], m[2])),  # 10/20/2026
    (re.compile(r"\b(\d{1,2})[-.](\d{1,2})[-.](\d{4})\b"), lambda m: (m[3], m[1], m[2])),  # 10-20-2026, 10.20.2026
)


def parse_date(text: str | None) -> date | None:
    """The first date written in the text, in any of the forms the three portals print."""
    text = (text or "").replace("\xa0", " ")
    found = []
    for pattern, parts in _DATES:
        for m in pattern.finditer(text):
            y, mo, d = parts(m)
            month = MONTHS.get(mo[:3].lower()) if not str(mo).isdigit() else int(mo)
            try:
                found.append((m.start(), date(int(y), int(month or 0), int(d))))
            except (TypeError, ValueError):
                continue
            break
    return min(found)[1] if found else None


GOVERNING = re.compile(
    r"\b(city|town|village|borough|common|county|metro(?:politan)?)?\s*council\b|board of (county |village |town |city )?"
    r"(supervisors|commissioners|trustees|aldermen|selectmen|directors|education|estimate|legislators|chosen freeholders)|"
    r"\bcommissioners'? court\b|\b(county|city|village|town|borough) commission\b|\bassembly\b|\bselect ?board\b|"
    r"\btownship (board|committee)\b|\bfiscal court\b|\blegislature\b|\bboard of mayor\b|\bcommittee of the whole\b|"
    r"\bboard of port commissioners\b|\bmayor and council\b|\bcouncil meeting\b", re.I)
NOT_GOVERNING = re.compile(
    r"committee|planning|zoning|p ?& ?z\b|advisory|historic|parks|recreation|design review|board of adjustment|appeals|"
    r"architectur|foundation|retirement|pension|civil service|library|authority|task force|airport|housing|"
    r"redevelopment|development (authority|corporation|board)|downtown|economic|arts|ethics|personnel|youth|"
    r"neighborhood|code (enforcement|compliance)|magistrate|hearing (officer|examiner)|tree|beautification|"
    r"environmental|human (rights|services)|transportation|traffic|cemetery|utilit|water|sewer|board of review|"
    r"equalization|canvass|election|technical|staff|training|community|commission on|senior|veterans", re.I)


def is_governing(board: str | None) -> bool | None:
    """True for the body's own governing board (council, commissioners, supervisors, trustees, assembly, board of
    education), False for committees and other boards, None when the name does not say. Whichever is named first wins
    ("City Council Joint Workshop with the School Committee" is the council's; "Planning Commission and City Council" is
    the commission's). A committee of the whole is the governing board meeting as a committee."""
    name = board or ""
    if re.search(r"committee of the whole", name, re.I):
        return True
    gov, other = GOVERNING.search(name), NOT_GOVERNING.search(name)
    if gov and (not other or gov.start() < other.start()):
        return True
    return False if other else None


def meeting_kind(*texts: str | None, governing: bool | None = None) -> str:
    """regular, special, work_session, committee or other, from the meeting's title, type or board name."""
    blob = " ".join(t for t in texts if t)
    if re.search(r"\b(special|emergency|called|adjourned)\b", blob, re.I):
        return "special"
    if re.search(r"work ?session|workshop|study session|retreat|budget (session|hearing|workshop)|strategic planning", blob, re.I):
        return "work_session"
    if governing is False or re.search(r"committee|commission|board of|task force|council of governments", blob, re.I) \
            and governing is not True:
        return "committee"
    if re.search(r"\bregular\b", blob, re.I) or governing:
        return "regular"
    return "other"


def doc_kind(text: str | None, url: str = "") -> str:
    """agenda, packet, minutes, video, notice or other, from a link's text (and its address when the text is bare)."""
    t = f"{text or ''}".lower()
    if re.search(r"minute|summary of action|action summary|journal|proceedings", t):
        return "minutes"
    if re.search(r"packet|package|full agenda|supporting|backup|back-up", t):
        return "packet"
    if re.search(r"agenda", t):
        return "agenda"
    if re.search(r"video|audio|mp3|mp4|recording|watch|listen", t):
        return "video"
    if re.search(r"notice|cancel", t):
        return "notice"
    u = url.lower()
    if re.search(r"minutes", u):
        return "minutes"
    if re.search(r"\.(mp4|mp3|m3u8)\b", u):
        return "video"
    return "other"


MONTH = r"(jan(uary)?|feb(ruary)?|mar(ch)?|apr(il)?|may|june?|july?|aug(ust)?|sept?(ember)?|oct(ober)?|nov(ember)?|dec(ember)?)"


def board_from(text: str) -> str:
    """The board a row or category names: "Board of Supervisors Special Meetings" -> "Board of Supervisors",
    "ASSEMBLY SPECIAL MEETING on 2026-10-20 4:00 PM" -> "ASSEMBLY", "2026 City Council" -> "City Council"."""
    t = re.sub(r"\bon \d{4}-\d{1,2}-\d{1,2}.*$", "", text or "", flags=re.I)
    t = re.split(r"\s+[-–—:|]\s+", t)[0]
    t = re.sub(r"^\s*(19|20)\d\d\s+", "", t)
    t = re.sub(rf"\b{MONTH}\.?\s+\d{{1,2}}(st|nd|rd|th)?,?\s+\d{{4}}\b|\b\d{{1,4}}[-/.]\d{{1,2}}[-/.]\d{{2,4}}\b|\b\d{{6,8}}\b|"
               r"\b\d{1,2}:\d\d\s*([ap]\.?m\.?)?|\btrimmed video\b|\bvideo\b", " ", t, flags=re.I)
    t = re.sub(r"\b(regular|special|adjourned|emergency|annual|meetings?|sessions?|work ?sessions?|workshops?|"
               r"cancell?ed|rescheduled|postponed|amended|revised|joint)\b", " ", t, flags=re.I)
    t = re.sub(r"\(\s*\)", " ", t)
    return clean(t).strip(" -,")


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()
