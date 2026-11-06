"""The structure layer: every document into the same primitives, so its structure is the same across documents and
bodies. A document (pages of text, and the PDF when there is one) becomes a Document of Items (number, section, title,
full text, pages, kind, procedural tag, outcome, amounts, organisations, people, refs), the meeting's attendance,
counts of what was dropped, and quality counters. No model calls; no per-body code.

    from ddwb.structure import structure
    doc = structure(pages, kind="minutes", pdf_path=path)   # pages: [(page, text)] as tools.doc_pages gives them
    doc.buying_items()                                      # what the buying question needs to see

Rules, in order (one module each): clean.py (running headers and footers, page numbers, e-sign stamps and boilerplate,
counted; pages joined with each line's page kept) -> bookmarks.py (the PDF outline first: an agenda tree numbered by
place when the document confirms it, numbered bookmarks, attachments) -> items.py (text rules otherwise, reusing
text.ITEM_START, item_label, SUB_BARE and outline_path; only a labelled line opens an item) -> outcome.py (motion and
vote lines; the consent motion copied to every item of its consent sections; block votes over a range) -> people.py
(procedural tags, attendance, people and organisations) -> core.py (quality counters and flags).

Measured on the four development bodies (2026-11-05; python -m ddwb.structure.measure, on the truth builder's minutes
text of 32 sampled minutes, bookmarks read with pypdf):
- Truth item numbers found (text.same_item): 362 of 366, against 332 today (Cortez 27/27, Pueblo 13/16, PYLUSD
  153/154, Summit 169/169). The 4 left: truth labels "D" (for IV.D) and "19.1-19.5", and Pueblo's VIII.B written in a
  table that merges the section headings.
- Truth decisions whose item carries its outcome: 303 of the 303 decided ones (all but first readings and items only
  considered), against 140 today (the same parser on today's passage holding the quote; 274 by the Jev audit's
  lenient test, which counts any "approved"). 156 of them carry the consent motion copied from their section.
  Cortez: 27 of 27 (the audit: 7 of 21 truth passages lacked one).
- Jev calls: 2,148 passages today -> 1,155 items -> 739 buying items (-66%); per document 67 -> 36 -> 23.
- Text dropped: 1.9% (Summit 5.7%: e-sign stamps and running footers; Pueblo 0%).
- Truth decisions lost to a procedural tag: 0. The closed-session settlement stays in "REPORT OF BOARD ACTION TAKEN IN
  CLOSED SESSION", which is never procedural; the closed-session agenda listing of it is.
- Bookmarks: PYLUSD without them 127/154 numbers and 105/154 outcomes, and every long minutes flags itself ("item
  numbers separated from their text (two-column layout)"); with them 153/154 and 154/154, no flags.
"""

from .bookmarks import Bookmark
from .core import structure
from .model import Document, Item, Outcome, Person

__all__ = ["Bookmark", "Document", "Item", "Outcome", "Person", "structure"]
