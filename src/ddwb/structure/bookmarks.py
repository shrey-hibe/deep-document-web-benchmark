"""Rule 3: the structure a PDF declares in its bookmarks (outline), read before any text rule.

It walks the outline, reads item labels such as "16.4 ..." and "17. C. ...", and anchors each title by its first words.
Two layouts are read, in any mix:
- An agenda tree: unnumbered sections at the top level ("CONSENT CALENDAR - BUSINESS SERVICES") with unnumbered items
  under them ("Approved the architectural services agreement ..."). Their numbers are their places in the tree (the
  22nd section's 3rd item is 22.3), used only when the document confirms them: the text prints a section's number
  with its title ("## 22. CONSENT CALENDAR-BUSINESS SERVICES"), or numbered attachment bookmarks fall inside the tree
  ("23.16 Gifts for September 9, 2025" under a 23rd section of at least 16 items).
- Numbered bookmarks ("2.b. Expenditure List", "22.3 NOC Detail"): an item, or the attachment of the tree item of the
  same number. An unnumbered bookmark after a numbered one ("ATT_Expenditure List") is that item's attachment.
Each item's text runs from its title in the text (found word by word, tolerating stray numbers between words) to the
next item's. pypdf reads the outline; without it, `read` returns None and the text rules apply.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from ddwb.deep import text as dtext

from .items import tidy
from .model import Item

COMPOUND = re.compile(r"^\s*(?:item\s+)?(\d{1,2})\.\s*([A-Za-z])\.\s+")  # "17. C. Approve ...", "2.a. minutes for ..."
DOTTED = re.compile(r"^\s*(?:item\s+)?(\d{1,2}(?:\.\d{1,2})+)\b\.?")  # "22.3 NOC Detail", "16.4 Surplus Property"
WORD = re.compile(r"[A-Za-z0-9]+")


@dataclass(frozen=True)
class Bookmark:
    level: int  # 0 = top level
    title: str
    page: int | None  # counted from 1; None when the destination cannot be resolved


def read(pdf_path) -> list[Bookmark] | None:
    """The PDF's bookmarks in outline order; [] when it has none; None when they cannot be read (no pypdf, a broken
    file)."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return None
    try:
        reader = PdfReader(str(pdf_path))
        out: list[Bookmark] = []

        def walk(outline, level):
            for node in outline:
                if isinstance(node, list):
                    walk(node, level + 1)
                    continue
                try:
                    page = reader.get_destination_page_number(node) + 1
                except Exception:  # noqa: BLE001 - a broken destination must not lose the rest of the tree
                    page = None
                title = " ".join(str(getattr(node, "title", "") or "").split())
                if title:
                    out.append(Bookmark(level, title, page if page and page > 0 else None))
        walk(reader.outline, 0)
        return out
    except Exception:  # noqa: BLE001 - an unreadable outline means: use the text rules
        return None


def label_of(title: str) -> str | None:
    """A bookmark's own item number: "2.b", "22.3", "17.C", "VIII", "2025-123", or None."""
    if m := COMPOUND.match(title):
        return f"{m.group(1)}.{m.group(2)}"
    if m := DOTTED.match(title):
        return m.group(1)
    lab = dtext.item_label(title)
    if lab and re.match(r"^\s*(?:item\s+)?\S+[.)]\s|^\s*20\d\d-\d|^\s*(?:resolution|ordinance)\b", title, re.I):
        return lab
    return None


def place_label(title: str) -> str | None:
    """A bookmark label that is a place in the outline (not a resolution or ordinance number, which an item of the
    tree can carry as its title: "Resolution No. 25-07")."""
    lab = label_of(title)
    return None if lab is None or re.match(r"(?:resolution|ordinance)\b|20\d\d-", lab, re.I) else lab


def anchor(title: str) -> list[str]:
    """A title's first words without its number, case or punctuation (its anchor)."""
    t = re.sub(r"^\s*(?:item\s+)?(?:\d{1,2}(?:\.\d{1,2})*|[A-Za-z]|[IVX]{1,5})[.):]?\s+(?:[A-Za-z][.)]\s+)?", "", title)
    return [w.lower() for w in WORD.findall(t)]


@dataclass
class _Anchor:
    order: int
    number: str | None
    title: str
    page: int
    kind: str  # section | item | attachment | front
    section: str | None = None


