"""Rules 6 and 7: the meeting's attendance from its roll call, and the people named in each item (moved, seconded,
presented, appointed, hired, resigned, spoke), with names and roles as written. People named in public records are in
scope (decision #122). Nothing here matches a person across documents."""

from __future__ import annotations

import re

from ddwb.deep import text as dtext

from .model import Item, Person
from .outcome import HONORIFIC, NAME, NOT_NAME, ROLE_NAME, ROLE_WORD, clean_name, plain

ORG_WORD = re.compile(r"\b(?:Department|Office|Division|Services|Committee|Commission|District|County|City|Board|Council|"
                      r"Library|School|Schools|Center|Authority|Agency|Inc|LLC|Company|Corporation|Foundation|University|"
                      r"College|Association|Group|Bank|Fund|Program|State|Ohio|California|Colorado)\b")

PROCEDURAL = [  # (tag, pattern on the start of an item's title, after its label)
    ("report out of closed session", None),  # never procedural: a decision reported out (checked first, below)
    ("call to order", r"call(?:ed)?\s+to\s+order|(?:the\s+)?(?:[\w.'’-]+\s+){0,5}(?:was|were)\s+called\s+to\s+order|opening"),
    ("roll call", r"roll\s+call|attendance|members?\s+present|determination\s+of\s+(?:a\s+)?quorum|quorum"),
    ("pledge", r"pledge\s+of\s+allegiance|pledge\b|invocation|moment\s+of\s+silence|salute\s+to\s+the\s+flag"),
    ("agenda", r"(?:approval|adoption)\s+of\s+(?:the\s+)?agenda|corrections?\s+or\s+modifications?\s+to\s+the\s+agenda|"
               r"(?:changes|amendments|additions)\s+to\s+the\s+agenda|agenda\s+(?:approval|changes)"),
    ("minutes", r"(?:approval|adoption|reading)\s+of\s+(?:the\s+)?(?:previous\s+)?minutes|minutes\s+(?:for\s+approval|of\s+(?:the\s+)?previous)|"
                r"approved\s+the\s+minutes"),
    ("public comment", r"(?:closed\s+session\s+)?public\s+(?:comments?|participation|forum|input|hearing\s+of\s+citizens)|"
                       r"citizens?\s+(?:comments?|participation|to\s+be\s+heard)|audience\s+(?:comments?|participation)|"
                       r"comments?\s+from\s+the\s+public|visitors"),
    ("executive session", r"(?:adjourn(?:ed)?\s+to\s+)?(?:executive|closed)\s+sessions?|reconvene|regular\s+session\b|open\s+session\b"),
    ("adjournment", r"adjourn|there\s+being\s+no\s+further\s+business|(?:the\s+)?(?:[\w-]+\s+){0,3}(?:meeting|session)\s+(?:was\s+)?adjourned"),
]
REPORT_OUT = re.compile(r"(?i)\b(?:report(?:ed)?|action\s+taken|announce\w*)\b.{0,40}\b(?:closed|executive)\s+session|"
                        r"\b(?:closed|executive)\s+session\b.{0,40}\b(?:report|action\s+taken)")
_PROC = [(tag, re.compile(rf"(?i)^\W*(?:{p})")) for tag, p in PROCEDURAL if p]


def procedural_tag(title: str) -> str | None:
    """The procedural kind of an item from its title, or None. A report of action taken in closed session is never
    procedural: it is where a settlement or a personnel action is made public."""
    t = re.sub(r"^\W*(?:item\s+)?(?:[\dIVXivx]{1,5}(?:\.\d{1,2})*|[A-Za-z])[.)]?\s+", "", plain(title)).strip()
    if REPORT_OUT.search(t[:120]):
        return None
    for tag, p in _PROC:
        if p.match(t):
            return tag
    return None


# ---------------------------------------------------------------- attendance
MARKERS = re.compile(
    r"(?i)(?P<absent>\b(?:(?:board\s+|council\s*)?members?\s+)?(?:absent|not\s+present|excused)\b)|"
    r"(?P<staff>\bstaff\s+(?:(?:&|and)\s+guests\s+)?(?:present|in\s+attendance)(?:\s+included)?|\b(?:also|others)\s+present|"
    r"\battorneys?\s+present|\badministration\s+present|\bguests?\s+present)|"
    r"(?P<present>\b(?:(?:board|council)\s*)?members?\s+present|\b(?:trustees|directors|commissioners|councilmembers|"
    r"council\s*members|board\s+members)\s+(?:were\s+)?present|\bwere\s+present|\bpresent(?=\s*[:\-–]))")
ENTRY_STOP = re.compile(r"(?:\b[A-Z][A-Z ]{3,}:|(?<![A-Z][a-z])(?<!\b[A-Z])\.\s+(?=[A-Z][a-z]+\s+[a-z]))")


