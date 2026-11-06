"""Rules 1 and 2: clean the pages, then join them into one list of lines, each with its page.

Dropped, and counted by reason:
- "header/footer": a line that runs at the top or bottom edge of most pages ("City Council Regular Meeting June 10,
  2025 Page 2", "Council Minutes April 28, 2025 Page 3 of 12"), compared with its digits blanked so the page number and
  date do not hide the repeat. A running line glued onto the first or last line of a page is cut off that line.
- "page number": "Page 4 of 33", "- 3 -", or a bare number at the edge of a page.
- "boilerplate": e-signature stamps ("Docusign Envelope ID: ..."), signature rules (_____), "Approved as to form",
  "Attest:".
Vote and motion lines, and lines that open a numbered item, are never dropped as running lines.
"""

from __future__ import annotations

import re
from collections import Counter

from ddwb.deep import text as dtext

EDGE = 4  # running headers and footers sit within this many lines of a page's top or bottom
LAYERS = 2  # a header can be two running lines (a title and a date line): peel at most this many per edge

PAGE_NO = re.compile(r"^\W*(?:page\s*\d{1,4}(?:\s*(?:of|/)\s*\d{1,4})?|\d{1,4}\s*(?:of|/)\s*\d{1,4}|-\s*\d{1,4}\s*-|page)\W*$",
                     re.I)
BARE_NUMBER = re.compile(r"^\W*\d{1,4}\W*$")  # a page number when it sits at a page's edge
PAGE_TAIL = re.compile(r"\s*\bpage\s+\d{1,4}\s+of\s+\d{1,4}\W*$", re.I)  # "... Page 3 of 10" glued to a line's end
STAMP = re.compile(r"^\W*(?:docusign envelope id|adobe sign transaction|dotloop signature verification|authentisign id)\b", re.I)
BOILER = re.compile(r"^\W*(?:approved as to form\W*|attest\W*|_{4,}[\W_]*|\(?seal\)?\W*)$", re.I)
OCR_MARK = re.compile(r"^\s*\[OCR\]\s*$")
VOTE_WORDS = re.compile(r"\b(?:moved|seconded|motion|carried|aye|ayes|nay|nays|yes|vote|adopted|approved|abstain)\b", re.I)
TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)*\|?\s*$")  # a Markdown table's |---|---| line


def norm(line: str) -> str:
    """A line compared across pages: markup, case and punctuation gone, every run of digits read as #."""
    s = re.sub(r"<[^>]+>", " ", line)
    s = re.sub(r"\d+", "#", s.lower())
    s = re.sub(r"[^a-z#]+", " ", s)
    return " ".join(s.split())


def _tokens(line: str) -> list[tuple[int, str]]:
    """(index of the raw whitespace token, normalised word) for every normalised word of a line, in order."""
    out = []
    for i, tok in enumerate(line.split()):
        out += [(i, w) for w in norm(tok).split()]
    return out


def _cut(line: str, running: str) -> tuple[str, bool]:
    """The line without a running line glued at its start or end (only when something else is left)."""
    want, toks = running.split(), _tokens(line)
    k = len(want)
    if not k or len(toks) <= k:
        return line, False
    raw = line.split()
    if [w for _, w in toks[-k:]] == want:
        return " ".join(raw[:toks[-k][0]]), True
    if [w for _, w in toks[:k]] == want:
        return " ".join(raw[toks[k - 1][0] + 1:]), True
    return line, False


def _protected(line: str) -> bool:
    return bool(VOTE_WORDS.search(line) or dtext.item_label(re.sub(r"^[\s#*|]+", "", line)) and len(line) > 12)


def _running(pages: list[list[str]], top: bool) -> set[str]:
    """Normalised lines that run at one edge of most pages, peeled one layer at a time."""
    found: set[str] = set()
    n = sum(1 for p in pages if p)
    if n < 2:
        return found
    need = 2 if n <= 3 else max(3, -(-n // 2))  # half the pages (two of a 2- or 3-page document)
    for _ in range(LAYERS):
        edge = Counter()
        for lines in pages:
            rest = [ln for ln in lines if norm(ln) not in found]
            rest = rest if top else rest[::-1]
            for ln in rest[:1]:
                key = norm(ln)
                if key and not _protected(ln):
                    edge[key] += 1
        new = {k for k, c in edge.items() if c >= need}
        if not new:
            break
        found |= new
    return found


def clean_pages(pages: list[tuple[int, str]]) -> tuple[list[tuple[int, str]], dict[str, int], dict[str, int]]:
    """Rules 1 and 2. Returns the document's lines [(page, line)] in order, and the lines and characters dropped by
    reason. Blank lines and Markdown table rules (|---|) go without a count: they hold no text."""
    raw = []
    for p, md in pages:
        lines = [ln.rstrip() for ln in (md or "").splitlines()]
        raw.append((p, [ln for ln in lines if ln.strip() and not OCR_MARK.match(ln) and not TABLE_RULE.match(ln)]))
    dropped, chars = Counter(), Counter()

    def drop(reason: str, ln: str) -> None:
        dropped[reason] += 1
        chars[reason] += len(ln.strip())

    # page numbers and stamps first, so they do not hide a running line behind them
    stage = []
    for p, lines in raw:
        kept = []
        for i, ln in enumerate(lines):
            at_edge = i < 2 or i >= len(lines) - 2
            if STAMP.match(ln) or BOILER.match(ln):
                drop("boilerplate", ln)
            elif PAGE_NO.match(ln) or at_edge and BARE_NUMBER.match(ln):
                drop("page number", ln)
            else:
                kept.append(ln)
        stage.append((p, kept))
    heads = _running([ls[:EDGE] for _, ls in stage], top=True)
    feet = _running([ls[-EDGE:] for _, ls in stage], top=False)
    out = []
    for p, lines in stage:
        n = len(lines)
        for i, ln in enumerate(lines):
            key = norm(ln)
            near_top, near_bottom = i < EDGE, i >= n - EDGE
            if key and (near_top and key in heads or near_bottom and key in feet):
                drop("header/footer", ln)
                continue
            if near_bottom:
                for f in feet:
                    ln2, cut = _cut(ln, f)
                    if cut:
                        chars["header/footer"] += len(ln) - len(ln2)
                        dropped["header/footer"] += 1
                        ln = ln2
                        break
                m = PAGE_TAIL.search(ln)
                if m and m.start() > 0:
                    chars["page number"] += len(m.group(0))
                    dropped["page number"] += 1
                    ln = ln[:m.start()]
            if near_top:
                for h in heads:
                    ln2, cut = _cut(ln, h)
                    if cut:
                        chars["header/footer"] += len(ln) - len(ln2)
                        dropped["header/footer"] += 1
                        ln = ln2
                        break
            if ln.strip():
                out.append((p, ln))
    return out, dict(dropped), dict(chars)
