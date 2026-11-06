"""Rule 4: split a document's lines into items by its text, reusing the navigator's item marks (text.ITEM_START,
text.item_label, text.SUB_BARE, text.outline_path).

Differences from text.labelled_items, all so an item stays whole:
- The pages are joined first, so an item that crosses a page break is one item.
- Only a line with an item label opens an item. A heading or bold line without one ("## Committee First Readings",
  "# seq., and Education Code ...", "## Moved by: ...") goes on with the item before it; an all-capitals one also
  becomes the section of the items that follow ("## CONSENT CALENDAR-CURRICULUM AND INSTRUCTION").
- A line that only reports an outcome ("Resolution 2025-123 was adopted.") never opens an item.
- A table row whose first cell is a label is read as a line ("|a.|Approval of ...|" -> "a. Approval of ..."), and a
  bold labelled heading inside a line starts a line of its own ("... comment. **VII. OLD BUSINESS**").
- A line that opens the meeting or closes it ("The Council Regular Meeting was called to order ...", "The meeting was
  adjourned ...") opens an unnumbered item, so a call to order or an adjournment motion is not read as the outcome
  of the item before it.
"""

from __future__ import annotations

import re

from ddwb.deep import text as dtext

from .model import Item

INLINE_HEAD = re.compile(r"(?<=\S)\s+(?=\*\*(?:[IVX]{1,5}|\d{1,2}|[A-Z])\.\s+[A-Z])")
LEAD = re.compile(r"^[\s#*|>_]+")
TAGS = re.compile(r"</?u>|</?sup>|</?b>|</?i>")
OPENS_MEETING = re.compile(r"(?i)^\W*(?:the\s+)?(?:[\w.'’-]+\s+){0,5}(?:was|were)\s+called\s+to\s+order\b|^\W*call\s+to\s+order\b")
CLOSES_MEETING = re.compile(r"(?i)^\W*(?:adjourn(?:ment|ed)?\b|(?:the\s+)?(?:[\w-]+\s+){0,3}(?:meeting|session)\s+(?:was\s+)?"
                            r"adjourned\b|there\s+being\s+no\s+further\s+business\b)")
# A record number with a letter prefix opening the line: "M-038-2026 A Motion of Council ...", "O-2025-14", "RES-2025-3"
RECORD = re.compile(r"^[\s#*]*((?:[A-Z]{1,3})-\d{1,4}-(?:20)?\d\d|(?:RES|ORD|[MOR])-?20\d\d-\d{1,4})(?=[\s*|:.]|$)")
LABEL_ONLY = re.compile(r"^\W*(?:\d{1,2}(?:\.\s?\d{1,2})*|[A-Za-z]|[IVXivx]{1,5})\W*$")


def detable(line: str) -> str:
    """A table row whose first cell is an item label, as a plain line. Other rows stay as they are."""
    s = line.strip()
    if not s.startswith("|"):
        return line
    cells = [c.strip() for c in s.strip("|").split("|")]
    cells = [c for c in cells if c]
    if not cells:
        return ""
    first = LEAD.sub("", cells[0])
    if dtext.item_label(first) is None:
        return line
    joined = " ".join(cells)
    head = joined.split("$")[0]
    if not re.search(r"[A-Za-z]{3,}", head[len(cells[0]):] if len(cells) > 1 else re.sub(r"^\S+", "", first)):
        return line
    return joined


def split_inline(line: str) -> list[str]:
    return [p for p in INLINE_HEAD.split(line) if p.strip()]


def tidy(line: str) -> str:
    """A line as an item's text keeps it: heading marks and tags gone, spaces collapsed."""
    return re.sub(r"[ \t]+", " ", TAGS.sub("", re.sub(r"^\s*#{1,6}\s*", "", line))).strip()


BARE_LETTER = re.compile(r"^[\s#*]*([A-Z])\s+\**\s*[A-Z][A-Za-z]")  # "A Approve Settlement Agreement ..." (no dot)


def bare_letter_runs(lines: list[str]) -> set[int]:
    """Lines that open an item with a bare capital letter: only in a run A, B, ... in order, each within 40 lines of
    the one before, so "A Caucus was held ..." or "A Resolution ..." alone never opens one."""
    found = [(i, m.group(1)) for i, ln in enumerate(lines) if (m := BARE_LETTER.match(ln))]
    keep: set[int] = set()
    for k, (i, letter) in enumerate(found):
        if letter != "A":
            continue
        run, last, want = [i], i, "B"
        for j, nxt in found[k + 1:]:
            if j - last > 40:
                break
            if nxt == want:
                run.append(j)
                last, want = j, chr(ord(want) + 1)
        if len(run) >= 2:
            keep |= set(run)
    return keep