def _split_entries(seg: str) -> list[str]:
    if re.search(r"(?m)^\s*[-•*]\s+", seg):
        return [re.sub(r"^\s*[-•*]\s+", "", ln) for ln in seg.splitlines() if re.match(r"^\s*[-•*]\s+", ln)]
    seg = " ".join(seg.split()).strip(" ;,.")
    commas = lambda x: [re.sub(r"^and\s+", "", e.strip()) for e in re.split(r",\s*", x)]  # noqa: E731
    parts = [e.strip() for e in seg.split(";") if e.strip()]
    if len(parts) < 2:
        return commas(seg)
    out = []
    for part in parts:  # "Marilyn Anderson, President; Carrie Buck, Vice President; ..." (a name, then its role)
        pieces = commas(part)
        out += pieces if all(NAME_ONLY.match(re.sub(r"^and\s+", "", x)) and re.match(HONORIFIC, x) for x in pieces) else [part]
    return out


ROLE_PREFIX = re.compile(rf"^(?P<role>(?:[A-Z][\w/&'’-]*\s+){{0,6}}?(?:{ROLE_WORD}|[A-Z][\w/&-]*(?:Administrator|Officer|"
                         rf"Manager|Director|Chief|Clerk|Attorney|Engineer|Specialist|Coordinator|Superintendent|Paralegal)"
                         rf"(?:\s+I{{1,3}})?|Interim\s+[A-Z][\w-]+(?:\s+[A-Z][\w-]+)*))\s+(?P<name>{NAME})$")
NAME_ONLY = re.compile(rf"^(?:{HONORIFIC}\s+)?[A-Z][a-zA-Z'’\-]+(?:\s+(?:[A-Z]\.|[A-Z][a-zA-Z'’\-]+|de|la|van|von|del|da)){{0,3}}$")


TITLE_TOKEN = re.compile(rf"^(?:{ROLE_WORD}|of|and|the|&|[A-Z][\w/&'’-]*(?:or|er|ist|ant|ent|ian|al|ef|ary|ive|IT))$")


def _entry(e: str, part: str) -> Person | None:
    """One attendance entry: "Name, Role", "Role Name" ("Airport Director Jeremy Patton", "Mayor Pro-tem Dennis
    Spruell") or a name alone ("Mrs. Darrow")."""
    e = re.sub(r"^\s*(?:and\s+)?", "", re.sub(r"\([^)]*\)", "", e)).strip(" .:-–*")
    if not e or len(e) > 120 or re.search(r"\d", e):
        return None
    name, _, role = e.partition(",")
    name, role = name.strip(), role.strip() or None
    toks = name.split()
    if not toks or not all(t[0].isupper() or t in ("of", "and", "the", "&", "de", "la", "van", "von", "del", "da") for t in toks):
        return None
    k = 2
    if len(toks) >= 3 and (re.fullmatch(HONORIFIC, toks[-3]) or re.fullmatch(r"[A-Z]\.", toks[-2])):
        k = 3
    if len(toks) > k:
        prefix = toks[:-k]
        if all(TITLE_TOKEN.match(t) for t in prefix) or len(prefix) >= 2:
            name = " ".join(toks[-k:])
            role = " ".join(prefix) if role is None else f"{' '.join(prefix)}, {role}"
    if not NAME_ONLY.match(name) or any(NOT_NAME.match(w) for w in name.split()) or len(name) < 3 or ORG_WORD.search(name):
        return None
    return Person(name=name, role=role, part=part)


def attendance(text: str) -> list[Person]:
    """Board members present and absent, and staff present, from the text of a roll call or call to order."""
    t = plain(text)
    marks = list(MARKERS.finditer(t))
    out = []
    for i, m in enumerate(marks):
        part = "absent" if m.group("absent") else "staff" if m.group("staff") else "present"
        end = marks[i + 1].start() if i + 1 < len(marks) else len(t)
        seg = t[m.end():end]
        seg = re.sub(r"^[ \t]*(?:were|was|included|including|as\s+follows)?[ \t]*[:\-–]?[ \t]*", "", seg)
        stop = ENTRY_STOP.search(seg)
        seg = seg[:stop.start()] if stop else seg
        for e in _split_entries(seg):
            p = _entry(e, part)
            if p and p.name not in {x.name for x in out}:
                out.append(p)
    return out


# ---------------------------------------------------------------- people in an item
def _split_role(n: str) -> tuple[str, str | None]:
    m = re.match(rf"^(?P<role>{ROLE_WORD})\s+(?P<name>.+)$", n)
    return (m.group("name"), m.group("role")) if m else (n, None)


