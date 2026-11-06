"""The fixed form a model fills for each item of one meeting's document, and the checks code makes on every answer.

The model sees item cards (number, section, title, outcome, amounts, the item's text trimmed) and answers a JSON array,
one object per decision:

    {"item": 3, "event": true, "type": "contract", "action": "approved", "vendor": "...", "amount": 125000,
     "what": "...", "quote": "...", "unsure": false}

Code checks each object: the item is one of the cards; the quote occurs in the item's text; the amount parses; the type
and action come from the allowed lists (ddwb.deep.forks EVENT_TYPES and ACTIONS). An item whose answer fails a check is
asked once more with the reasons; if it fails again it is left undecided and counted (never guessed).
"""

from __future__ import annotations

import json
import re

from ..deep.forks import ACTIONS, EVENT_TYPES

BUYING_TYPES = {k: v for k, v in EVENT_TYPES.items() if k not in ("none", "staffing")}
CARD_CHARS = 2400  # the item's text shown on its card (head and tail kept when longer)
BATCH_CHARS = 16_000  # cards per model call: about 4,000 tokens of items
BATCH_ITEMS = 12
SPLIT_ITEMS = 3  # items still unanswered after the retry are asked again this many at a time
QUOTE_MIN = 8

INSTRUCTIONS = """You read one meeting document of {name}, a {kind} in {state}: the {doc_kind} of the meeting of {date}{title}.
For each item card below, decide whether it records a buying decision by the body.

A buying decision: the body buys, contracts, renews, puts out to bid or awards a bid, accepts or applies for a grant, or
sets a budget, appropriation or transfer. Staffing (hiring, appointments, pay, resignations), ceremonies, reports,
liquor or business licences the body grants to others, and approvals of earlier minutes are not.

Types: {types}
Actions: {actions}
{doc_note}
Rules:
- Answer every card. One JSON object per decision. An item that records several separate decisions (several contracts or
  purchases approved in one consent item) gets one object for each, all with the same "item".
- An item that records no buying decision gets one object: {{"item": <n>, "event": false}}.
- "quote": words copied exactly from the item's text that show the decision (10 to 200 characters).
- "amount": the decision's dollar amount as a number (no $ or commas), or null when none is stated.
- "vendor": the vendor, contractor or grantor as written, or null.
- "what": a few words on what was bought or decided.
- A consent agenda approved in one vote approves each of its items.
- "unsure": true when you cannot tell from the card.

Reply with a JSON array only, no other text:
[{{"item": 1, "event": true, "type": "...", "action": "...", "vendor": "...", "amount": 0, "what": "...", "quote": "...", "unsure": false}}]
"""

DOC_NOTE = {"minutes": "", "agenda": "This is an agenda: it lists what the board would consider, so a decision on it is usually to_consider.\n",
            "packet": "This is an agenda packet: it lists what the board would consider, so a decision in it is usually to_consider.\n"}


