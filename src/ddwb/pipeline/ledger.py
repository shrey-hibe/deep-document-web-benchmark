"""The run's ledger read as a chain: where true decisions survive or leak, stage by stage, what each stage handled and
what it cost, and which model versions answered (harness backlog items 1 and 7, from "Harness to AI Employee").

Each true decision moves through the same states: its meeting is on the map, a document of the meeting is opened, an
item holding it is found, the item is sent to the model (not set aside by Jev or as procedural), an event is recorded, and
the event's fields are right. `evaluate.why_missed` gives each missed decision the first state it failed to reach, so the
chain is the survivors at each state; the leaks are where to work next. Volume and cost come from the store's own
records: links, passages and their verdicts, model calls (`call` nodes: model, cache, outcome) and the events table.
"""

from __future__ import annotations

from collections import Counter

from ..deep.state import State

STAGES = ("on the map", "document opened", "item found", "sent to the model", "event recorded", "fields right")
LEAKS = {  # the first state a missed decision failed to reach -> the stage it leaked at
    "meeting not on the map": "on the map", "no document opened": "document opened",
    "item not found in the document": "item found", "set aside by Jev": "sent to the model",
    "set aside as procedural": "sent to the model", "model said not an event": "event recorded",
    "item holds several decisions; fewer recorded": "event recorded", "other": "event recorded",
    "recorded with wrong fields": "fields right",
}


def leak_stage(reason: str) -> str:
    return LEAKS.get(reason) or ("event recorded" if reason.startswith("left undecided") else "event recorded")


def chain(breakdown: dict) -> list[tuple[str, int, int]]:
    """(stage, reached, leaked here) for each stage, from the miss breakdown: every true decision starts at the first
    stage; a found one reaches the last with its fields counted as right only when type and action match."""
    lost = Counter()
    for reason, n in breakdown["missed_because"].items():
        lost[leak_stage(reason)] += n
    wrong_fields = breakdown["found"] - min(breakdown["type_right"], breakdown["action_right"])
    lost["fields right"] += max(wrong_fields, 0)
    out, alive = [], breakdown["truth"]
    for stage in STAGES:
        out.append((stage, alive - lost[stage], lost[stage]))
        alive -= lost[stage]
    return out


def throughput(run: str, sid: str) -> dict:
    """What each stage handled and what it cost, from the store."""
    st = State(run)
    links = st.nodes(sid, "link")
    ps = st.nodes(sid, "passage")
    calls = st.nodes(sid, "call")
    verdicts = Counter(((p.get("data") or {}).get("pipeline") or {}).get("verdict") or p["status"] for p in ps)
    by_model = Counter(((c.get("data") or {}).get("by") or "?") for c in calls)
    cached = sum(1 for c in calls if (c.get("data") or {}).get("cached"))
    cost = {b or "none": round(v or 0.0, 4) for b, v in st.db.execute(
        "SELECT budget, sum(cost_usd) FROM events WHERE session = ? GROUP BY budget", [sid]).fetchall()}
    return {"links": len(links), "documents_read": sum(1 for n in links if n["status"] == "read"),
            "items": len(ps), "items_by_verdict": dict(verdicts),
            "model_calls": len(calls), "model_calls_cached": cached,
            "model_calls_by_outcome": dict(Counter(c["status"] for c in calls)),
            "models": dict(by_model),  # model versions that answered (backlog item 7)
            "events": len(st.nodes(sid, "event", "recorded")), "cost_usd_by_budget": cost}


def lines(ch: list[tuple[str, int, int]], tp: dict) -> list[str]:
    start = ch[0][1] + ch[0][2] if ch else 0
    out = ["  chain: " + " → ".join(f"{s} {reached}/{start}" + (f" (−{leak})" if leak else "") for s, reached, leak in ch)]
    worst = max(ch, key=lambda x: x[2]) if ch else None
    if worst and worst[2]:
        out.append(f"  biggest leak: {worst[0]} ({worst[2]} true decisions)")
    out.append(f"  handled: {tp['documents_read']} documents, {tp['items']} items {tp['items_by_verdict']}, "
               f"{tp['model_calls']} model calls ({tp['model_calls_cached']} cached; {tp['model_calls_by_outcome']}), "
               f"{tp['events']} events; models {tp['models']}; cost {tp['cost_usd_by_budget']}")
    return out