ROLE_AFTER = r"(?:,\s*(?P<r>(?:[A-Z][\w/&'’-]*|of|and|the|for)(?:\s+(?:[A-Z][\w/&'’-]*|of|and|the|for)){0,7}))?"
ITEM_PEOPLE = [
    ("moved", re.compile(rf"(?i:moved\s+by)\s*:?\W{{0,4}}\s*(?P<n>{ROLE_NAME})")),
    ("moved", re.compile(rf"(?P<n>{ROLE_NAME})\s+(?i:moved\s+(?:to|that|for|approval|adoption|acceptance)|made\s+(?:a|the)\s+motion)\b")),
    ("seconded", re.compile(rf"(?i:seconded(?:\s+by)?)\s*:?\W{{0,4}}\s*(?P<n>{ROLE_NAME})")),
    ("seconded", re.compile(rf"(?P<n>{ROLE_NAME})\s+(?i:seconded)\b(?!\s*(?i:by))")),
    ("presented", re.compile(rf"(?i:presented\s+by|presentation\s+by|report\s+by|staff\s+contact)\s*:?\s*(?P<n>{ROLE_NAME}){ROLE_AFTER}")),
    ("presented", re.compile(rf"(?P<n>{NAME}),\s*(?P<r>[A-Z][\w/&'’ -]{{2,60}}?),\s*(?i:presented|reviewed|provided|gave|reported|"
                             rf"explained|discussed|introduced)\b")),
    ("presented", re.compile(rf"(?P<n>{NAME})\s+(?:from|of)\s+the\s+(?P<r>[A-Z][\w'’ -]{{2,60}}?)\s+(?i:addressed|presented|spoke)\b")),
    ("appointed", re.compile(rf"(?i:appoint(?:ment\s+of|ed|ing|s)?|reappoint(?:ment\s+of|ed|ing|s)?|promoted)\s+(?P<n>{NAME})"
                             rf"(?:\s+(?i:as|to)\s+(?:the\s+)?(?P<r>[A-Z][^,.;\n]{{2,80}}))?")),
    ("appointed", re.compile(rf"(?P<n>{NAME})\s+(?i:was\s+promoted\s+to)\s+(?P<r>[^,.;\n]{{3,80}})")),
    ("hired", re.compile(rf"(?i:hir(?:e|ed|ing)\s+of|employment\s+of|hired)\s+(?P<n>{NAME})(?:\s+(?i:as)\s+(?:the\s+|a\s+|an\s+)?"
                         rf"(?P<r>[^,.;\n]{{3,80}}))?")),
    ("hired", re.compile(rf"(?P<n>{NAME})\s+(?i:is\s+the\s+new|was\s+hired\s+as|has\s+been\s+hired\s+as)\s+(?P<r>[^,.;\n]{{3,80}})")),
    ("resigned", re.compile(rf"(?i:resignation\s+of|retirement\s+of)\s+(?P<n>{NAME})")),
    ("resigned", re.compile(rf"(?P<n>{NAME})\s+(?i:resigned|has\s+resigned|retired|will\s+retire)\b")),
    ("spoke", re.compile(rf"(?m)^\s*[-•]\s*(?P<n>{NAME})(?:\s+(?:re:?|regarding|about|on)\s|\s*$)")),
    ("spoke", re.compile(rf"(?P<n>{NAME}),\s*(?P<r>[A-Z][\w ]{{2,30}}),\s*(?i:stated|spoke|addressed|expressed|commented|asked|"
                         rf"thanked|shared|introduced)\b")),
    ("spoke", re.compile(rf"(?P<n>{NAME})\s+(?i:addressed\s+(?:the\s+)?(?:board|council|commission|trustees)|spoke|commented)\b")),
]


def item_people(text: str) -> list[Person]:
    t = plain(text)
    out, seen = [], set()
    for part, p in ITEM_PEOPLE:
        for m in p.finditer(t):
            raw = clean_name(m.group("n"))
            if not raw or len(raw) < 3:
                continue
            name, role = _split_role(raw)
            if len(name.split()) == 1 and not re.match(HONORIFIC, name) and role is None and part not in ("moved", "seconded"):
                continue  # a single capitalised word is too often not a name
            r = (m.groupdict().get("r") or "").strip() or None
            role = role if r is None else (f"{role}, {r}" if role else r)
            key = (name, part)
            if key in seen or any(NOT_NAME.match(w) for w in name.split()) or ORG_WORD.search(name):
                continue
            seen.add(key)
            out.append(Person(name=name, role=role, part=part))
    return out


def orgs(text: str) -> list[str]:
    return dtext.vendor_candidates(plain(text), limit=12)


def annotate(item: Item) -> None:
    item.amounts = dtext.amounts(item.text)
    item.orgs = orgs(item.text)
    item.people = item_people(item.text)