def _printed_numbers(lines: list[tuple[int, str]]) -> dict[tuple[str, ...], set[int]]:
    """Section numbers printed in the text, by the first three words of the title they open."""
    out: dict[tuple[str, ...], set[int]] = {}
    for _, ln in lines:
        m = re.match(r"^[\s#*|]*(\d{1,2})\.?\s+(?:\*\*)?\s*([A-Za-z].*)", ln)
        if m:
            key = tuple(w.lower() for w in WORD.findall(m.group(2))[:3])
            if key:
                out.setdefault(key, set()).add(int(m.group(1)))
    return out


def plan(marks: list[Bookmark], lines: list[tuple[int, str]]) -> list[_Anchor]:
    """The document's items as the bookmarks declare them, before their text is cut."""
    marks = [b for b in marks if b.page]
    # the agenda tree: unnumbered top-level bookmarks and their unnumbered children, by place
    # it ends at the first bookmark that points back to an earlier page (a wrapper such as "Minutes" -> page 1) or at the
    # first numbered top-level bookmark after it (the attachments that follow the agenda)
    tops: list[tuple[int, Bookmark, list[Bookmark]]] = []
    seen_page = 0
    for b in marks:
        if b.level == 0 and (place_label(b.title) and tops or b.page < seen_page):
            break
        seen_page = max(seen_page, b.page)
        if place_label(b.title):
            continue
        if b.level == 0:
            tops.append((len(tops) + 1, b, []))
        elif b.level == 1 and tops:
            tops[-1][2].append(b)
    printed = _printed_numbers(lines)
    offsets = Counter()
    for pos, b, _ in tops:
        for n in printed.get(tuple(anchor(b.title)[:3]), ()):
            offsets[n - pos] += 1
    numbered = [label_of(b.title) for b in marks if label_of(b.title)]
    offset, confirmed = 0, False
    if offsets:
        offset, hits = offsets.most_common(1)[0]
        confirmed = hits >= 2 and hits >= 0.6 * sum(offsets.values()) or hits >= 1 and len(offsets) == 1
    sizes = {pos + offset: len(kids) for pos, _, kids in tops}
    inside = [n for n in numbered if re.fullmatch(r"\d{1,2}\.\d{1,2}", n)]
    if inside and not confirmed:
        fit = sum(1 for n in inside if sizes.get(int(n.split(".")[0]), 0) >= int(n.split(".")[1]))
        confirmed = fit >= max(2, 0.8 * len(inside))
    tree_numbers: dict[int, str] = {}
    tree_titles: dict[str, str] = {}
    if confirmed:
        for pos, b, kids in tops:
            sec = str(pos + offset)
            tree_numbers[id(b)] = sec
            tree_titles[sec] = b.title
            for j, k in enumerate(kids, start=1):
                tree_numbers[id(k)] = f"{sec}.{j}"
                tree_titles[f"{sec}.{j}"] = k.title
    out: list[_Anchor] = []
    last_item: _Anchor | None = None
    last_page = 0
    for i, b in enumerate(marks):
        num = tree_numbers.get(id(b))
        if num is not None:
            has_kids = any(n.startswith(num + ".") for n in tree_titles) if "." not in num else False
            a = _Anchor(i, num, b.title, b.page, "section" if has_kids else "item",
                        tree_titles.get(num.rsplit(".", 1)[0]) if "." in num else None)
        elif (lab := label_of(b.title)) is not None:
            if lab in tree_titles or lab.split(".")[0] in tree_titles and "." in lab and confirmed:
                a = _Anchor(i, lab, b.title, b.page, "attachment", tree_titles.get(lab))
            else:
                parent = lab.rsplit(".", 1)[0] if "." in lab else None
                a = _Anchor(i, lab, b.title, b.page, "item", tree_titles.get(parent) if parent else None)
        elif b.level == 0 and b.page >= last_page:
            if last_item is not None and last_item.kind in ("item", "attachment") and not confirmed:
                a = _Anchor(i, last_item.number, b.title, b.page, "attachment", last_item.title)
            elif not out:
                a = _Anchor(i, None, b.title, b.page, "front")
            else:
                continue  # an unnumbered bookmark the tree does not place (a wrapper such as "Minutes")
        else:
            continue  # bookmarks inside an item or an attachment, and wrappers pointing back
        if a.kind in ("item", "section"):
            last_item = a
        elif a.kind == "attachment" and a.number:
            last_item = last_item if last_item and last_item.number == a.number else a
        last_page = max(last_page, a.page)
        out.append(a)
    return out


