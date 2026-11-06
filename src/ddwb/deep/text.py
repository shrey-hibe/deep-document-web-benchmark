"""Plain-code steps of the deep chain: dates, dollar amounts, vendor candidates, contract terms, splitting a document
into items, and which links are worth a Jev question. No model calls here; Jev decides what these can't (chain.py)."""

from __future__ import annotations

import hashlib
import re
from datetime import date

MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
_MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
DATE_PATTERNS = (
    ("ymd", re.compile(r"(?<!\d)(20\d{2})[-_./](\d{1,2})[-_./](\d{1,2})(?!\d)")),
    ("ymd8", re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")),
    ("mdy", re.compile(r"(?<!\d)(\d{1,2})[-_./](\d{1,2})[-_./](20\d{2}|\d{2})(?!\d)")),
    ("mdy8", re.compile(r"(?<!\d)(\d{2})(\d{2})(20\d{2})(?!\d)")),
    ("month_day_year", re.compile(_MON + r"\s+(\d{1,2})(?:st|nd|rd|th)?[,_]?\s+(20\d{2})", re.I)),
    ("day_month_year", re.compile(r"(?<!\d)(\d{1,2})\s+" + _MON + r",?\s+(20\d{2})", re.I)),
    ("month_day", re.compile(_MON + r"\s+(\d{1,2})(?:st|nd|rd|th)?(?![\d,_]*\s*20\d{2})", re.I)),
)
YEAR = re.compile(r"(?<!\d)(20[1-4]\d)(?!\d)")


def _make(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d) if 2000 <= y <= 2100 else None
    except ValueError:
        return None


def find_date(text: str | None, default_year: int | None = None) -> date | None:
    """The first meeting-like date in a link's text, URL or row: 2026-10-27, 10/27/2026, 102726 is not read,
    'October 27, 2026', '27 Oct 2026', and 'Oct 27' when the year comes from the page (default_year)."""
    if not text:
        return None
    for kind, pattern in DATE_PATTERNS:
        for m in pattern.finditer(text):
            g = m.groups()
            if kind in ("ymd", "ymd8"):
                found = _make(int(g[0]), int(g[1]), int(g[2]))
            elif kind in ("mdy", "mdy8"):
                y = int(g[2]) + (2000 if len(g[2]) == 2 else 0)
                found = _make(y, int(g[0]), int(g[1]))
            elif kind == "month_day_year":
                found = _make(int(g[2]), MONTHS[g[0][:3].lower()], int(g[1]))
            elif kind == "day_month_year":
                found = _make(int(g[2]), MONTHS[g[1][:3].lower()], int(g[0]))
            else:
                found = _make(default_year, MONTHS[g[0][:3].lower()], int(g[1])) if default_year else None
            if found:
                return found
    return None


def loose_date(text: str | None, default_year: int | None = None) -> date | None:
    """find_date, then again with underscores and commas read as spaces ('july_24_2025', 'July, 24, 2025')."""
    return find_date(text, default_year) or find_date(re.sub(r"[_,]+", " ", text or ""), default_year)


MEETING_NEAR = re.compile(r"meeting|minutes|agenda|session|board|council|commission|trustees|submitted to|held", re.I)
NOT_MEETING_DATE = re.compile(r"\b(?:posted|printed|revised|updated|generated|invoice|due|expir\w*|effective|signed|received)\b", re.I)


def first_page_date(text: str | None, chars: int = 3000) -> date | None:
    """The meeting date a document's first page states: the first date on a line next to one that names a meeting, an
    agenda, minutes or the board ('BOARD MEETING MINUTES' over '5:30pm, Thursday, July 24, 2025'; 'Submitted to the Board
    of Trustees August 28, 2025'). A date on a line about posting, printing, an invoice or an expiry is not the meeting's."""
    lines = [line for line in (text or "")[:chars].splitlines() if line.strip()]
    for i, line in enumerate(lines):
        if NOT_MEETING_DATE.search(line):
            continue
        when = loose_date(line)
        if when and MEETING_NEAR.search(" ".join(lines[max(0, i - 2): i + 2])):
            return when
    return None


def find_year(text: str | None) -> int | None:
    m = YEAR.search(text or "")
    return int(m.group(1)) if m else None


MONEY = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d{1,2})?(\s?(?:million|mil\b|m\b))?", re.I)


