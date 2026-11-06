"""Rule 5: the motion and vote lines that decide an item, read from its own text, and the consent motion copied to
every item of its consent section.

An item's outcome starts at the sentence of its first motion or vote mark ("Moved by", "X moved that", "made a
Motion", "Voice Vote", "Motion carried", "Resolution 2025-123 was adopted", "Carried 5-0", "Yes:") and runs to the
end of its last vote mark. The mover, seconder, tally and result are read from the last motion (an item can carry a
motion to suspend the rules and then one to adopt; the last one decides it).
"""

from __future__ import annotations

import re

from .model import Item, Outcome

HONORIFIC = r"(?:Mr|Mrs|Ms|Mx|Dr|Miss)\.?"
ROLE_WORD = (r"(?:Council\s?(?:member|man|woman|or)|Board\s+Member|Trustee|Commissioner|Supervisor|Alderman|Alderwoman|"
             r"Selectman|Selectwoman|Mayor(?:\s+Pro[- ]?[Tt]em)?|(?:Vice[- ])?President|(?:Vice[- ])?Chair(?:man|woman|person)?|"
             r"Clerk|Secretary|Treasurer|Director|Member|Senator|Representative|Superintendent)")
_WORD = r"[A-Z][a-zA-Z'’\-]+"
NAME = rf"(?:{HONORIFIC}\s+)?{_WORD}(?:\s+(?:[A-Z]\.|{_WORD}|de|la|van|von|del|da|di)){{0,3}}"
ROLE_NAME = rf"(?:{ROLE_WORD}\s+)?{NAME}"
NOT_NAME = re.compile(r"^(?:The|This|That|Council|Board|City|County|District|Motion|Consent|Item|Items|Resolution|Ordinance|"
                      r"Approval|Staff|Public|Voice|Vote|Roll|Agenda|Minutes|It|A|An|And|Seconded|Moved|Carried|Aye|Ayes|Yes|"
                      r"No|Nay|Nays|Absent|Abstain|Preferential|Student)$")

MARK = re.compile(
    r"(?i:\bmoved\s+by\b|\bmotion\s+(?:by|of)\s+(?!(?:the\s+)?(?:council|board|commission|trustees|directors)\b)|\bon\s+(?:a\s+)?motion\b|\bit\s+was\s+moved\b|\b(?:a\s+)?motion\s+was\s+made\b|"
    r"\bmade\s+(?:a|the)\s+motion\b|\bmoved\s+(?:to|that|for|approval|adoption|acceptance)\b|\bseconded\s+by\b|"
    r"\bvoice\s+vote\b|\broll\s+call\s+vote\b|\bvote\s+was\s+as\s+follows\b|\bvote\s*:|"
    r"\bmotion\s+(?:carried|passed|failed|approved|died)\b|\bmotion\b[^.\n]{0,60}?\b(?:passed|carried|failed)\b|"
    r"\b(?:a|the)\s+motion\s+to\s+\w+|"
    r"\b(?:motion|resolution|ordinance|items?|consent\s+(?:agenda|calendar)|minutes|it)\b[^.\n]{0,60}?\b(?:was|were)\s+"
    r"(?:duly\s+|unanimously\s+)?(?:adopted|approved|passed|carried|defeated|failed|tabled|postponed|withdrawn|denied|rejected)\b|"
    r"\bcarried\s+\d|\b(?:ayes?|yeas?|yes)\s*:)")
# The last mark of the outcome: a result, a tally, or a row of Yes/No votes
END_MARK = re.compile(r"(?i:\b(?:carried|passed|adopted|approved|failed|defeated|denied|rejected|tabled|postponed|withdrawn)\b"
                      r"(?:\s*[-–:]?\s*(?:voice\s+vote\s*:?\s*)?\d{1,2}\s*[-–]\s*\d{1,2})?|\b\d{1,2}\s*[-–]\s*\d{1,2}\b|"
                      r"(?:\b(?:yes|no|aye|nay|absent|abstain)\b[\s,]*){3,}|\bvote:\s*aye\b)")
MOVER = [re.compile(rf"(?i:moved\s+by)\s*:?\W{{0,4}}\s*(?P<n>{ROLE_NAME})"),
         re.compile(rf"(?P<n>{ROLE_NAME})\s+(?i:moved\s+(?:to|that|for|approval|adoption|acceptance)|made\s+(?:a|the)\s+motion)\b"),
         re.compile(rf"(?i:motion\s+(?:by|of|from))\s+(?P<n>{ROLE_NAME})")]
SECONDER = [re.compile(rf"(?i:seconded(?:\s+by)?)\s*:?\W{{0,4}}\s*(?P<n>{ROLE_NAME})"),
            re.compile(rf"(?P<n>{ROLE_NAME})\s+(?i:seconded)\b(?!\s*(?i:by))")]
RESULT = re.compile(r"(?i)(?<!\bbe )(?<!\bto )\b(adopted|approved|passed|carried|defeated|failed|denied|rejected|tabled|postponed|"
                    r"withdrawn)\b(?!\s+(?:the|a|an|by|that|to)\b)")