def norm(s: str | None) -> str:
    """Text as compared for quotes: markup and case gone, quotes and dashes plain, spaces collapsed."""
    s = (s or "").lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"[–—‑]", "-", s)
    s = re.sub(r"</?u>|[*#_|`]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def trimmed(t: str, n: int = CARD_CHARS) -> str:
    if len(t) <= n:
        return t
    head = int(n * 0.8)
    return t[:head] + "\n[…]\n" + t[-(n - head - 5):]


def card(n: int, item: dict, note: str | None = None) -> str:
    """One item as the model sees it."""
    head = [f"[{n}]"]
    if item.get("number"):
        head.append(f"item {item['number']}")
    if item.get("section"):
        head.append(f"section: {item['section'][:100]}")
    lines = [" | ".join(head), f"title: {item.get('title') or ''}"]
    if item.get("outcome"):
        lines.append(f"outcome: {item['outcome']}")
    if item.get("amounts"):
        lines.append("amounts: " + ", ".join(f"${a:,.2f}" for a in item["amounts"][:8]))
    if note:
        lines.append(f"CHECK AGAIN: {note}")
    lines.append("text:\n" + trimmed(item.get("text") or ""))
    return "\n".join(lines)


def batches(cards: list[tuple[int, dict]]) -> list[list[tuple[int, dict]]]:
    out, cur, size = [], [], 0
    for n, it in cards:
        c = min(len(it.get("text") or ""), CARD_CHARS) + 300
        if cur and (len(cur) >= BATCH_ITEMS or size + c > BATCH_CHARS):
            out.append(cur)
            cur, size = [], 0
        cur.append((n, it))
        size += c
    if cur:
        out.append(cur)
    return out


def prompt(meeting: dict, cards: list[tuple[int, dict]], notes: dict[int, str] | None = None) -> str:
    """The whole prompt for one model call: the meeting, the definitions, the rules and the cards. Deterministic, so
    the same call is the same cache key."""
    notes = notes or {}
    title = f' ("{meeting["title"][:120]}")' if meeting.get("title") else ""
    head = INSTRUCTIONS.format(
        name=meeting.get("name") or "the body", kind=meeting.get("kind") or "public body", state=meeting.get("state") or "the US",
        doc_kind=meeting.get("doc_kind") or "record", date=meeting.get("date") or "unknown date", title=title,
        types="; ".join(f"{k} = {v}" for k, v in BUYING_TYPES.items()),
        actions="; ".join(f"{k} = {v}" for k, v in ACTIONS.items()), doc_note=DOC_NOTE.get(meeting.get("doc_kind") or "", ""))
    if notes:
        head += ("\nEach card below was answered before; a check by code found a reason to look again (CHECK AGAIN). Decide "
                 "afresh from the text.\n")
    return head + "\nITEM CARDS:\n\n" + "\n\n".join(card(n, it, notes.get(n)) for n, it in cards)


def parse(reply: str | None) -> tuple[list | None, str | None]:
    """The model's reply as a list of objects, or why it is not one."""
    if not reply or not reply.strip():
        return None, "empty reply"
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", reply.strip())
    for opener, closer in (("[", "]"), ("{", "}")):
        i, j = s.find(opener), s.rfind(closer)
        if i < 0 or j <= i:
            continue
        try:
            got = json.loads(s[i: j + 1])
        except json.JSONDecodeError:
            continue
        if isinstance(got, dict):
            got = next((v for k, v in got.items() if isinstance(v, list) and k in ("items", "decisions", "answers")), [got])
        if isinstance(got, list):
            return [x for x in got if isinstance(x, dict)], None
    return None, "no JSON array in the reply"


def parse_amount(v) -> tuple[float | None, str | None]:
    if v is None or (isinstance(v, str) and v.strip().lower() in ("", "null", "none", "n/a")):
        return None, None
    if isinstance(v, bool):
        return None, "amount must be a number"
    if isinstance(v, (int, float)):
        return float(v), None
    m = re.fullmatch(r"\$?\s*([\d,]*\.?\d+)\s*(million|m)?", str(v).strip(), re.I)
    if not m:
        return None, f"amount {str(v)[:40]!r} is not a number"
    value = float(m.group(1).replace(",", ""))
    return value * (1_000_000 if m.group(2) else 1), None


def as_bool(v) -> bool | None:
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("true", "false"):
        return v.strip().lower() == "true"
    return None


def card_of(ref, items: dict[int, dict]) -> int | None:
    """The card an answer means: its card number ([3], "3"), else the agenda's own item number shown on one card
    ("item 25-0417"), which models often give instead."""
    text = str(ref if ref is not None else "").strip().strip("[]").strip()
    if text.isdigit() and int(text) in items:
        return int(text)
    key = re.sub(r"^item\s+", "", text, flags=re.I).strip().lower()
    hits = [n for n, it in items.items() if key and str(it.get("number") or "").strip().lower() == key]
    return hits[0] if len(hits) == 1 else None


def words_only(s: str | None) -> str:
    """Letters and digits only: a quote copied with other spacing or punctuation is still the item's words."""
    return re.sub(r"[^a-z0-9]+", "", norm(s))


def check(obj: dict, items: dict[int, dict]) -> tuple[dict | None, str | None]:
    """One object of the reply, checked. Returns (clean answer, None) or (None, the reason it failed)."""
    n = card_of(obj.get("item"), items)
    if n is None:
        return None, f"item {obj.get('item')!r} is not one of the cards"
    event = as_bool(obj.get("event"))
    if event is None:
        return None, '"event" must be true or false'
    unsure = as_bool(obj.get("unsure")) or False
    if not event:
        return {"item": n, "event": False, "unsure": unsure}, None
    kind, action = obj.get("type"), obj.get("action")
    if kind not in BUYING_TYPES:
        return None, f'"type" must be one of {", ".join(BUYING_TYPES)} (got {str(kind)[:30]!r})'
    if action not in ACTIONS:
        return None, f'"action" must be one of {", ".join(ACTIONS)} (got {str(action)[:30]!r})'
    amount, why = parse_amount(obj.get("amount"))
    if why:
        return None, why
    quote = str(obj.get("quote") or "").strip()
    if len(norm(quote)) < QUOTE_MIN:
        return None, '"quote" must copy the words of the item that show the decision'
    it = items[n]
    if words_only(quote) not in words_only(" ".join(str(it.get(k) or "") for k in ("title", "outcome", "text"))):  # all on the card
        return None, f'the quote "{quote[:80]}" does not occur in the item\'s title, outcome or text: copy its words exactly'
    what = str(obj.get("what") or "").strip()
    if not what:
        return None, '"what" must say in a few words what was bought or decided'
    vendor = obj.get("vendor")
    vendor = str(vendor).strip() if vendor not in (None, "") and str(vendor).strip().lower() not in ("null", "none", "n/a") else None
    return {"item": n, "event": True, "type": kind, "action": action, "vendor": vendor, "amount": amount, "what": what[:300],
            "quote": quote[:400], "unsure": unsure}, None


def sort_answers(objects: list[dict], items: dict[int, dict]) -> tuple[dict[int, list[dict]], dict[int, list[str]]]:
    """Objects grouped by item: {item: [clean answers]} for items whose every object passed, and {item: [reasons]} for
    items with a failed object or none at all. An item answered both "event" and "no event" keeps its events."""
    good: dict[int, list[dict]] = {}
    bad: dict[int, list[str]] = {}
    for obj in objects:
        clean, why = check(obj, items)
        if why:
            n = card_of(obj.get("item"), items)
            if n is not None:
                bad.setdefault(n, []).append(why)
            continue
        good.setdefault(clean["item"], []).append(clean)
    for n in items:
        if n not in good and n not in bad:
            bad[n] = ["no answer for this item"]
    for n in list(good):
        if n in bad:
            del good[n]
            continue
        events = [a for a in good[n] if a["event"]]
        good[n] = events or good[n][:1]
    return good, bad