def amounts(text: str) -> list[float]:
    """Dollar amounts in a passage, in order: '$1.99 million' -> 1990000.0, '$102,505' -> 102505.0."""
    out = []
    for m in MONEY.finditer(text or ""):
        value = float(m.group(1).replace(",", "") + (m.group(2) or ""))
        if m.group(3):
            value *= 1_000_000
        out.append(value)
    return out


_SUFFIX = (r"Inc\.?|LLC|L\.L\.C\.|LLP|Ltd\.?|Corp\.?|Corporation|Company|Co\.|Group|Associates|Services|Solutions|"
           r"Technologies|Technology|Construction|Constructors|Engineering|Engineers|Consulting|Consultants|Partners|Systems|"
           r"Enterprises|Industries|Architects|Contractors|Communications|Software|Insurance|Bank|Foundation")
_CAP = r"[A-Z][A-Za-z0-9&'.\-]*"
VENDOR = re.compile(rf"\b({_CAP}(?:\s+(?:{_CAP}|of|and|&|the)){{0,5}}\s+(?:{_SUFFIX}))(?![A-Za-z])")
VENDOR_AFTER = re.compile(rf"\b(?:with|to|from|by|awarded to|contract with|agreement with)\s+({_CAP}(?:\s+(?:{_CAP}|&|of|and)){{0,4}})")
NOT_VENDOR = re.compile(r"^(?:the|this|that|board|council|county|city|district|commission|resolution|ordinance|agenda|item|"
                        r"motion|staff|public|state|section|exhibit|attachment|approval|request|director|manager|mayor)\b", re.I)


def vendor_candidates(text: str, limit: int = 6) -> list[str]:
    """Organisation-like names in a passage (a capitalised phrase ending in Inc, LLC, Company, ..., or after 'with',
    'to', 'awarded to'), for Jev to choose from. Order of first appearance, duplicates dropped."""
    seen, out = set(), []
    for pattern in (VENDOR, VENDOR_AFTER):
        for m in pattern.finditer(text or ""):
            name = re.sub(r"\s+", " ", m.group(1)).strip(" .,;:")
            key = name.lower()
            if len(name) < 4 or key in seen or NOT_VENDOR.match(name) or name.isupper() and len(name) > 40:
                continue
            seen.add(key)
            out.append(name)
    return out[:limit]


TERM = re.compile(r"(?:through|thru|until|ending|expir\w*|terminat\w*|term of|for a period of|renewal (?:through|until))\s+"
                  r"([^.;\n]{3,60})", re.I)


def term(text: str) -> tuple[str | None, int | None]:
    """A contract term in the passage ('through June 30, 2027', 'term of three years') and the year it ends, if stated."""
    m = TERM.search(text or "")
    if not m:
        return None, None
    phrase = m.group(0).strip()
    return phrase[:120], find_year(m.group(1))


# A new agenda item starts at a numbered or lettered line, a Markdown heading, or a resolution/ordinance number.
ITEM_START = re.compile(r"^\s*(?:#{1,6}\s|\*\*|(?:\d{1,2}(?:\.\d{1,2}){0,3}|[A-Z]|[IVX]{1,5}|[a-z])[.)]\s|\(\w{1,3}\)\s|"
                        r"(?:item|resolution|ordinance|motion)\s+(?:no\.?\s*)?[\w-]+|R-?\d{2,4}-\d+|"
                        r"20\d\d-(?!20\d\d\b)\d{1,4}\s|"  # a resolution number opening the line: "2026-260 A Resolution ..."
                        r"\|\s*(?:\d{1,2}(?:\.\d{1,2}){0,3}|[A-Z]|20\d\d-\d{1,4}|R-?\d{2,4}-\d+)\.?\s*\|)", re.I)  # a table row per item
# A line that only reports what happened to the item before it ("Resolution 2026-259 was adopted.", "Motion carried 5-0").
OUTCOME = re.compile(r"^\W*(?:(?:the\s+)?(?:motion|resolution|ordinance|item|consent (?:agenda|calendar))\b[^.]{0,60}?\b(?:was|were)\s+"
                     r"(?:duly\s+|unanimously\s+)?(?:adopted|approved|passed|carried|defeated|failed|tabled|postponed|withdrawn|denied|"
                     r"rejected)|motion\s+(?:carried|passed|failed|approved)|roll call vote|vote:)", re.I)