RESULT_OF = {"passed": "carried", "carried": "carried", "adopted": "adopted", "approved": "approved", "defeated": "failed",
             "failed": "failed", "denied": "failed", "rejected": "failed", "tabled": "tabled", "postponed": "tabled",
             "withdrawn": "withdrawn"}
TALLY = re.compile(r"(?<![\d/.,-])(\d{1,2})\s*[-–]\s*(\d{1,2})(?![\d/.,-]*\d)")
VOTE_ROW = re.compile(r"(?i)(?:\b(?:yes|no|aye|nay|absent|abstain(?:ed)?)\b[\s,]*){3,}")
LIST_VOTE = re.compile(r"(?i)\b(ayes?|yeas?|yes|nays?|no(?:es)?)\b\s*:?\s*(?:\*\*)?\s*([A-Z][^:\n*]*?)(?=\s*(?:\*\*|\b(?:nays?|no|noes|"
                       r"absent|abstain\w*|carried|motion|preferential|resolution)\b|[.;]\s|$))")
CONSENT = re.compile(r"(?i)\bconsent\s+(?:agenda|calendar|items?)\b")
RANGE = re.compile(r"(?i)\bitems?\s+(?:no\.?\s*)?(\d{1,2}(?:\.\d{1,2})*)\s*(?:through|thru|to|-|–)\s*(\d{1,2}(?:\.\d{1,2})*)")
BLOCK = re.compile(r"(?i)\b(?:as\s+a\s+block|block\s+vote|en\s+bloc|in\s+one\s+motion)\b")
SENTENCE_END = re.compile(r"[.;!?](?=\s)|\n")
NO_STOP = re.compile(r"(?:\b(?:Mr|Mrs|Ms|Mx|Dr|St|No|Jr|Sr|Inc|Co|vs)|\b[A-Z])$")
MARKUP = re.compile(r"\*\*?|</?u>|</?sup>|<[^>]+>")


def plain(text: str) -> str:
    return re.sub(r"[ \t]+", " ", MARKUP.sub("", text or ""))


def _sentence_start(text: str, at: int) -> int:
    """Where the sentence holding position `at` begins: after the last sentence end that is not an abbreviation."""
    start = 0
    for m in SENTENCE_END.finditer(text, 0, at):
        if m.group(0) == "\n" or not NO_STOP.search(text[max(0, m.start() - 4):m.start()]):
            start = m.end()
    return start


def _name(m: re.Match | None) -> str | None:
    if not m:
        return None
    return clean_name(m.group("n")) or None


def clean_name(name: str) -> str:
    """A matched name without the words around it that are not names ("Carrie Buck Aye Leandra" -> "Carrie Buck")."""
    words = name.split()
    while words and NOT_NAME.match(words[0]):
        words.pop(0)
    for i, w in enumerate(words):
        if NOT_NAME.match(w):
            words = words[:i]
            break
    return " ".join(words)


def _last(patterns: list[re.Pattern], text: str, before: int | None = None) -> re.Match | None:
    best = None
    for p in patterns:
        for m in p.finditer(text):
            if (before is None or m.start() < before) and _name(m) and (best is None or m.start() > best.start()):
                best = m
    return best


def _count_names(s: str) -> int:
    s = re.sub(r"\band\b", ",", s)
    return sum(1 for part in s.split(",") if re.search(r"[A-Z][a-z]", part))


def read_outcome(text: str) -> Outcome | None:
    """The outcome written in an item's text, or None when the text holds no motion or vote."""
    t = plain(text)
    first = MARK.search(t)
    if not first:
        return None
    start = _sentence_start(t, first.start())
    ends = list(END_MARK.finditer(t, first.start()))
    end = ends[-1].end() if ends else first.end()
    nxt = SENTENCE_END.search(t, end)
    end = nxt.end() if nxt and nxt.start() - end < 120 else end
    out = t[start:end].strip(" \n*")
    # a trailing "Preferential Student Board Member Vote: Aye" style line stays with the vote
    if len(out) > 1500:
        out = out[:1500]
    region = t[start:end]
    res = list(RESULT.finditer(region))
    last_result = res[-1] if res else None
    cut = last_result.start() if last_result else None
    mover = _last(MOVER, region, cut)
    seconder = _last(SECONDER, region, cut)
    if mover and seconder and seconder.start() < mover.start():
        # "X moved, seconded by Y" puts the seconder after the mover; one before it seconded an earlier motion, unless
        # the mover's sentence names nobody else ("Y seconded" written first is rare)
        seconder = _last(SECONDER, region[mover.start():])
    seconder_name = _name(seconder)
    tallies = list(TALLY.finditer(region))
    ayes = nays = None
    tally = None
    if tallies:
        a, n = int(tallies[-1].group(1)), int(tallies[-1].group(2))
        if a + n <= 30:
            ayes, nays, tally = a, n, f"{a}-{n}"
    if tally is None:
        rows = VOTE_ROW.findall(region)
        if rows:
            words = re.findall(r"(?i)\b(yes|aye|no|nay)\b", " ".join(rows))
            ayes = sum(w.lower() in ("yes", "aye") for w in words)
            nays = sum(w.lower() in ("no", "nay") for w in words)
        else:
            for kind, names in LIST_VOTE.findall(region):
                k = _count_names(names)
                if kind.lower().startswith(("aye", "yea", "yes")):
                    ayes = (ayes or 0) + k
                else:
                    nays = (nays or 0) + k
            if ayes is not None and nays is None:
                nays = 0
        if ayes:
            tally = f"{ayes}-{nays or 0}"
    result = RESULT_OF[last_result.group(1).lower()] if last_result else None
    if result is None and ayes is not None and nays is not None:
        result = "carried" if ayes > nays else "failed"
    if not (_name(mover) or seconder_name or tally or result):
        return None  # a motion named but not decided here ("A Motion of Council confirming ...", an agenda's title)
    return Outcome(text=out, mover=_name(mover), seconder=seconder_name, tally=tally, ayes=ayes, nays=nays, result=result)


