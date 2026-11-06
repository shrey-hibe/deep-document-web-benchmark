"""The pipeline's commands.

    uv run python -m ddwb.pipeline replay --from-run 20261105-nav3 --arm jev-only [--bodies cortez-co,...]
        [--from-arm haiku-jev-relay] [--run <id>] [--answers-from gateway|claude-code] [--jev-match text|exact]
        The $0 loop: the map of <from-run>:<from-arm>:<body> copied into a new session, then the pipeline on cached HTTP,
        cached Jev and cached model answers only (misses counted, never fetched or paid), scored against the truth with
        the miss breakdown. Development bodies only.

    uv run python -m ddwb.pipeline run --map-from <run>:<arm>:<body> --run <new run> --arm haiku-jev
        --backend gateway|claude-code --cap-reading USD --cap-model USD --approved-by "..."
        LIVE: documents fetched (our polite GET), Jev and the models called. Needs the owner's yes with its cost.

    uv run python -m ddwb.pipeline score --run <id> [--bodies ...]
"""

from __future__ import annotations

import argparse
from pathlib import Path
import json
import os
import sys
from datetime import UTC, datetime

from .. import paths
from ..deep import truth
from ..deep.chain import BODIES
from ..deep.state import State
from ..providers import cost as cost_mod
from ..providers import load_settings
from . import evaluate, replay
from .backends import (
    GATEWAY_MODELS,
    CacheOnlyBackend,
    ClaudeCodeBackend,
    GatewayBackend,
    JevDecider,
    ModelDecider,
    RecordedDecider,
    ResponseCache,
)
from .driver import ARMS, AUDIT, PipeCx, Pipeline, new_session

OUT = paths.DISCOVERY / "pipeline-runs"


def deciders(arm, backend, src=None, src_sid=None):
    if arm.decider == "jev":
        return JevDecider(), None
    if arm.decider == "recorded":
        return RecordedDecider(src, src_sid), None
    return ModelDecider(backend, arm.small), (ModelDecider(backend, arm.big) if arm.big else None)


def auditor(arm, backend):
    """The margin audit's reader (policy.toml [audit]), for the arms that tune per body."""
    return ModelDecider(backend, AUDIT["model"]) if arm.audit and arm.decider == "model" else None


def report(run: str, body: str, sid: str, answer: dict, extra: dict) -> dict:
    ev = evaluate.evaluate(run, body, sid)
    for line in evaluate.lines(body, ev, answer):
        print(line, flush=True)
    out = OUT / run
    out.mkdir(parents=True, exist_ok=True)
    doc = {"run": run, "session": sid, "at": datetime.now(UTC).isoformat(timespec="seconds"), **ev, "caches": extra,
           "pipeline": answer.get("pipeline"), "complete": answer.get("complete"), "incomplete_because": answer.get("incomplete_because")}
    (out / f"{body}.json").write_text(json.dumps(doc, indent=1, default=str))
    return doc


def t_replay(a) -> int:
    arm = ARMS[a.arm]
    bodies = [b for b in a.bodies.split(",") if b]
    bad = [b for b in bodies if b not in BODIES]
    if bad:
        raise SystemExit(f"replay is for the development bodies only ({', '.join(BODIES)}); not {', '.join(bad)}")
    run = a.run or f"{a.from_run}-replay-{a.arm}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
    src = replay.store_path(a.from_run)
    responses = ResponseCache(a.responses, read_only=True)
    backend = CacheOnlyBackend(responses, a.answers_from)
    jev = replay.ReplayJev(match=a.jev_match) if arm.jev else None
    print(f"REPLAY {run}: arm {arm.name}, map from {a.from_run}:{a.from_arm}, answers from the {a.answers_from} cache; "
          f"nothing is fetched or paid", flush=True)
    for body in bodies:
        src_sid = f"{a.from_run}:{a.from_arm}:{body}"
        sid = new_session(run, arm.name, body)
        st = State(run)
        copied = replay.copy_map(src, src_sid, st, sid, run, f"pipe-{arm.name}")
        http = replay.CacheOnlyHttp()
        if jev:
            jev.counts.clear()
            jev.miss_usd = jev.live_usd = 0.0
        cx = PipeCx(sid, jev_on=arm.jev, http=http, jev=jev, offline=True, profiled=arm.audit)
        small, big = deciders(arm, backend, src, src_sid)
        answer = Pipeline(cx, arm, small, big, auditor=auditor(arm, backend)).run()
        caches = {"map_copied": copied, "http": {"hits": http.hits, "misses": len(http.misses), "missed": http.misses[:10]},
                  "jev": dict(jev.counts) if jev else None, "jev_misses_estimate_usd": round(jev.miss_usd, 4) if jev else None,
                  "jev_all_live_estimate_usd": round(jev.live_usd, 4) if jev else None}
        print(f"  caches: {json.dumps(caches)}", flush=True)
        report(run, body, sid, answer, caches)
    print(f"reports: {OUT / run}", flush=True)
    return 0