BUYING_WORDS = re.compile(r"contract|agreement|purchase|procure|bid|rfp|rfq|request for (?:proposals|qualifications)|proposal|"
                          r"award|renew|extension|amend|grant|budget|appropriat|fund|lease|vendor|quote|invoice|payment|claims?\b|"
                          r"hire|hiring|appoint|resign|retire|superintendent|director|manager|chief|employ|subscription|license|"
                          r"software|services", re.I)
SEGMENT_CHARS = 1200


def segments(text: str, limit: int = 120) -> list[str]:
    """Split a document's text into items and keep those that could be a buying event (a dollar amount or a buying
    word). Items with an amount come first when there are more than `limit`."""
    items, current = [], []
    for line in (text or "").splitlines():
        if ITEM_START.match(line) and current:
            items.append("\n".join(current))
            current = []
        if line.strip():
            current.append(line.strip())
    if current:
        items.append("\n".join(current))
    kept = [re.sub(r"[ \t]+", " ", s)[:SEGMENT_CHARS] for s in items if MONEY.search(s) or BUYING_WORDS.search(s)]
    kept = [s for s in kept if len(s) >= 40]
    if len(kept) > limit:
        kept = sorted(kept, key=lambda s: (not MONEY.search(s), kept.index(s)))[:limit]
    return kept


def items_of(text: str, chunk: int = 2000) -> list[str]:
    """Every item of a document's text, in order, nothing dropped or cut short: a long item is split into consecutive
    parts at line breaks. A short line that only reports an outcome stays with the item before it, so a passage carries
    its own outcome and not the previous item's. (segments above is Phase 1's, with its caps; the navigation tools use this.)"""
    return [it["text"] for it in labelled_items(text, chunk)]


# The number or resolution that opens an item: "23.1", "7.", "B)", "IV.", "Item 12", "Resolution No. 2025-14", "R-2025-14".
ITEM_LABEL = re.compile(r"^[\s#*|(]*(?:item\s+(?:no\.?\s*)?)?((?:resolution|ordinance)\s+(?:no\.?\s*)?[\w-]*\d[\w-]*|R-?\d{2,4}-\d+|"
                        r"20\d\d-(?!20\d\d\b)\d{1,4}(?=\s)|\d{1,2}(?:\.\d{1,2}){0,3}(?=[.)|\s]|:(?!\d)|$)|[A-Za-z](?=[.)])|[IVX]{1,5}(?=[.)])|"
                        r"[ivx]{1,5}(?=[.)]))", re.I)


# A sub-item numbered without a final dot, "23.1 Approved the ..." or "22.8" alone on its line: an item only under its
# open numbered item (labelled_items checks that).
SUB_BARE = re.compile(r"^\s*(\d{1,2})\.\d{1,2}(?:\.\d{1,2}){0,2}(?:\s+(?-i:[A-Z])|\s*$)")


def item_label(line: str) -> str | None:
    """The item's own label as written ("23.1", "b", "IV", "resolution 2025-14"), from the line that opens it."""
    m = ITEM_LABEL.match(line or "")
    if not m:
        return None
    label = re.sub(r"\s+", " ", re.sub(r"\bno\.?\s*", "", m.group(1), flags=re.I)).strip()
    return label.lower() if label[:3].lower() in ("res", "ord") else label


def label_style(label: str, stack: list[tuple[str, str]]) -> str:
    """The outline level a label belongs to: res (a resolution, a number of its own), dotted (23.1), num, roman (IV, iv)
    or alpha (B, b), by case. A lone I, V or X is a roman numeral when the roman level of its case is open or it is an I
    opening one; otherwise a letter (I after H)."""
    if re.fullmatch(r"(?:resolution|ordinance) .*|R-?\d{2,4}-\d+|20\d\d-\d{1,4}", label, re.I):
        return "res"
    if re.fullmatch(r"\d{1,2}(?:\.\d{1,2})+", label):
        return "dotted"
    if label.isdigit():
        return "num"
    case = "upper" if label.isupper() else "lower"
    styles = {st: lab for st, lab in stack}
    if re.fullmatch(r"[IVXivx]{2,5}", label):
        return f"roman_{case}"
    if label.lower() in ("i", "v", "x"):
        alpha = styles.get(f"alpha_{case}")
        if alpha and alpha.lower() == {"i": "h", "v": "u", "x": "w"}[label.lower()]:
            return f"alpha_{case}"  # the letter after the open one
        if f"roman_{case}" in styles or label.lower() == "i":
            return f"roman_{case}"
    return f"alpha_{case}"


