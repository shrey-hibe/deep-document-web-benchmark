"""A pipeline session against the truth: `truth.score` (recall, precision, fields, on the sampled meetings), and the miss
breakdown every run prints:

    each true decision not found:  meeting not on the map / no document opened / item not found in the document /
                                   set aside as procedural / set aside by Jev / model said not an event /
                                   left undecided (and why) / recorded with wrong fields
    each extra event (on the sample, matching no true decision):  duplicate / not a decision
    and the events outside the sample (other meetings, not scored), and those matching a decision the truth sets aside
    (not buying).

The truth is read from work/deep-discovery/truth/<body>/ for the development bodies only.
"""

from __future__ import annotations

import json
from collections import Counter

from ..deep import truth
from ..deep.state import State
from ..deep.tools import governing
from . import ledger
from .form import norm

MISSES = ("meeting not on the map", "no document opened", "item not found in the document", "set aside as procedural",
          "set aside by Jev", "model said not an event", "left undecided", "item holds several decisions; fewer recorded",
          "recorded with wrong fields", "other")


def truth_of(body: str) -> tuple[list[dict], list[dict], set[str]]:
    if not (truth.folder(body) / "truth.json").exists():  # development bodies and held-out bodies alike (scored, never tuned on)
        raise SystemExit(f"{body} has no truth set")
    every = json.loads((truth.folder(body) / "truth.json").read_text())["decisions"]
    flagged = any("buyer" in d for d in every)
    scored = [d for d in every if d.get("buyer") is True] if flagged else every
    aside = [d for d in every if d.get("buyer") is not True] if flagged else []
    dates = {m["date"] for m in json.loads((truth.folder(body) / "sample.json").read_text())["sampled"]}
    return scored, aside, dates


