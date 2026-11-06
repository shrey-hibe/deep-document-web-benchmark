"""A body's profile: the tuned values the tools and the pipeline read, as a function of the body (decision #126).

Each knob (policy.toml [profile]: the Jev cut, the check threshold, the procedural limit, the gap guard) starts at its
default and moves only on evidence recorded with the move, from three sources:
- the body's own records, measured by code: the gap guard from the governing board's own meeting rhythm (`cadence`);
- the harness: the pipeline's margin audit, a stronger model reading what the cheap filter set aside nearest the cut;
- an agent, with the passage or link that shows it (`nav <session> profile set <knob> <value> --evidence <id>`).
A value never leaves its bounds, and nothing is learnt from a truth set. The profile in force is kept in the session's
store (node T), so a replay reads the values the run used, and in data/profiles/<body>.json, where the body's next nav
session starts from it. A session that runs fixed (`cx.profiled` false: a benchmark's fixed arm) reads the defaults
and moves nothing.
"""

from __future__ import annotations

import json
from statistics import quantiles

from .. import policy
from . import state as nav_state
from .state import now

NODE = "T"  # the session's profile node ("B" is the body)
CADENCE_MIN = 8  # meetings on the map before the board's rhythm sets the gap guard


def knobs() -> dict[str, dict]:
    return policy.profile()


def clamp(name: str, value: float) -> float:
    k = knobs()[name]
    out = min(max(float(value), k["low"]), k["high"])
    return int(out) if isinstance(k["default"], int) else round(out, 4)


def path(body: str):
    return nav_state.NAV.parent / "profiles" / f"{body}.json"  # beside the nav stores (data/profiles/)


def carried(body: str) -> dict | None:
    f = path(body)
    try:
        return json.loads(f.read_text()) if f.exists() else None
    except (OSError, ValueError):
        return None


def _load(cx) -> dict:
    prof = cx.__dict__.get("_profile")
    if prof is not None:
        return prof
    node = cx.st.get(cx.sid, NODE)
    data = (node or {}).get("data") or {}
    prof = {"values": dict(data.get("values") or {}), "log": list(data.get("log") or [])}
    cx.__dict__["_profile"] = prof
    if node is None and getattr(cx, "carry", True):
        old = carried(cx.body)
        values = {k: clamp(k, v) for k, v in ((old or {}).get("values") or {}).items() if k in knobs()}
        if values:
            prof["values"] = values
            prof["log"].append({"knob": "*", "to": values, "source": "carried", "evidence": old.get("from"),
                                "reason": "this body's last profile", "at": now(), "session": cx.sid})
            _save(cx, prof)
    return prof


def _save(cx, prof: dict) -> None:
    cx.st.put(cx.sid, NODE, "profile", "in_force", tool="profile", data=prof)
    f = path(cx.body)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"body": cx.body, "from": cx.sid, "values": prof["values"], "log": prof["log"][-40:]}, indent=1))


def value(cx, name: str) -> float:
    """The knob's value for this body (its default when the session runs fixed or nothing moved it)."""
    default = knobs()[name]["default"]
    if not getattr(cx, "profiled", True):
        return default
    return _load(cx)["values"].get(name, default)


def values(cx) -> dict[str, float]:
    return {name: value(cx, name) for name in knobs()}


def log(cx) -> list[dict]:
    return _load(cx)["log"] if getattr(cx, "profiled", True) else []


def move(cx, name: str, to: float, *, source: str, evidence, reason: str) -> float | None:
    """Set a knob for this body, inside its bounds, with what shows it. Returns the value now in force, or None when the
    session runs fixed (nothing moves)."""
    if name not in knobs():
        raise KeyError(f"no knob {name!r} (knobs: {', '.join(knobs())})")
    if not getattr(cx, "profiled", True):
        return None
    prof = _load(cx)
    before = prof["values"].get(name, knobs()[name]["default"])
    after = clamp(name, to)
    if after != before:
        prof["values"][name] = after
        prof["log"].append({"knob": name, "from": before, "to": after, "source": source, "evidence": evidence,
                            "reason": reason, "at": now(), "session": cx.sid})
        _save(cx, prof)
    return after


def note(cx, name: str, *, source: str, evidence, reason: str) -> None:
    """Evidence that leaves the knob where it is (a band read clean): logged, so the next run sees it was checked."""
    if not getattr(cx, "profiled", True):
        return
    prof = _load(cx)
    at = prof["values"].get(name, knobs()[name]["default"])
    prof["log"].append({"knob": name, "from": at, "to": at, "source": source, "evidence": evidence, "reason": reason,
                        "at": now(), "session": cx.sid})
    _save(cx, prof)


def cadence(cx, dates: list[str]) -> float | None:
    """The gap guard from the board's own rhythm: twice the longest usual interval between its meetings on the map (the
    90th percentile, so one missing stretch does not set it), once there are CADENCE_MIN meetings."""
    from datetime import date
    days = sorted(dates)
    if len(days) < CADENCE_MIN:
        return None
    gaps = [(date.fromisoformat(b) - date.fromisoformat(a)).days for a, b in zip(days, days[1:])]
    usual = quantiles(gaps, n=10)[-1]
    return move(cx, "gap_days", 2 * usual, source="measured",
                evidence=f"{len(days)} governing meetings on the map, 90% of them at most {usual:.0f} days apart",
                reason="the board's own rhythm: a stretch twice its usual longest interval means a missing view")
