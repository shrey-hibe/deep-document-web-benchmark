"""A document's items, as the pipeline decides on them: one dict per agenda item.

    {"number", "section", "title", "text", "outcome", "amounts", "orgs", "people", "pages", "procedural", "passages"}

This is the shape `ddwb.structure` will give (items with number, section, title, text, outcome, amounts, orgs, people,
pages; procedural items tagged). Until it is merged, `adapter_items` builds it as a thin layer over the navigator's own
split (`text.labelled_items`, page by page with one outline context, exactly as `tools.t_read` reads), so the passages
here are the navigator's passages, word for word, and Jev's labels on them are comparable (and cached) across the two.

The adapter only regroups; it never rewrites a passage:
- a passage that continues an item (a long item's second part, or a page that opens mid-item) joins that item;
- a short vote line ("Moved by ...", "Carried 5-0") joins the item before it, as its outcome;
- a consent agenda's single vote is carried to each of its items that has no vote of its own;
- an item that is only a page marker or a fragment, or a short procedural item with no amount (call to order, roll
  call, pledge, public comment, adjournment), is tagged `procedural` and set aside by the driver, counted.

Switching to the structure module is the one line marked SWITCH below. Its items need no "passages": the driver then
makes one passage per item.
"""

from __future__ import annotations

import re

from ..deep import text

# Lines that report a vote or an outcome: the item's outcome.
VOTE = re.compile(r"(?:\bmotion\b|\bmoved\b)[^\n]{0,240}?\b(?:carried|passed|approved|adopted|failed|defeated|denied)\b|"
                  r"\bcarried\b\W*(?:\d+\s*-\s*\d+|unanimously)|"
                  r"\b(?:was|were)\s+(?:duly\s+|unanimously\s+)?(?:adopted|approved|passed|tabled|denied|rejected|postponed)\b|"
                  r"\broll call vote\b|\bvote\s*:", re.I)
# A short passage that is only part of a vote: it belongs to the item before it.
VOTE_LINE = re.compile(r"^[\W_]*(?:moved by|motion by|seconded by|second(?:ed)?\s*:|ayes?\b|nays?\b|noes\b|abstain|absent\b|"
                       r"carried\b|motion (?:carried|passed|failed)|roll call vote|vote\s*:|preferential student)", re.I)
PROCEDURAL = re.compile(r"^[\W_]*(?:[\w.]{1,6}[.)]?\s+)?(?:call(?:ed)? to order|roll call|pledge of allegiance|invocation|"
                        r"moment of silence|adjourn|public (?:comment|participation)|citizen(?:s'?)? comments?|"
                        r"audience participation|announcements?|recess|reconvened?)\b", re.I)
BOILERPLATE_LINE = re.compile(r"^[\W_]*(?:page \d+(?: of \d+)?|docusign envelope id\b.*|\d{1,3})[\W_]*$", re.I)
CONSENT = re.compile(r"consent", re.I)
PERSON = re.compile(r"\b(?:Mr|Ms|Mrs|Dr|Miss)\.?\s+[A-Z][A-Za-z'\-]+(?:\s+[A-Z][A-Za-z'\-]+)?")
PROCEDURAL_CHARS = 600  # a procedural heading with this much text after it is read anyway: it may hold a decision
FRAGMENT_CHARS = 40


def adapter_items(pages: list[tuple], kind: str | None = None, pdf_path=None, procedural_max: int | None = None) -> list[dict]:
    """Items of a document read as pages [(page, markdown, image_only?), ...] by `tools.doc_pages`. (`kind` and
    `pdf_path` are the structure layer's; the adapter reads text only.)"""
    passages, context = [], {}
    for page, md, *_ in pages:
        for i, it in enumerate(text.labelled_items(md, context=context), start=1):
            passages.append({"page": page, "i": i, **it})
    groups: list[list[dict]] = []
    for ps in passages:
        first = ps["text"].split("\n", 1)[0]
        opens = bool(text.ITEM_START.match(first)) and not is_vote_line(ps["text"])
        continues = groups and ((ps.get("part") or 0) > 1 or (ps["i"] == 1 and not opens) or is_vote_line(ps["text"]))
        if continues:
            groups[-1].append(ps)
        else:
            groups.append([ps])
    items = [item_of(g) for g in groups]
    carry_consent_votes(items)
    return items