def outline_path(label: str, context: dict) -> str:
    """The item's full number in the document's outline: VIII → B → 1 gives "VIII.B.1", 2 → b gives "2.b", and 22.10 or
    a resolution number stays as it is. A label of a style already open replaces that level and closes the levels
    under it; a new style opens a level under the current one."""
    stack = context.setdefault("stack", [])
    style = label_style(label, stack)
    if style == "res":
        return label
    if style == "dotted":
        top = label.split(".")[0]
        while stack and not (stack[-1][0] == "num" and stack[-1][1] == top):
            stack.pop()
        stack.append(("dotted", label))
        return label
    for i, (st, _) in enumerate(stack):
        if st == style:
            del stack[i:]
            break
    stack.append((style, label))
    return ".".join(lab for st, lab in stack if st != "dotted") if not any(st == "dotted" for st, _ in stack[:-1]) else \
        ".".join([next(lab for st, lab in stack if st == "dotted")] + [lab for st, lab in stack if st not in ("dotted", "num")])


# A resolution or ordinance named in an item's title ("a. Ordinance No. 1360, Series 2025"): the same decision's number in
# every document, and often the number the record itself goes by.
REF = re.compile(r"\b(resolution|ordinance)\s+(?:no\.?|number|#)\s*(\d[\w-]*)", re.I)


def refs_of(line: str) -> list[str]:
    return list(dict.fromkeys(f"{k.lower()} {n.lower()}" for k, n in REF.findall(line or "")))


def labelled_items(text: str, chunk: int = 2000, context: dict | None = None) -> list[dict]:
    """items_of's split, each item with its place in the document's outline: {"text", "item_no", "section", "part",
    "refs"}. "refs" are the resolutions or ordinances its title names.
    "item_no" is the item's full number ("VIII.B.1", "2.b", "22.10", "2025-108"); "section" the title of the item it
    sits under (23.1 under "23. CONSENT CALENDAR"), or of the last unnumbered heading. A document read page by page
    passes one `context` dict to every page, so the outline carries over and a page that opens mid-item keeps that
    item's number."""
    context = context if context is not None else {}
    titles, last = context.setdefault("titles", {}), context.get("last")
    groups, current, opened, top = [], [], [], context.get("open_top")
    for line in (text or "").splitlines():
        starts = bool(ITEM_START.match(line))
        if not starts and (m := SUB_BARE.match(line)):
            starts = m.group(1) == top  # 23.1 only under an open item 23: "3.5 Million gallon tank" is not one
        if starts:
            lab = item_label(line) or ""
            if re.fullmatch(r"\d{1,2}(?:\.\d{1,2})*", lab):
                top = lab.split(".")[0]
        if starts and current:
            groups.append(current)
            opened.append(current_opens)
            current = []
        if not current:
            current_opens = starts
        if line.strip():
            current.append(re.sub(r"[ \t]+", " ", line.strip()))
        if groups and len(current) == 1 and len(current[0]) <= 200 and OUTCOME.match(current[0]):
            groups[-1].append(current.pop())
    if current:
        groups.append(current)
        opened.append(current_opens)
    context["open_top"] = top
    out = []
    for gi, lines in enumerate(groups):
        opens = opened[gi]
        title = re.sub(r"^[\s#*|]+", "", " ".join(lines[:2]) if re.fullmatch(r"[\d.]+", lines[0]) else lines[0])[:150]
        if opens:
            label = item_label(lines[0])
            if label is None:  # an unnumbered heading ("## CONSENT CALENDAR"): the section of the items under it
                context["heading"] = title
                section = None
            else:
                path = outline_path(label, context)
                parent = path.rsplit(".", 1)[0] if "." in path else None
                section = titles.get(parent) if parent else context.get("heading")
                titles[path], label = title, path
        elif gi == 0:  # the page opens mid-item: the item before it goes on
            label, section = last, context.get("last_section")
        else:
            label = section = None
        parts, part, size = [], [], 0
        for line in lines:
            if part and size + len(line) > chunk:
                parts.append("\n".join(part))
                part, size = [], 0
            part.append(line)
            size += len(line) + 1
        if part:
            parts.append("\n".join(part))
        for i, t in enumerate(parts, start=1):
            out.append({"text": t, "item_no": label, "section": section, "part": i if len(parts) > 1 else None,
                        "refs": refs_of(lines[0]) if opens else context.get("last_refs", []) if gi == 0 else []})
        last, context["last_section"] = label, section
        context["last_refs"] = out[-1]["refs"] if out else []
    context["last"] = last
    return out


