"""structure(): rules 1-8 in order, one document at a time."""

from __future__ import annotations

import re

from . import bookmarks as bm
from . import clean, items as text_items, outcome as oc, people
from .model import Document, Item

NOTHING = re.compile(r"(?i)^[^\n]{0,80}?(?::\s*none\.?|\bthere\s+(?:was|were)\s+no\b[^\n]{0,60}|\bno\s+(?:comments?|action|report|"
                     r"business)\b[^\n]{0,40})\s*$")  # "5. PUBLIC HEARINGS: None", "VII. There was no old business."
BARE_LABEL = re.compile(r"^[\s#*|]*\d{1,2}\.\s?\d{1,2}[\s*|]*$")  # "22.1 0" alone: a number column split from its text


def _kinds(items: list[Item]) -> None:
    numbers = [it.number for it in items if it.number and it.kind in ("item", "section")]
    for it in items:
        if it.kind == "item" and it.number and any(n.startswith(it.number + ".") for n in numbers):
            it.kind = "section"


def _tag(items: list[Item]) -> None:
    for it in items:
        if it.kind == "procedural":
            it.kind = "item"
            it.procedural = people.procedural_tag(it.title) or (
                "adjournment" if text_items.CLOSES_MEETING.match(it.title) else "call to order")
        elif it.kind in ("item", "section"):
            it.procedural = people.procedural_tag(it.title)
            if not it.procedural and it.kind == "item" and len(it.text) <= 160 and NOTHING.match(it.text.replace("\n", " ")):
                it.procedural = "nothing recorded"
            # an item under a procedural section is procedural too (3.1 under "3. CLOSED SESSION"), unless it reports
            # an action
            if not it.procedural and it.section and it.number and "." in it.number:
                tag = people.procedural_tag(it.section)
                if tag in ("executive session", "minutes", "public comment") and not people.REPORT_OUT.search(it.title):
                    it.procedural = tag


def _quality(doc_items: list[Item], kind: str | None, total_chars: int, dropped_chars: dict[str, int],
             lines: list[tuple[int, str]], source: str) -> dict:
    items = [it for it in doc_items if it.kind != "front"]
    decisions = Document(doc_items, [], {}, {}, source).buying_items()
    largest = max((len(it.text) for it in doc_items), default=0)
    bare = sum(1 for _, ln in lines if BARE_LABEL.match(ln))
    q = {
        "items": len(items),
        "buying_items": len(decisions),
        "numbered": round(sum(1 for it in items if it.number) / len(items), 3) if items else 0.0,
        "with_outcome": round(sum(1 for it in decisions if it.outcome) / len(decisions), 3) if decisions else 0.0,
        "consent_outcomes": sum(1 for it in items if it.outcome and it.outcome.consent),
        "fragments": sum(1 for it in items if it.kind == "item" and not it.procedural and len(it.text) < 60),
        "procedural": sum(1 for it in items if it.procedural),
        "chars": total_chars,
        "dropped_share": round(sum(dropped_chars.values()) / total_chars, 3) if total_chars else 0.0,
        "dropped_chars": dropped_chars,
        "largest_item_share": round(largest / max(1, sum(len(it.text) for it in doc_items)), 3),
        "bare_labels": bare,
        "flags": [],
    }
    f = q["flags"]
    if len(items) >= 4 and q["numbered"] < 0.3:
        f.append("most items unnumbered")
    if total_chars > 8000 and q["largest_item_share"] > 0.6:
        f.append("one item holds most of the text")
    if kind == "minutes" and len(decisions) >= 3 and q["with_outcome"] < 0.2:
        f.append("minutes with few outcomes")
    if items and q["fragments"] / len(items) > 0.3:
        f.append("many fragments")
    if bare >= 4 and source == "text":
        f.append("item numbers separated from their text (two-column layout)")
    if total_chars > 2000 and len(items) <= 1:
        f.append("no items found")
    return q


def structure(pages, *, kind: str | None = None, pdf_path=None, context: dict | None = None,
              marks: list | None = None) -> Document:
    """A document as items. `pages` is [(page, text)] as the navigator's readers give them (numbered from 1, as PDF
    bookmarks count); `kind` the document's kind (minutes, agenda, packet, item); `pdf_path` the PDF, for its bookmarks
    (or pass them as `marks`, a list of bookmarks.Bookmark); `context` a dict carried from one call to the next when a
    document is read in parts (the text rules' outline). Nothing is dropped silently: `dropped` counts every line left
    out, and procedural items are kept and tagged."""
    pages = [(p, t) for p, t, *_ in pages]
    context = context if context is not None else {}
    total = sum(len(t or "") for _, t in pages)
    lines, dropped, dropped_chars = clean.clean_pages(pages)
    if marks is None and pdf_path is not None:
        marks = bm.read(pdf_path)
    found = bm.items_from(marks, lines) if marks else None
    source = "bookmarks" if found else "text"
    items = found if found else text_items.split(lines, context)
    _kinds(items)
    _tag(items)
    for it in items:
        it.outcome = oc.read_outcome(it.text)
        people.annotate(it)
    oc.propagate_ranges(items)
    oc.propagate_consent(items)
    present = []
    for it in items:
        if it.procedural in ("roll call", "call to order") or it.kind == "front" or it is items[0]:
            for p in people.attendance(it.text):
                if p.name not in {x.name for x in present}:
                    present.append(p)
    quality = _quality(items, kind, total, dropped_chars, lines, source)
    if marks is None and pdf_path is not None:
        quality["flags"].append("bookmarks not read")
    return Document(items=items, attendance=present, dropped=dropped, quality=quality, source=source)
