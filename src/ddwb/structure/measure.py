"""Measure the structure layer against truth, on the truth builder's minutes text ($0, no network):

    uv run --with pypdf python -m ddwb.structure.measure <body> [<body> ...] [--json out.json]

Reads data/truth/<body>/<date>/chunk-*.md ([page N] markers), the minutes PDF named in
work/deep-discovery/truth/<body>/collected.json (for its bookmarks) and truth.json. Prints, per body and in all:
- truth item numbers found (text.same_item), today (text.labelled_items, page by page) and with structure();
- truth decisions whose item carries an outcome, today (the audit's outcome-line test on the passage holding the
  quote) and with structure() (Item.outcome);
- passages today (Jev calls) against items and buying items; share of text dropped;
- truth decisions whose item is tagged procedural (each one a loss).
Pass only development bodies: the held-out ones are scored by someone else.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from ddwb import paths
from ddwb.deep import text as dtext

from .core import structure
from .outcome import read_outcome

WORDS = re.compile(r"[a-z0-9]{3,}")
AUDIT_OUTCOME = re.compile(r"\b(?:was|were) (?:duly |unanimously )?(?:adopted|approved|passed|tabled|postponed|denied|defeated)|"
                           r"motion (?:carried|passed|failed)|carried \d|vote was as follows|moved (?:to|that)|seconded|"
                           r"\bapproved\b|\badopted\b|yes:|ayes?\b", re.I)


def words(s: str | None) -> set[str]:
    return set(WORDS.findall((s or "").lower()))


def pages_of(body: str, date: str) -> list[tuple[int, str]]:
    pages: dict[int, list[str]] = {}
    cur = None
    for f in sorted((paths.DATA / "truth" / body / date).glob("chunk-*.md")):
        for line in f.read_text().splitlines():
            m = re.fullmatch(r"\[(?:\w+ (\d+) )?page (\d+)\]", line.strip())  # "[minutes 2 page 1]": a second document
            if m:
                cur = pages.setdefault(int(m.group(2)), []) if (m.group(1) or "1") == "1" else None
                continue
            if cur is not None:
                cur.append(line)
    return [(p, "\n".join(ls)) for p, ls in sorted(pages.items())]


def overlap(quote: str, text: str) -> float:
    q = words(quote)
    return len(q & words(text)) / max(1, len(q))


def measure(body: str, use_bookmarks: bool = True) -> dict:
    truth = json.loads((paths.DISCOVERY / "truth" / body / "truth.json").read_text())["decisions"]
    collected = json.loads((paths.DISCOVERY / "truth" / body / "collected.json").read_text())
    pdfs = {m["date"]: m["documents"][0]["path"] for m in collected["meetings"] if m.get("documents")}
    c, losses, misses, docs = Counter(), [], [], []
    for date in sorted(pdfs):
        pages = pages_of(body, date)
        ctx, passages = {}, []
        for p, md in pages:
            passages += [dict(it, page=p) for it in dtext.labelled_items(md, context=ctx)]
        pdf = pdfs.get(date)
        doc = structure(pages, kind="minutes", pdf_path=paths.stored(pdf) if pdf and use_bookmarks else None)
        docs.append({"date": date, "source": doc.source, "items": len(doc.items), "buying": len(doc.buying_items()),
                     "passages": len(passages), "quality": doc.quality, "dropped": doc.dropped})
        c["documents"] += 1
        c["passages_today"] += len(passages)
        c["items"] += len(doc.items)
        c["buying_items"] += len(doc.buying_items())
        c["chars"] += doc.quality["chars"]
        c["dropped_chars"] += sum(doc.quality["dropped_chars"].values())
        c[f"source_{doc.source}"] += 1
        for d in (x for x in truth if x["date"] == date):
            c["truth"] += 1
            q = d.get("quote") or ""
            todo = d.get("action") == "to_consider"
            # today: the passage holding the quote, and whether it shows an outcome line
            best_p = max(passages, key=lambda it: overlap(q, it["text"]), default=None)
            if best_p is not None and overlap(q, best_p["text"]) >= 0.5:
                c["today_quote_found"] += 1
                c["today_outcome"] += bool(AUDIT_OUTCOME.search(best_p["text"]))
                if not todo:
                    c["today_outcome_decided"] += bool(AUDIT_OUTCOME.search(best_p["text"]))
                    c["today_outcome_decided_strict"] += read_outcome(best_p["text"]) is not None
            # the new item: by number when it has one, else by the quote
            want = {"item_no": d.get("item"), "refs": dtext.refs_of(d.get("item") or "")}
            by_no = [it for it in doc.items if d.get("item") and dtext.same_item({"item_no": it.number, "refs": it.refs}, want)]
            if d.get("item"):
                c["truth_with_item"] += 1
                c["today_number_found"] += any(dtext.same_item(it, want) for it in passages)
                c["number_found"] += bool(by_no)
            buying = {id(it) for it in doc.buying_items()}

            def pick(pool):
                good = [(overlap(q, it.title + " " + it.text), it) for it in pool]
                good = [x for x in good if x[0] >= 0.5]
                return max(good, key=lambda x: x[0])[1] if good else None
            best = pick([it for it in by_no if id(it) in buying]) or pick([it for it in doc.items if id(it) in buying])
            if best is None and (held := pick(by_no) or pick(doc.items)) is not None:
                c["lost_to_procedural_or_section"] += 1
                losses.append({"date": date, "item": d.get("item"), "got": held.number, "tag": held.procedural,
                               "kind": held.kind, "title": held.title[:100]})
                best = held
            if by_no and best is not None and best in by_no:
                c["number_found_and_holds_quote"] += 1
            if best is None:
                c["quote_not_found"] += 1
                misses.append({"date": date, "item": d.get("item"), "quote": q[:100], "why": "quote not in any item"})
                continue
            c["quote_found"] += 1
            c["decided"] += not todo
            c["outcome"] += best.outcome is not None
            c["outcome_decided"] += (best.outcome is not None) and not todo
            c["outcome_consent"] += bool(best.outcome and best.outcome.consent)
            if d.get("item") and not by_no:
                misses.append({"date": date, "item": d.get("item"), "got": best.number, "title": best.title[:80],
                               "why": "number not found"})
            elif not todo and best.outcome is None:
                misses.append({"date": date, "item": d.get("item"), "got": best.number, "title": best.title[:80],
                               "why": "no outcome"})
    return {"counts": dict(c), "losses": losses, "misses": misses, "documents": docs}


def summary(body: str, r: dict) -> str:
    c = r["counts"]
    pct = lambda a, b: f"{c.get(a, 0)}/{c.get(b, 0)}"  # noqa: E731
    return (f"{body}: numbers today {pct('today_number_found', 'truth_with_item')}, now {pct('number_found', 'truth_with_item')} "
            f"(holding the quote {c.get('number_found_and_holds_quote', 0)}); outcome on decided truth today "
            f"{c.get('today_outcome_decided_strict', 0)} (lenient {c.get('today_outcome_decided', 0)}), now {pct('outcome_decided', 'decided')} (all {pct('outcome', 'quote_found')}, "
            f"consent {c.get('outcome_consent', 0)}); passages {c['passages_today']} -> items {c['items']} -> buying "
            f"{c['buying_items']} over {c['documents']} docs; dropped {c['dropped_chars'] / max(1, c['chars']):.1%}; "
            f"lost to procedural {c.get('lost_to_procedural_or_section', 0)}; quote not found {c.get('quote_not_found', 0)}; "
            f"sources {c.get('source_bookmarks', 0)} bookmarks / {c.get('source_text', 0)} text")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("bodies", nargs="+")
    ap.add_argument("--json")
    ap.add_argument("--misses", action="store_true")
    ap.add_argument("--no-bookmarks", action="store_true", help="text rules only, to see what the bookmarks add")
    a = ap.parse_args()
    out, total = {}, Counter()
    for body in a.bodies:
        r = measure(body, use_bookmarks=not a.no_bookmarks)
        out[body] = r
        total.update(r["counts"])
        print(summary(body, r))
        if a.misses:
            for m in r["misses"] + r["losses"]:
                print("   ", m)
    print(summary("all", {"counts": dict(total)}))
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