BASELINE = Path(__file__).resolve().parents[3] / "evals" / "pipeline_baseline.json"


def ci_compare(current: dict, baseline: dict, tol_recall: float, tol_precision: float) -> list[str]:
    """Regressions of the current replay against the baseline, per body: recall or precision down by more than the
    tolerance, a body that no longer scores, or a replay that missed the caches (it would have fetched or paid)."""
    bad = []
    for body, b in baseline.items():
        c = current.get(body)
        if c is None:
            bad.append(f"{body}: not scored")
            continue
        for k, tol in (("recall", tol_recall), ("precision", tol_precision)):
            if b.get(k) is not None and (c.get(k) is None or c[k] < b[k] - tol):
                bad.append(f"{body}: {k} {c.get(k)} < baseline {b[k]} - {tol}")
        if c.get("cache_misses"):
            bad.append(f"{body}: {c['cache_misses']} cache misses (the replay is not complete at $0)")
    return bad


def t_ci(a) -> int:
    """Evals as CI (harness backlog item 2): the $0 replay of a cached run, scored and compared with the baseline;
    exits 1 on a regression. --update-baseline stores the current scores as the new baseline."""
    run = f"{a.from_run}-ci-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
    ns = argparse.Namespace(arm=a.arm, bodies=a.bodies, from_run=a.from_run, from_arm=a.from_arm, run=run,
                            answers_from=a.answers_from, jev_match="text", responses=a.responses)
    t_replay(ns)
    current = {}
    for body in [b for b in a.bodies.split(",") if b]:
        f = OUT / run / f"{body}.json"
        if not f.exists():
            continue
        doc = json.loads(f.read_text())
        br, caches = doc.get("breakdown") or {}, doc.get("caches") or {}
        misses = ((caches.get("http") or {}).get("misses") or 0) + sum(v for k, v in (caches.get("jev") or {}).items() if "miss" in k)
        misses += ((doc.get("throughput") or {}).get("model_calls_by_outcome") or {}).get("missed", 0)
        current[body] = {"recall": br.get("recall"), "precision": br.get("precision"), "cache_misses": misses}
    path = Path(a.baseline)
    if a.update_baseline:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"from_run": a.from_run, "arm": a.arm, "at": datetime.now(UTC).isoformat(timespec="seconds"),
                                    "bodies": current}, indent=1))
        print(f"CI: baseline written to {path}")
        return 0
    if not path.exists():
        raise SystemExit(f"no baseline at {path}: run once with --update-baseline")
    bad = ci_compare(current, json.loads(path.read_text())["bodies"], a.tol_recall, a.tol_precision)
    for body, c in current.items():
        print(f"CI {body}: recall {c['recall']}, precision {c['precision']}, cache misses {c['cache_misses']}")
    print("CI: " + ("FAIL\n  " + "\n  ".join(bad) if bad else "pass"))
    return 1 if bad else 0