def structure_items(pages: list[tuple], kind: str | None = None, pdf_path=None, procedural_max: int | None = None) -> list[dict]:
    """The same item dicts from the structure layer (`ddwb.structure.structure`, bookmarks first when there is a PDF).
    Items it does not send to the buying question (procedural items, attachments, bare section headings) are tagged
    procedural here, so they are set aside and counted the same way. Its items carry no passages: the driver makes one
    passage per item."""
    from ..structure import structure  # imported here: the package arrives with the merge of build/structure

    doc = structure(pages, kind=kind, pdf_path=pdf_path)
    sent = {id(it) for it in doc.buying_items(procedural_max)}
    out = []
    for it in doc.items:
        outcome = None
        if it.outcome:
            outcome = it.outcome.text[:240] + (f" (the consent vote{' under ' + it.outcome.from_item if it.outcome.from_item else ''})"
                                               if it.outcome.consent else "")
        tag = it.procedural or (None if id(it) in sent else f"not sent ({it.kind})")
        out.append({"number": it.number, "section": it.section, "title": it.title, "text": it.text, "outcome": outcome,
                    "amounts": list(it.amounts), "orgs": list(it.orgs), "people": [p.name for p in it.people],
                    "pages": list(range(it.pages[0], it.pages[1] + 1)), "procedural": tag, "refs": list(it.refs)})
    return out


document_items = structure_items  # SWITCH done 2026-11-06: build/structure is merged (adapter_items stays as the fallback)


def is_vote_line(t: str) -> bool:
    return len(t) <= 240 and not text.MONEY.search(t) and bool(VOTE_LINE.match(t))


def title_of(t: str) -> str:
    lines = [x for x in t.split("\n") if x.strip()]
    if not lines:
        return ""
    head = " ".join(lines[:2]) if re.fullmatch(r"[\W\d.]+", lines[0]) else lines[0]
    return re.sub(r"^[\s#*|]+|[\s*|]+$", "", re.sub(r"</?u>", "", head))[:200]


def outcome_of(t: str) -> str | None:
    lines = [x.strip() for x in t.split("\n") if x.strip()]
    hits = [x for x in lines if VOTE.search(x) or text.OUTCOME.match(x) or re.match(r"^[\W_]*carried\b", x, re.I)]
    return re.sub(r"[*#]+", "", hits[-1]).strip()[:240] if hits else None


def procedural_of(t: str, amounts: list[float]) -> str | None:
    lines = [x for x in t.split("\n") if x.strip()]
    if lines and all(BOILERPLATE_LINE.match(x) for x in lines):
        return "boilerplate"
    if amounts:
        return None
    if len(t.strip()) < FRAGMENT_CHARS:
        return "fragment"
    if len(t) <= PROCEDURAL_CHARS and PROCEDURAL.match(title_of(t)):
        return "procedural"
    return None


def item_of(group: list[dict]) -> dict:
    body = "\n".join(p["text"] for p in group)
    amounts = text.amounts(body)
    first = group[0]
    return {"number": first.get("item_no"), "section": first.get("section"), "title": title_of(first["text"]),
            "text": body, "outcome": outcome_of(body), "amounts": amounts, "orgs": text.vendor_candidates(body),
            "people": list(dict.fromkeys(PERSON.findall(body)))[:8], "pages": sorted({p["page"] for p in group}),
            "procedural": procedural_of(body, amounts), "refs": list(dict.fromkeys(r for p in group for r in p.get("refs") or [])),
            "passages": group}


def carry_consent_votes(items: list[dict]) -> None:
    """A consent agenda is approved in one vote, written once (often after its last item): each of its items without
    a vote of its own gets that vote as its outcome, marked as the section's."""
    by_section: dict[str, list[dict]] = {}
    for it in items:
        if it["section"] and CONSENT.search(it["section"]):
            by_section.setdefault(it["section"], []).append(it)
    for section, members in by_section.items():
        head = next((it for it in items if it["title"] and it["title"][:150] == section[:150]), None)
        votes = [it["outcome"] for it in ([head] if head else []) + members if it and it["outcome"]]
        if not votes:
            continue
        for it in members:
            if not it["outcome"]:
                it["outcome"] = f"{votes[-1]} (the vote on the whole {section[:80]})"