def windows(q: str, width: int = 45) -> list[str]:
    """Pieces of a quote to look for in a passage: its start, middle and end (a quote can cross a page break)."""
    if len(q) <= width:
        return [q] if len(q) >= 15 else []
    mid = len(q) // 2
    return [q[:width], q[mid - width // 2: mid + width // 2], q[-width:]]


def passages_for(t: dict, ps: list[dict]) -> list[dict]:
    """The session's passages that hold a true decision: by its quote, else its item number, else its amount."""
    pieces = windows(norm(t.get("quote")))
    hit = [p for p in ps if any(w in norm(p["data"].get("text")) for w in pieces)]
    if hit:
        return hit
    item = truth.norm_item(t.get("item"))
    if item:
        hit = [p for p in ps if truth.norm_item(p["data"].get("item_no") or p["data"].get("item_number")) == item]
        if hit:
            return hit
    if t.get("amount"):
        hit = [p for p in ps if any(abs(a - t["amount"]) <= 0.01 * t["amount"] for a in p["data"].get("amounts") or [])
               and len(truth.words(t.get("what")) & truth.words(p["data"].get("text"))) >= 2]
    return hit


def why_missed(t: dict, links: list[dict], ps: list[dict], paired_events: set[str]) -> str:
    when = t["date"]
    day = [n for n in links if (n.get("data") or {}).get("date") == when]
    if not any(governing(n) for n in day):
        return "meeting not on the map"
    if not any(n["status"] == "read" for n in day):
        return "no document opened"
    mine = passages_for(t, [p for p in ps if p["data"].get("date") == when])
    if not mine:
        return "item not found in the document"
    verdicts = [(p["data"].get("pipeline") or {}).get("verdict") for p in mine]
    events = {e for p in mine for e in (p["data"].get("events") or []) + ((p["data"].get("pipeline") or {}).get("events") or [])}
    if events or "event" in verdicts:
        if events and events <= paired_events:  # every event from the item found another true decision
            return "item holds several decisions; fewer recorded"
        return "recorded with wrong fields"
    if "not_event" in verdicts:
        return "model said not an event"
    if "undecided" in verdicts or any(p["status"] == "kept" for p in mine):
        why = next((p["data"].get("undecided") for p in mine if p["data"].get("undecided")), "")
        return "left undecided" + (f" ({why.split(':')[0]})" if why else "")
    if "set_aside_jev" in verdicts or any(p["data"].get("skipped_on_jev") for p in mine):
        return "set aside by Jev"
    if "procedural" in verdicts:
        return "set aside as procedural"
    return "other"


def breakdown(run: str, body: str, sid: str) -> dict:
    scored, aside, dates = truth_of(body)
    st = State(run)
    links = st.nodes(sid, "link")
    ps = st.nodes(sid, "passage")
    evs = [e["data"] | {"node": e["node"]} for e in st.nodes(sid, "event", "recorded")]
    on_sample = [e for e in evs if e.get("date") in dates]
    missed, extras, pairs, matched_aside = [], [], [], 0
    for d in sorted(dates):
        p, t_only, a_only = truth.match([t for t in scored if t["date"] == d], [e for e in on_sample if e.get("date") == d],
                                        truth.arm_similarity)
        q, _, a_only = truth.match([t for t in aside if t["date"] == d], a_only, truth.arm_similarity)
        pairs += p
        missed += t_only
        extras += a_only
        matched_aside += len(q)
    paired_events = {e["node"] for _, e in pairs}
    reasons = Counter()
    examples: dict[str, list] = {}
    for t in missed:
        why = why_missed(t, links, ps, paired_events)
        reasons[why] += 1
        examples.setdefault(why, []).append({k: t.get(k) for k in ("date", "item", "type", "amount", "what")})
    extra = Counter()
    extra_examples: dict[str, list] = {}
    paired_truth = [t for t, _ in pairs]
    for e in extras:
        dup = any(truth.arm_similarity(t, e) >= 2.0 for t in paired_truth if t["date"] == e.get("date")) or \
            any(o.get("passage") == e.get("passage") for _, o in pairs)
        kind = "duplicate" if dup else "not a decision"
        extra[kind] += 1
        extra_examples.setdefault(kind, []).append({k: e.get(k) for k in ("date", "type", "action", "amount", "what")})
    return {"truth": len(scored), "found": len(pairs), "missed": len(missed),
            "recall": round(len(pairs) / len(scored), 3) if scored else None,
            "precision": round(len(pairs) / (len(pairs) + len(extras)), 3) if pairs or extras else None,
            "missed_because": dict(reasons), "extra_events": dict(extra),
            "events_outside_sample": len(evs) - len(on_sample), "events_matching_set_aside_truth": matched_aside,
            "type_right": sum(1 for t, e in pairs if t.get("type") == e.get("type")),
            "action_right": sum(1 for t, e in pairs if t.get("action") == e.get("action")),
            "missed_examples": {k: v[:5] for k, v in examples.items()}, "extra_examples": {k: v[:5] for k, v in extra_examples.items()}}


def evaluate(run: str, body: str, sid: str) -> dict:
    s = truth.score(run, body)
    arm = sid.split(":")[1]
    b = breakdown(run, body, sid)
    return {"score": s["arms"].get(arm), "breakdown": b, "chain": ledger.chain(b), "throughput": ledger.throughput(run, sid)}


def lines(body: str, ev: dict, answer: dict | None = None) -> list[str]:
    b, s = ev["breakdown"], ev.get("score") or {}
    out = [f"{body}: recall {b['recall']} ({b['found']}/{b['truth']}), precision {b['precision']}; type right {b['type_right']}, "
           f"action right {b['action_right']}, amount right {s.get('amount_right')}; events {s.get('events_all_meetings')} "
           f"({s.get('events_on_sample')} on the sample)"]
    out.append("  missed because: " + (", ".join(f"{k} {v}" for k, v in sorted(b["missed_because"].items(), key=lambda x: -x[1])) or "none"))
    if ev.get("chain"):
        out += ledger.lines(ev["chain"], ev["throughput"])
    out.append("  extra events: " + (", ".join(f"{k} {v}" for k, v in b["extra_events"].items()) or "none")
               + f"; outside the sample {b['events_outside_sample']}; matching a decision the truth sets aside (not buying) "
                 f"{b['events_matching_set_aside_truth']}")
    if answer:
        p = answer.get("pipeline") or {}
        out.append("  pipeline: " + json.dumps(p.get("stats")))
        if p.get("undecided"):
            out.append("  undecided: " + json.dumps(p["undecided"]))
        out.append("  money: " + json.dumps(p.get("money")) + ("" if answer.get("complete") else
                                                               "; INCOMPLETE: " + "; ".join(answer.get("incomplete_because") or [])[:400]))
    return out