def _children(items: list[Item], number: str) -> list[Item]:
    return [it for it in items if it.number and it.number.startswith(number + ".")]


def _is_top(number: str | None) -> bool:
    return bool(number) and "." not in number


def propagate_consent(items: list[Item]) -> int:
    """A consent section is decided by one motion: written under the section itself, or after its last item. Copy it to
    every item of the section that has no outcome of its own (marked consent). Consecutive consent sections ("CONSENT
    CALENDAR", "CONSENT CALENDAR - BUSINESS SERVICES", ...) share the motion. Returns how many items received it."""
    copied = 0
    i = 0
    while i < len(items):
        it = items[i]
        if not (it.number and CONSENT.search(it.title) and it.kind != "attachment"):
            i += 1
            continue
        depth = it.number.count(".")
        block = [it]
        j = i + 1
        while j < len(items):  # the section's own items, and any consent sections right after it at the same depth
            nxt = items[j]
            sections = tuple(b.number + "." for b in block if b.number.count(".") == depth)
            if nxt.number and (nxt.number.startswith(sections) or nxt.number.count(".") == depth and CONSENT.search(nxt.title)):
                block.append(nxt)
            elif nxt.number and nxt.kind != "attachment":
                break  # an unnumbered part (an adjournment line) or an attachment does not end the block
            j += 1
        members = [b for b in block if b.kind != "attachment"]
        heads = [b for b in members if b.number.count(".") == depth]
        leaves = [b for b in members if b.number.count(".") > depth]
        motion, source = None, None
        for h in heads:
            if h.outcome and not h.outcome.consent:
                motion, source = h.outcome, h
                break
        if motion is None and leaves:
            last = leaves[-1]
            others = [b for b in leaves[:-1] if b.outcome]
            if last.outcome and (CONSENT.search(last.outcome.text) or not others):
                motion, source = last.outcome, last
        if motion is not None:
            motion.consent = True
            for b in members:
                if b is not source and b.outcome is None:
                    b.outcome = Outcome(**{**motion.__dict__, "consent": True, "from_item": source.number})
                    copied += 1
        i = j if j > i + 1 else i + 1
    return copied


def _key(number: str) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", number)]


def propagate_ranges(items: list[Item]) -> int:
    """'Approved Items 12.2 through 12.4 as a block vote' in item 12.4: its vote decides 12.2 and 12.3 too."""
    copied = 0
    for idx, it in enumerate(items):
        if not it.outcome or it.outcome.consent:
            continue
        ranges = RANGE.findall(plain(it.text))
        if not ranges and BLOCK.search(plain(it.text)) and it.number and "." in it.number:
            # "Approved ... textbooks as a block vote" after the last item of a section: the items before it in the
            # same section that have no outcome of their own
            parent = it.number.rsplit(".", 1)[0]
            for other in reversed(items[:idx]):
                if other.kind == "attachment":
                    continue
                if not (other.number and other.number.rsplit(".", 1)[0] == parent and "." in other.number) or other.outcome:
                    break
                other.outcome = Outcome(**{**it.outcome.__dict__, "from_item": it.number})
                copied += 1
        for a, b in ranges:
            lo, hi = _key(a), _key(b)
            if len(lo) != len(hi) or lo[:-1] != hi[:-1] or lo[-1] >= hi[-1]:
                continue
            for other in items:
                if other is it or other.outcome or not other.number or other.kind == "attachment":
                    continue
                k = _key(other.number)
                if re.fullmatch(r"[\d.]+", other.number) and len(k) == len(lo) and k[:-1] == lo[:-1] and lo[-1] <= k[-1] <= hi[-1]:
                    other.outcome = Outcome(**{**it.outcome.__dict__, "from_item": it.number})
                    copied += 1
    return copied