def _find(full: str, title: str, start: int, stop: int) -> int | None:
    words = WORD.findall(title)
    words = [w for w in words if not re.fullmatch(r"\d{1,2}(?:\.\d{1,2})*", w)] or words
    for k in (10, 6, 4, 3, 2):
        if len(words) < k and k != 2:
            continue
        ws = words[:k]
        if not ws:
            return None
        sep = r"[\W_]+(?:[A-Za-z0-9]+[\W_]+){0,2}?"
        pat = re.compile(r"(?<![A-Za-z0-9])" + sep.join(re.escape(w) for w in ws) + r"(?![A-Za-z0-9])", re.I)
        m = pat.search(full, start, stop)
        if m:
            return m.start()
    return None


LABEL_PREFIX = re.compile(r"[\s#*|>_]*(?:item\s+)?(?:(?:\d{1,2}(?:\.\s?\d{1,2})*|[IVXivx]{1,5}|[A-Za-z])[.):]?\s*){0,2}(?:\*\*)?\s*")


def cut(plan_: list[_Anchor], lines: list[tuple[int, str]]) -> list[Item]:
    """Each planned item with its text: from its title in the text to the next item's."""
    full_parts, starts, page_at = [], [], []
    pos = 0
    for p, ln in lines:
        starts.append(pos)
        page_at.append(p)
        full_parts.append(ln)
        pos += len(ln) + 1
    full = "\n".join(full_parts)
    page_start: dict[int, int] = {}
    for s, p in zip(starts, page_at):
        page_start.setdefault(p, s)
    pages_sorted = sorted(page_start)

    def page_of(offset: int) -> int:
        lo = 0
        for i, s in enumerate(starts):
            if s > offset:
                break
            lo = i
        return page_at[lo] if page_at else 1

    def start_of(page: int) -> int | None:
        nxt = [p for p in pages_sorted if p >= page]
        return page_start[nxt[0]] if nxt else None

    anchors = sorted((a for a in plan_ if start_of(a.page) is not None), key=lambda a: (a.page, a.order))
    placed: list[tuple[int, _Anchor]] = []
    cursor, after = 0, 0  # after: where the next title search may start (past the last title found)
    for a in anchors:
        ps = start_of(a.page)
        later = [p for p in pages_sorted if p > a.page + 1]
        stop = page_start[later[0]] if later else len(full)
        at = None
        if a.kind in ("item", "section"):
            title = a.title if not label_of(a.title) else re.sub(r"^\S+\s+", "", a.title)
            at = _find(full, title, max(after, ps), stop)
            if at is None:  # the destination can point a page late
                prev = [p for p in pages_sorted if p < a.page]
                if prev:
                    at = _find(full, title, max(after, page_start[prev[-1]]), stop)
            if at is not None:
                ls = full.rfind("\n", 0, at) + 1
                if LABEL_PREFIX.fullmatch(full[ls:at]) and ls >= cursor:
                    at = ls
        found = at is not None
        if at is None:
            at = max(cursor, ps)
        placed.append((at, a))
        cursor = at
        after = at + 1 if found else at
    items = []
    for i, (at, a) in enumerate(placed):
        end = placed[i + 1][0] if i + 1 < len(placed) else len(full)
        chunk = full[at:end]
        body = "\n".join(t for t in (tidy(x) for x in chunk.splitlines()) if t)
        first_page = page_of(at) if chunk.strip() else a.page
        last_page = page_of(max(at, end - 2)) if chunk.strip() else a.page
        title = a.title[:250]
        head = [w.lower() for w in WORD.findall(body.split("\n", 1)[0])]
        want = [w for w in anchor(title)[:3]]
        if a.kind in ("item", "section") and not (want and head[:len(want) + 3] and set(want) <= set(head[:len(want) + 3])):
            body = f"{title}\n{body}" if body else title
        refs = dtext.refs_of(a.title)
        items.append(Item(number=a.number, section=a.section, title=title, text=body or title,
                          pages=(first_page, max(first_page, last_page)), kind=a.kind, refs=refs))
    lead = placed[0][0] if placed else 0
    if lead > 0 and full[:lead].strip():  # the title block before the first bookmark
        body = "\n".join(t for t in (tidy(x) for x in full[:lead].splitlines()) if t)
        items.insert(0, Item(number=None, section=None, title=body.split("\n")[0][:250], text=body,
                             pages=(page_at[0], page_of(lead - 1)), kind="front"))
    return items


def items_from(marks: list[Bookmark] | None, lines: list[tuple[int, str]]) -> list[Item] | None:
    """Items from the bookmarks, or None when they do not describe the document's items (fewer than three items)."""
    if not marks:
        return None
    p = plan(marks, lines)
    if sum(1 for a in p if a.kind in ("item", "section")) < 3:
        return None
    return cut(p, lines)