def same_item(a: dict, b: dict) -> bool:
    """Two items (dicts with item_no and refs) that are the same decision by number: the same outline number, or a
    resolution or ordinance both name."""
    return same_item_no(a.get("item_no"), b.get("item_no")) or bool(set(a.get("refs") or []) & set(b.get("refs") or []))


def same_item_no(a: str | None, b: str | None) -> bool:
    """Item numbers written differently for the same item: "7.a" and "7a", "VIII.B.1" and "viii.b.1"."""
    key = lambda s: re.findall(r"\d+|[a-z]+", (s or "").lower())  # noqa: E731  (7.a and 7a alike; 2.10 is not 21.0)
    return bool(a and b) and key(a) == key(b)


# Words of a decision that names no amount and no buying word ("authorizing the Executive to enter into ... with X").
ACTION_WORDS = re.compile(r"authoriz|approv|accept|adopt|ratif|enter into|execute|change order|memorandum of understanding|"
                          r"\bmou\b|intergovernmental|\biga\b|warrant|expenditure|disburse|acqui|equipment|construction|repair|"
                          r"maintenance|engineer|consult|design|insurance|settle|reimburs|donat|allocat|\bcosts?\b|price|\bfees?\b|"
                          r"sourcewell|cooperative|piggyback|consent (?:agenda|calendar)|resolution|ordinance", re.I)


def could_be_buying(item: str) -> bool:
    """The plain filter on items (the arms without Jev): a dollar amount, a buying word or a decision word. Items it drops
    are counted, never hidden."""
    return len(item) >= 40 and bool(MONEY.search(item) or BUYING_WORDS.search(item) or ACTION_WORDS.search(item))


def text_hash(text: str) -> str:
    return hashlib.sha256(re.sub(r"\s+", " ", text or "").strip().lower().encode()).hexdigest()


# ------------------------------------------------------------------------------------------- links
MEETING_WORDS = re.compile(r"agenda|minute|meeting|council|commission|board|trustee|packet|clerk|legislat|government|"
                           r"public notice|calendar|archive|document|transparen|records|governance|elected|leadership", re.I)
PLATFORMS = re.compile(r"destinyhosted|diligent|boarddocs|legistar|granicus|civicclerk|municode|agendacenter|simbli|boardbook|"
                       r"iqm2|novusagenda|primegov|civicweb|escribe|agendaonline|boardontrack|meetingsplus|onbase|laserfiche|"
                       r"agendasuite|swagit|youtube\.com/@|ecode360|civicplus", re.I)
DOC_WORDS = re.compile(r"agenda|minute|packet|meeting|archive|previous|older|past|year|materials|attachment|memo|"
                       r"\.pdf|viewfile|download|document|showdocument|getfile", re.I)
SKIP = re.compile(r"^(?:mailto|tel|javascript|sms|fax):|"
                  r"//(?:[\w-]+\.)*(?:facebook|twitter|x|instagram|linkedin|nextdoor|tiktok|pinterest)\.com(?:[/:?#]|$)|"
                  r"google\.com/maps|translate\.google|/login|/signin|/subscribe|"
                  r"/referral|powered-?by|connect\.civicplus\.com|"
                  r"#$|^#", re.I)


def keep_for_locate(href: str, text: str, row: str = "") -> bool:
    """A homepage or navigation link worth asking Jev whether it leads to the board's meetings."""
    if SKIP.search(href):
        return False
    return bool(MEETING_WORDS.search(f"{text} {href}") or PLATFORMS.search(href))


def keep_for_documents(href: str, text: str, row: str = "") -> bool:
    """A link on a meetings list worth asking Jev what it is (agenda, packet, minutes, a meeting's page, more of the list)."""
    if SKIP.search(href):
        return False
    blob = f"{text} {href} {row}"
    return bool(DOC_WORDS.search(f"{text} {href}") or PLATFORMS.search(href) or find_date(blob))


def listing_hash(links: list[tuple[str, str]]) -> str:
    """Fingerprint of a meetings list: its document-like links and their texts, order ignored. A new meeting or a
    replaced file changes it; a redesigned header or a new banner does not."""
    keep = sorted({f"{h}\t{re.sub(r'\s+', ' ', t).strip().lower()}" for h, t in links if keep_for_documents(h, t)})
    return hashlib.sha256("\n".join(keep).encode()).hexdigest()