def t_run(a) -> int:
    """A live run: needs the owner's yes. Documents are fetched by our polite GET, Jev and the models are called."""
    arm = ARMS[a.arm]
    if not (a.approved_by or "").strip():
        raise SystemExit("a live run needs --approved-by (who said yes to its cost)")
    src_run, src_arm, body = a.map_from.split(":")
    if a.backend == "gateway":
        prices = load_settings().prices.get("models")
        unpriced = [GATEWAY_MODELS[t] for t in (arm.small, arm.big) if t and cost_mod.model_price(GATEWAY_MODELS[t], prices) is None]
        if unpriced:
            raise SystemExit(f"no price for {', '.join(unpriced)}: add it under [prices.models] in providers.toml "
                             "(input_per_mtok, output_per_mtok) before a live run, so its cost is checked")
    sid = new_session(a.run, arm.name, body)
    st = State(a.run)
    st.fix_settings(caps={"map": 0.0, "reading": a.cap_reading, "model": a.cap_model}, approved_by=a.approved_by.strip())
    copied = replay.copy_map(replay.store_path(src_run), a.map_from, st, sid, a.run, f"pipe-{arm.name}")
    cx = PipeCx(sid, jev_on=arm.jev, profiled=arm.audit)
    backend = GatewayBackend(cx.providers) if a.backend == "gateway" else ClaudeCodeBackend()
    small, big = deciders(arm, backend, replay.store_path(src_run), a.map_from)
    answer = Pipeline(cx, arm, small, big, auditor=auditor(arm, backend)).run()
    print(f"RUN {sid}: map copied {json.dumps(copied)}; {len(answer['events'])} events; "
          f"{'complete' if answer['complete'] else 'INCOMPLETE: ' + '; '.join(answer['incomplete_because'])}", flush=True)
    print("  pipeline: " + json.dumps(answer["pipeline"]), flush=True)
    if (truth.folder(body) / "truth.json").exists():  # a development or a held-out body: scored, never tuned on
        report(a.run, body, sid, answer, {"map_copied": copied})
    return 0


def t_score(a) -> int:
    st = State(a.run)
    for body in [b for b in a.bodies.split(",") if b]:
        for (sid,) in st.db.execute("SELECT session FROM sessions WHERE body = ? AND arm LIKE 'pipe-%'", [body]).fetchall():
            answer = (st.get(sid, "A") or {}).get("data") or {}
            report(a.run, body, sid, answer, {})
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m ddwb.pipeline", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    r = sub.add_parser("replay", help="the $0 loop on cached data")
    r.add_argument("--from-run", required=True)
    r.add_argument("--from-arm", default="haiku-jev-relay", help="whose map (and, for --arm recorded, whose decisions)")
    r.add_argument("--arm", required=True, choices=list(ARMS))
    r.add_argument("--bodies", default=",".join(BODIES))
    r.add_argument("--run", help="the new run's ID (default: <from-run>-replay-<arm>-<time>)")
    r.add_argument("--answers-from", default=os.environ.get("DDWB_PIPELINE_BACKEND", "gateway"), choices=["gateway", "claude-code"])
    r.add_argument("--jev-match", default="text", choices=["text", "exact"])
    r.add_argument("--responses", help="the response cache (default data/pipeline/responses.sqlite)")
    x = sub.add_parser("run", help="LIVE: needs the owner's yes")
    x.add_argument("--map-from", required=True, help="<run>:<arm>:<body>, a session whose map is built")
    x.add_argument("--run", required=True)
    x.add_argument("--arm", required=True, choices=[k for k, v in ARMS.items() if v.decider == "model"] + ["jev-only"])
    x.add_argument("--backend", default=os.environ.get("DDWB_PIPELINE_BACKEND", "gateway"), choices=["gateway", "claude-code"])
    x.add_argument("--cap-reading", type=float, required=True, help="USD for Jev")
    x.add_argument("--cap-model", type=float, required=True, help="USD for the models (cash; Claude Code on the Max plan is $0)")
    x.add_argument("--approved-by", required=True)
    c = sub.add_parser("ci", help="evals as CI: replay a cached run at $0 and compare with the baseline")
    c.add_argument("--from-run", required=True, help="a live run whose map and answers are cached")
    c.add_argument("--from-arm", required=True, help="that run's session arm, e.g. pipe-haiku-jev")
    c.add_argument("--arm", required=True, choices=list(ARMS))
    c.add_argument("--bodies", default=",".join(BODIES))
    c.add_argument("--answers-from", default=os.environ.get("DDWB_PIPELINE_BACKEND", "gateway"), choices=["gateway", "claude-code"])
    c.add_argument("--responses", help="the response cache (default data/pipeline/responses.sqlite)")
    c.add_argument("--baseline", default=str(BASELINE))
    c.add_argument("--update-baseline", action="store_true")
    c.add_argument("--tol-recall", type=float, default=0.02)
    c.add_argument("--tol-precision", type=float, default=0.03)
    s = sub.add_parser("score")
    s.add_argument("--run", required=True)
    s.add_argument("--bodies", default=",".join(BODIES))
    return p


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    return {"replay": t_replay, "run": t_run, "score": t_score, "ci": t_ci}[a.command](a)


if __name__ == "__main__":
    sys.exit(main())