def opens_item(line: str, top: str | None, bare_ok: bool = False) -> tuple[bool, str | None]:
    """Whether a line opens a numbered item, and its label."""
    s = line.strip()
    if dtext.OUTCOME.match(LEAD.sub("", s)):
        return False, None
    if bare_ok and (m := BARE_LETTER.match(s)):
        return True, m.group(1)
    if m := RECORD.match(s):
        return True, m.group(1)
    if dtext.ITEM_START.match(s):
        lab = dtext.item_label(s)
        if lab is not None:
            rest = LEAD.sub("", s)[len(lab):] if LEAD.sub("", s).startswith(lab) else ""
            if re.fullmatch(r"\d{1,2}(?:\.\d{1,2})+", lab) and not re.match(r"[.)]", rest) and lab.split(".")[0] != top:
                return False, None  # "**9.64 and 149.433, ..." (a statute) is not item 9.64 unless item 9 is open
            return True, lab
    m = dtext.SUB_BARE.match(LEAD.sub("", s))
    if m and m.group(1) == top:
        return True, dtext.item_label(LEAD.sub("", s))
    return False, None


def section_heading(line: str) -> str | None:
    """An unlabelled line that names a section: all capitals, short, not a sentence."""
    s = tidy(LEAD.sub("", line)).strip("*_ ").strip()
    letters = re.sub(r"[^A-Za-z]", "", s)
    if not (4 <= len(s) <= 90) or len(letters) < 4 or not letters.isupper() or s.endswith((",", ";")):
        return None
    if len(s.split()) < 2 and len(s) < 10:
        return None
    return s


def title_of(lines: list[str]) -> str:
    first = tidy(LEAD.sub("", lines[0])).replace("**", "").replace("*", "").strip()
    if LABEL_ONLY.match(first) and len(lines) > 1:  # "22.8" or "**23.**" alone on its line: its title is the next line
        first = f"{first} {tidy(LEAD.sub('', lines[1])).replace('**', '')}"
    return first[:250]


def split(lines: list[tuple[int, str]], context: dict) -> list[Item]:
    """The document's items by text rules. `context` carries the outline from one call to the next (a document read in
    parts): stack, titles, heading, open_top, last and last_section."""
    titles = context.setdefault("titles", {})
    top = context.get("open_top")
    heading = context.get("heading")
    groups: list[dict] = []
    flat = [(page, line) for page, raw in lines for line in split_inline(detable(raw)) if line.strip()]
    bare = bare_letter_runs([ln for _, ln in flat])
    for n, (page, line) in enumerate(flat):
        starts, lab = opens_item(line, top, n in bare)
        if starts:
            if re.fullmatch(r"\d{1,2}(?:\.\d{1,2})*", lab or ""):
                top = lab.split(".")[0]
            groups.append({"lines": [(page, line)], "label": lab, "heading": heading, "kind": "item"})
            continue
        plain = LEAD.sub("", line)
        if OPENS_MEETING.match(plain) or CLOSES_MEETING.match(plain):
            groups.append({"lines": [(page, line)], "label": None, "heading": heading, "kind": "procedural"})
            continue
        h = section_heading(line) if re.match(r"^\s*(?:#|\*\*)", line) or line.isupper() else None
        if h:
            heading = h
        if not groups:
            groups.append({"lines": [], "label": None, "heading": heading, "kind": "front"})
        groups[-1]["lines"].append((page, line))
    context["open_top"], context["heading"] = top, heading
    items = []
    for gi, g in enumerate(groups):
        if not g["lines"]:
            continue
        texts = [ln for _, ln in g["lines"]]
        title = title_of(texts)
        number, section, kind = None, g["heading"], g["kind"]
        if g["label"] is not None and RECORD.match(g["label"]):
            number = g["label"]  # a record number of its own, outside the outline (as text.outline_path keeps 2025-108)
        elif g["label"] is not None:
            number = dtext.outline_path(g["label"], context)
            parent = number.rsplit(".", 1)[0] if "." in number else None
            section = titles.get(parent) if parent and parent in titles else g["heading"]
            titles[number] = title
        elif gi == 0 and kind == "front" and context.get("last"):  # a part read on its own opens mid-item
            number, section, kind = context["last"], context.get("last_section"), "item"
        body = "\n".join(t for t in (tidy(x) for x in texts) if t)
        refs = dtext.refs_of(texts[0]) if g["label"] is not None else []
        if number and number.startswith(("resolution ", "ordinance ")):
            refs = list(dict.fromkeys([number] + refs))
        items.append(Item(number=number, section=section, title=title, text=body,
                          pages=(g["lines"][0][0], g["lines"][-1][0]), kind=kind, refs=refs))
        if number:
            context["last"], context["last_section"] = number, section
    return items
