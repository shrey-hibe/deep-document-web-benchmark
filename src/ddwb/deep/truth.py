"""The truth set for the four-arm test: the buying decisions each body's governing board recorded in a random sample of
its meetings since 2025-01-01, read in full by two blind judges.

    frame      the board's meetings with minutes posted, from the reference map (run 20261103-truth, arm test), with the
               meetings per year set against the counts the discovery traces already know
    sample     8 meetings per body, drawn with a recorded seed
    collect    each sampled meeting's minutes downloaded and every page extracted, no filter (text in data/truth/)
    export-a   judge A: Claude Code subagents (Sonnet, Max plan) read the same chunks from files
    judge-b    judge B: DeepSeek v4.1 Flash through the provider layer (the reading cap, --cap), blind to judge A
    merge      decisions matched within a meeting: item number, else amount, else vendor and words; both judges agree ->
               truth; one judge only -> the adjudicator (export-c: an Opus subagent with the page); truth = agreed +
               adjudicated yes

The truth is defined as the minutes' record. Minutes are the board's official record of its actions and list each one,
consent items included, with what was approved; arms may find a decision in any document and are scored on whether they
found the decision. Arms never see this folder or data/truth/.

    uv run python -m ddwb.deep.truth frame|sample|collect|export-a|judge-b|import-a|merge|export-c|import-c [--bodies ...]
        [--cap USD] [--approved-by "..."]
"""

from __future__ import annotations

import argparse
import json
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

from .. import paths
from ..providers import Providers
from . import web
from .chain import BODIES
from .forks import JUDGE_B_MODEL, parse_json
from .state import State, now
from .tools import DEFAULT_CAPS, SINCE, Http, doc_format, ocr_pages, office_text, pdf_pages

RUN = "20261103-truth"  # the development bodies' truth run; `--truth-run` names another (the held-out bodies')
WORK = paths.DISCOVERY / "truth"
HELDOUT = paths.DISCOVERY / "heldout"  # held-out bodies: bodies.json (the draw) and <body>/frame.json (meetings, by hand)
KIND = {"counties": "county", "cities_towns": "city or town", "k12": "school district", "water": "water district",
        "fire": "fire protection district", "transit": "transit authority"}
TEXT = paths.DATA / "truth"
SEED = "truth-2026-11-03"
N_MEETINGS = 8
CHUNK_CHARS = 60_000  # about 15,000 tokens a chunk, split at page breaks: every page is read
PAUSE_MINUTES = 15  # a longer gap between two tool calls is a pause (a session left waiting, then resumed), not work
SETTINGS = {"cap": DEFAULT_CAPS["reading"], "approved_by": "", "run": RUN}  # reading cap (USD), approver, run: set by main()


def approval() -> dict:
    return {"approved_by": SETTINGS["approved_by"]} if SETTINGS["approved_by"] else {}
KNOWN = {  # governing meetings per year, from the discovery traces (work/deep-discovery/*/<body>.json)
    "cortez-co": {2025: 22, 2026: 20}, "summit-county-oh": {2025: 22, 2026: 19}, "pueblo-library-co": {2025: 13, 2026: 10},
    "pylusd-ca": {},
}
SESSION = re.compile(r"work ?session|worksession|study session|workshop|retreat", re.I)
TYPES = ("contract", "renewal", "bid", "grant", "purchase", "budget")
ACTIONS = ("approved", "rejected_or_tabled", "to_consider", "reported_only", "unclear")

JUDGE = """You are reading pages of the official minutes of a meeting of {name}, a {kind} in {state}, held on {date} (part {part} of {parts}). List every buying decision the minutes record on these pages.

A buying decision is an action by the board on: a purchase; a contract or agreement; a renewal, extension or amendment; a bid, RFP or RFQ (authorising, issuing or awarding it); a grant or outside funding; or a budget, appropriation or transfer of funds. Staffing decisions (hiring, appointments, resignations) are not buying decisions. Give one decision per agenda item or motion: a list of purchase orders, warrants or claims approved together is one decision. Reports attached to the minutes (audits, financial statements, presentations) are not decisions.

For each decision give:
- item: the item number as printed (e.g. "24.1", "7b", "Resolution 2026-123"), or "" if there is none
- page: the [page N] it is on
- type: one of contract, renewal, bid, grant, purchase, budget
- action: one of approved, rejected_or_tabled, to_consider, reported_only, unclear
- vendor: the company, contractor, grantor or person named, or "" if none
- amount: the dollar amount as a number (the total if several), or null if none
- what: a few words on what was bought or decided
- quote: the minutes' words that record it, at most 200 characters

Also list every people decision the minutes record on these pages: someone hired, appointed, elected, promoted or transferred; a resignation, retirement, dismissal, non-renewal or leave; or pay or contract terms set for named staff or officials. Give one entry per person per decision (a list of hires approved together gives one entry per person named). For each give:
- item: the item number as printed, or ""
- page: the [page N] it is on
- name: the person's name as written
- role: the position or title as written, or ""
- change: one of hire_or_appoint, departure, move, pay_or_terms
- action: one of approved, rejected_or_tabled, to_consider, reported_only, unclear
- quote: the minutes' words that record it, at most 200 characters

Reply with JSON only, no other text: {{"decisions": [ ... ], "people": [ ... ]}} (empty lists if these pages record none).

PAGES:
{text}"""


def say(*lines) -> None:
    print("\n".join(str(x) for x in lines), flush=True)


def session(body: str) -> tuple[State, str]:
    return State(RUN), f"{RUN}:test:{body}"


def folder(body: str):
    f = WORK / body
    f.mkdir(parents=True, exist_ok=True)
    return f


# ------------------------------------------------------------------------------------------- frame and sample
def heldout_frame(body: str) -> dict | None:
    path = HELDOUT / body / "frame.json"
    if not path.exists():
        return None
    fr = json.loads(path.read_text())
    drawn = {b["slug"]: b for b in json.loads((HELDOUT / "bodies.json").read_text())}.get(body, {})
    card = {"name": fr.get("name") or drawn.get("name"), "state": fr.get("state") or drawn.get("state"),
            "kind": KIND.get((drawn.get("slot") or "").rsplit("-", 1)[0], "public body"), "board": fr.get("board")}
    meetings: dict[str, dict] = {}
    for m in fr.get("meetings") or []:
        when = m.get("date") or ""
        if not (SINCE.isoformat() <= when <= date.today().isoformat()) or m.get("kind") not in ("regular", "special") \
                or SESSION.search(m.get("title") or ""):
            continue
        e = meetings.setdefault(when, {"date": when, "minutes": [], "documents": 0, "titles": []})
        if m.get("minutes_listed_now") is False:  # still online but no longer linked from the site: no navigator can find it
            m = m | {"minutes": []}
        e["minutes"] += [{"node": None, "url": x["url"], "text": x.get("text")} for x in m.get("minutes") or [] if x.get("url")]
        e["documents"] += len(m.get("minutes") or []) + len(m.get("agenda") or [])
        e["titles"].append((m.get("title") or "")[:120])
    per_year = {}
    for when in meetings:
        per_year[int(when[:4])] = per_year.get(int(when[:4]), 0) + 1
    with_minutes = sorted((m for m in meetings.values() if m["minutes"]), key=lambda m: m["date"])
    return {"body": body, "card": card, "meetings_per_year": per_year, "known_per_year": fr.get("per_year") or {},
            "meetings": len(meetings), "with_minutes": len(with_minutes), "frame": with_minutes}


def frame(body: str) -> dict:
    held = heldout_frame(body)
    if held:
        return held
    st, sid = session(body)
    sess = st.session(sid)
    if not sess:
        raise SystemExit(f"no reference map for {body}: map it in session {sid} first")
    meetings: dict[str, dict] = {}
    for n in st.nodes(sid, "link"):
        d = n["data"]
        when = d.get("date")
        if not when or when < SINCE.isoformat() or when > date.today().isoformat() or not d.get("rule_governing"):
            continue
        # the board's business meetings: its own agenda or minutes for that date; work and study sessions take no votes,
        # and reports carry the date of the period they cover, not of a meeting
        if d.get("rule_kind") not in ("agenda", "packet", "minutes") or SESSION.search(f"{d.get('text')} {d.get('row')}"):
            continue
        m = meetings.setdefault(when, {"date": when, "minutes": [], "documents": 0, "titles": []})
        m["documents"] += 1
        if d.get("rule_kind") == "minutes" and not re.search(r"\.(zip|docx?)($|\?)", n["url"] or "", re.I):
            m["minutes"].append({"node": n["node"], "url": n["url"], "text": d.get("text")})
        if len(m["titles"]) < 3 and d.get("row"):
            m["titles"].append(d["row"][:120])
    per_year = {}
    for when in meetings:
        per_year[int(when[:4])] = per_year.get(int(when[:4]), 0) + 1
    with_minutes = sorted((m for m in meetings.values() if m["minutes"]), key=lambda m: m["date"])
    return {"body": body, "card": sess["card"], "meetings_per_year": per_year, "known_per_year": KNOWN.get(body, {}),
            "meetings": len(meetings), "with_minutes": len(with_minutes), "frame": with_minutes}


def t_frame(bodies: list[str]) -> None:
    for b in bodies:
        f = frame(b)
        say(f"{b}: {f['meetings']} governing meeting dates since {SINCE} (per year {f['meetings_per_year']}; known "
            f"{f['known_per_year'] or 'no count on record'}); {f['with_minutes']} with minutes posted")


def t_sample(bodies: list[str]) -> None:
    for b in bodies:
        path = folder(b) / "sample.json"
        if path.exists():
            say(f"{b}: sample already drawn ({path}); not redrawn")
            continue
        f = frame(b)
        rng = random.Random(f"{SEED}:{b}")
        chosen = sorted(rng.sample([m["date"] for m in f["frame"]], min(N_MEETINGS, len(f["frame"]))))
        out = {"body": b, "seed": f"{SEED}:{b}", "drawn_at": now(), **approval(), "n": len(chosen),
               "meetings_per_year": f["meetings_per_year"], "known_per_year": f["known_per_year"],
               "frame_size": len(f["frame"]), "frame": [m["date"] for m in f["frame"]],
               "sampled": [m for m in f["frame"] if m["date"] in chosen]}
        path.write_text(json.dumps(out, indent=1))
        say(f"{b}: {len(chosen)} of {len(f['frame'])} meetings with minutes: {', '.join(chosen)}")


# ------------------------------------------------------------------------------------------- collect
def t_collect(bodies: list[str]) -> None:
    http = Http()
    for b in bodies:
        sample = json.loads((folder(b) / "sample.json").read_text())
        collected = []
        for m in sample["sampled"]:
            pages_all, docs = [], []
            for i, doc in enumerate(m["minutes"], start=1):
                resp = http.get(web.direct_url(doc["url"]))
                if not resp.ok:
                    docs.append({"url": doc["url"], "error": resp.error or f"HTTP {resp.status}"})
                    continue
                fmt = doc_format(resp, doc["url"])  # the navigator's own format check: pdf, txt, doc, docx, rtf, html, binary
                rel, sha = web.save_download(b, f"{m['date']}-minutes-{resp.sha256[:8]}.{'bin' if fmt == 'binary' else fmt}", resp.body)
                if fmt == "binary":
                    docs.append({"url": doc["url"], "error": f"a format the tools cannot read ({resp.content_type})"})
                    continue
                if fmt == "pdf":
                    pages, total = pdf_pages(rel, None)
                elif fmt == "txt":
                    pages, total = [(1, resp.text, False)], 1
                elif fmt in ("doc", "docx", "rtf"):
                    body = office_text(rel)
                    if body is None:
                        docs.append({"url": doc["url"], "error": f"could not read the {fmt} file"})
                        continue
                    pages, total = [(1, body, False)], 1
                else:
                    pages, total = [(1, web.read_html(resp.text, resp.final_url or doc["url"]).text, False)], 1
                no_text = [p for p, md, ocr in pages if ocr or len(md.strip()) < 30] if fmt == "pdf" else []
                texts = ocr_pages(rel, no_text) if no_text else {}  # pages with no text layer: macOS OCR (PDFs only)
                pages = [(p, f"[OCR]\n{texts[p]}" if texts.get(p, "").strip() else md, o) for p, md, o in pages]
                ocred = [p for p in no_text if texts.get(p, "").strip()]
                docs.append({"url": doc["url"], "path": rel, "sha256": sha, "pages": total, "pages_without_text": no_text,
                             "pages_read_by_ocr": ocred, "chars": sum(len(md) for _, md, _ in pages)})
                pages_all += [(f"minutes {i} page {p}" if len(m["minutes"]) > 1 else f"page {p}", md) for p, md, _ in pages]
            chunks, cur, size = [], [], 0
            for label, md in pages_all:
                block = f"[{label}]\n{md}\n"
                if cur and size + len(block) > CHUNK_CHARS:
                    chunks.append("".join(cur))
                    cur, size = [], 0
                cur.append(block)
                size += len(block)
            if cur:
                chunks.append("".join(cur))
            out = TEXT / b / m["date"]
            out.mkdir(parents=True, exist_ok=True)
            for n, c in enumerate(chunks, start=1):
                (out / f"chunk-{n:02d}.md").write_text(c)
            collected.append({"date": m["date"], "documents": docs, "chunks": len(chunks), "chars": sum(len(c) for c in chunks)})
            say(f"  {b} {m['date']}: {len(docs)} minutes file(s), {sum(d.get('pages', 0) for d in docs)} pages, {len(chunks)} chunks"
                + "".join(f"; {len(d["pages_without_text"])} pages without text, {len(d["pages_read_by_ocr"])} read by OCR" for d in docs if d.get("pages_without_text"))
                + "".join(f"; NOT READ {d['url']}: {d['error']}" for d in docs if d.get("error")))
        (folder(b) / "collected.json").write_text(json.dumps({"body": b, "collected_at": now(), "meetings": collected}, indent=1))


def chunk_items(b: str) -> list[dict]:
    sample = json.loads((folder(b) / "sample.json").read_text())
    card = sample.get("card") or frame(b)["card"]
    collected = json.loads((folder(b) / "collected.json").read_text())
    items = []
    for m in collected["meetings"]:
        for n in range(1, m["chunks"] + 1):
            path = TEXT / b / m["date"] / f"chunk-{n:02d}.md"
            items.append({"chunk": f"{b}:{m['date']}:{n:02d}", "body": b, "date": m["date"], "part": n, "parts": m["chunks"],
                          "path": str(path), "name": (card.get("name") or b).title(), "kind": card.get("kind", "public body"),
                          "state": card.get("state", "")})
    return items


def prompt(it: dict, text: str) -> str:
    return JUDGE.format(name=it["name"], kind=it["kind"].replace("_", " "), state=it["state"], date=it["date"], part=it["part"],
                        parts=it["parts"], text=text)


# ------------------------------------------------------------------------------------------- judges
def t_export_a(bodies: list[str]) -> None:
    """Judge A's work list: one file per body, each line a chunk to read (its file) and where the answer goes."""
    for b in bodies:
        items = chunk_items(b)
        with (folder(b) / "judge_a.jsonl").open("w") as h:
            for it in items:
                h.write(json.dumps({"chunk": it["chunk"], "path": it["path"], "instructions": prompt(it, "<the file's text>")}) + "\n")
        say(f"{b}: {len(items)} chunks for judge A → {folder(b) / 'judge_a.jsonl'}; answers go to judge_a.answers.jsonl")


def t_judge_b(bodies: list[str]) -> None:
    run = SETTINGS["run"]
    st = State(run)
    cap = SETTINGS["cap"]
    left = cap - st.spent("reading")
    # the provider's ledger counts this run's earlier spend itself: give it the whole cap, not what is left of it
    providers = Providers(run_id=f"{run}-judge-b", max_usd=cap, max_calls=None, approved_by=SETTINGS["approved_by"])
    for b in bodies:
        out = folder(b) / "judge_b.jsonl"
        done = {r["chunk"] for r in map(json.loads, out.read_text().splitlines()) if r.get("decisions") is not None} if out.exists() else set()
        todo = [it for it in chunk_items(b) if it["chunk"] not in done]

        def one(it):
            started = time.time()
            try:
                res = providers.reason([{"role": "user", "content": prompt(it, open(it["path"]).read())}], JUDGE_B_MODEL,
                                       max_tokens=16000, temperature=0)
                resp = res.response or {}
                content = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content")
                parsed = parse_json(content)
                return it, {"decisions": (parsed or {}).get("decisions") if parsed else None,
                            "people": (parsed or {}).get("people") if parsed else None, "cost_usd": res.cost,
                            "error": None if parsed else f"no JSON: {str(content)[:200]}", "usage": resp.get("usage")}, started
            except Exception as error:
                return it, {"decisions": None, "cost_usd": 0.0, "error": f"{type(error).__name__}: {str(error)[:300]}"}, started

        if st.spent("reading") >= cap:
            raise SystemExit("the reading cap is used up")
        with ThreadPoolExecutor(max_workers=6) as pool, out.open("a") as h:
            for it, r, started in pool.map(one, todo):
                st.put(f"{run}:judge-b:{b}", f"J{it['chunk'][-14:].replace(':', '').replace('-', '')[:7]}", "judge", "done",
                       tool="judge-b", cost=r["cost_usd"] or 0.0, budget="reading", data={"chunk": it["chunk"], "error": r["error"]})
                h.write(json.dumps({"chunk": it["chunk"], "model": JUDGE_B_MODEL, "at": now(), "ms": int((time.time() - started) * 1000), **r}) + "\n")
                say(f"  {it['chunk']}: {len(r['decisions']) if r['decisions'] is not None else 'ERROR ' + str(r['error'])} decisions (${r['cost_usd'] or 0:.4f})")
    say(f"reading cap: ${SETTINGS['cap'] - st.spent('reading'):.3f} left")


# ------------------------------------------------------------------------------------------- merge
ITEM_FILLER = {"no", "number", "item", "items", "ordinance", "resolution", "res", "ord", "motion", "agenda"}


def norm_item(s) -> str:
    """An item number's key: digits and letters without filler words, so "Ordinance No. 71-2024/25" and
    "Ordinance 71-2024/25" agree; a label with no digit ("Opioid Review Committee") is no item number."""
    toks = [t for t in re.findall(r"\d+|[a-z]+", str(s or "").lower()) if t not in ITEM_FILLER]
    key = "".join(toks)
    return key if any(c.isdigit() for c in key) else ""


def words(s) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", str(s or "").lower()) if len(w) > 2 and w not in
            {"the", "and", "for", "with", "approved", "approve", "contract", "agreement", "city", "county", "district"}}


def similarity(x: dict, y: dict) -> float:
    """How surely two records (two judges', or an arm's and the truth's) are one decision of the same meeting. Item
    numbers, amounts, vendors and words each add; no single field vetoes, since item numbers can be misread (a garbled
    layout, OCR) and amounts can be a part or a total."""
    s = 0.0
    ix, iy = norm_item(x.get("item")), norm_item(y.get("item"))
    if ix and iy:
        s += 3.0 if ix == iy else -1.0
    ax, ay = x.get("amount"), y.get("amount")
    if isinstance(ax, (int, float)) and isinstance(ay, (int, float)) and ax and ay and abs(ax - ay) <= 0.01 * max(ax, ay):
        s += 3.0
    vx, vy = words(x.get("vendor")), words(y.get("vendor"))
    if vx and vy and len(vx & vy) / min(len(vx), len(vy)) >= 0.5:
        s += 2.0
    wx, wy = words(x.get("what")) | words(x.get("quote")), words(y.get("what")) | words(y.get("quote"))
    if wx and wy:
        s += 3.0 * len(wx & wy) / len(wx | wy)
    return s


def same_decision(x: dict, y: dict) -> bool:
    return similarity(x, y) >= 2.0


def stems(s) -> set[str]:
    return {w[:5] for w in words(s)}


def arm_similarity(t: dict, e: dict) -> float:
    """An arm's event against a truth decision. Arms write longer, freer descriptions than the judges ("claims list
    (expenditure list for June 10, 2025), consent agenda" for "Expenditure list for June 10, 2025"), so words count by
    stem and by how much of the shorter description the longer one holds. Amount and vendor count as for the judges.
    (The truth's own merge keeps `similarity`.)"""
    s = 0.0
    ta, ea = t.get("amount"), e.get("amount")
    if isinstance(ta, (int, float)) and isinstance(ea, (int, float)) and ta and ea and abs(ta - ea) <= 0.01 * max(ta, ea):
        s += 3.0
    vt, ve = words(t.get("vendor")), words(e.get("vendor"))
    if vt and ve and len(vt & ve) / min(len(vt), len(ve)) >= 0.5:
        s += 2.0
    wt, we = stems(t.get("what")), stems(e.get("what"))
    if len(wt) >= 2 and len(we) >= 2:
        s += 3.0 * len(wt & we) / min(len(wt), len(we))
    return s


def match(a: list[dict], b: list[dict], sim=similarity) -> tuple[list, list, list]:
    """One-to-one, the surest pairs first: pairs, a only, b only."""
    scored = sorted(((sim(x, y), i, j) for i, x in enumerate(a) for j, y in enumerate(b)), reverse=True)
    pairs, used_a, used_b = [], set(), set()
    for s, i, j in scored:
        if s < 2.0:
            break
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        pairs.append((a[i], b[j]))
    return pairs, [x for i, x in enumerate(a) if i not in used_a], [y for j, y in enumerate(b) if j not in used_b]


def answers(b: str, judge: str) -> dict[str, list[dict]]:
    """Decisions by meeting, chunk answers joined; the same item seen in two chunks (a page break) counted once."""
    paths_ = sorted(folder(b).glob("judge_a.answers*.jsonl")) if judge == "a" else [folder(b) / "judge_b.jsonl"]
    lines = [l for p in paths_ if p.exists() for l in p.read_text().splitlines() if l.strip()]
    by_meeting: dict[str, list[dict]] = {}
    for line in lines:
        r = json.loads(line)
        if r.get("decisions") is None:
            continue
        date_ = r["chunk"].split(":")[1]
        for d in r["decisions"]:
            if isinstance(d, dict):
                lst = by_meeting.setdefault(date_, [])
                if not any(e["chunk"] != r["chunk"] and similarity(d, e) >= 4.0 for e in lst):  # a page break: one decision in two chunks
                    lst.append(d | {"chunk": r["chunk"]})
    return by_meeting


def people_answers(b: str, judge: str) -> dict[str, list[dict]]:
    paths_ = sorted(folder(b).glob("judge_a.answers*.jsonl")) if judge == "a" else [folder(b) / "judge_b.jsonl"]
    out: dict[str, list[dict]] = {}
    for p in paths_:
        for line in (p.read_text().splitlines() if p.exists() else []):
            r = json.loads(line) if line.strip() else {}
            for x in r.get("people") or []:
                if isinstance(x, dict) and x.get("name"):
                    lst = out.setdefault(r["chunk"].split(":")[1], [])
                    if not any(same_person(x, e) for e in lst):  # a page break: one entry in two chunks
                        lst.append(x | {"chunk": r["chunk"]})
    return out


def same_person(x: dict, y: dict) -> bool:
    """The same person and the same kind of change: surname and first initial as written, and the change."""
    key = lambda d: tuple(re.findall(r"[a-z]+", (d.get("name") or "").lower())[-1:] + [((d.get("name") or " ")[0]).lower(),  # noqa: E731
                                                                                          d.get("change") or ""])
    return key(x) == key(y)


def merge_people(b: str, sampled: list[dict]) -> tuple[list[dict], list[dict]]:
    A, B = people_answers(b, "a"), people_answers(b, "b")
    agreed, single = [], []
    for m in sampled:
        d, bs = m["date"], list(B.get(m["date"], []))
        for x in A.get(d, []):
            y = next((y for y in bs if same_person(x, y)), None)
            if y:
                bs.remove(y)
                agreed.append({"date": d, "status": "agreed", "a": x, "b": y})
            else:
                single.append({"date": d, "only": "a", "person": x})
        single += [{"date": d, "only": "b", "person": y} for y in bs]
    return agreed, single


def t_merge(bodies: list[str]) -> None:
    for b in bodies:
        A, B = answers(b, "a"), answers(b, "b")
        sample = json.loads((folder(b) / "sample.json").read_text())
        out, disputes = [], []
        for m in sample["sampled"]:
            d = m["date"]
            pairs, a_only, b_only = match(A.get(d, []), B.get(d, []))
            for x, y in pairs:
                out.append({"date": d, "status": "agreed", "a": x, "b": y,
                            "fields": {f: x.get(f) for f in ("type", "action") if x.get(f) == y.get(f)}})
            for who, xs in (("a", a_only), ("b", b_only)):
                for x in xs:
                    disputes.append({"date": d, "only": who, "decision": x, "id": f"{b}:{d}:{len(disputes) + 1}"})
        people_agreed, people_single = merge_people(b, sample["sampled"])
        (folder(b) / "merged.json").write_text(json.dumps({"body": b, "merged_at": now(), "agreed": out, "disputes": disputes,
                                                          "people_agreed": people_agreed, "people_single": people_single}, indent=1))
        say(f"{b}: judge A {sum(map(len, A.values()))} decisions, judge B {sum(map(len, B.values()))}; agreed {len(out)}, "
            f"A only {sum(1 for x in disputes if x['only'] == 'a')}, B only {sum(1 for x in disputes if x['only'] == 'b')}")


STAFFING = re.compile(r"\b(?:bonus(?:es)?|salar(?:y|ies)|pay (?:grades?|plans?|schedules?|scales?|increases?|raises?|rates?)|"
                      r"compensation (?:and classification|study|plan)|stipends?|longevity|wages?|classification study|"
                      r"new positions?|positions? (?:added|created)|hiring|employment contract|cost of living)\b", re.I)


def t_export_c(bodies: list[str]) -> None:
    """The adjudicator's work list: each decision only one judge found, and each decision both judges agreed on that
    looks like staffing (pay, bonuses, positions: not buying by the judges' own definition), with its chunk file."""
    for b in bodies:
        merged = json.loads((folder(b) / "merged.json").read_text())
        listed: dict[str, list] = {}  # what both judges already agree on, per meeting: a disputed decision may be one of these
        for x in merged["agreed"]:
            listed.setdefault(x["date"], []).append({k: x["a"].get(k) for k in ("item", "what", "amount")})
        with (folder(b) / "judge_c.jsonl").open("w") as h:
            for x in merged["disputes"]:
                h.write(json.dumps({"id": x["id"], "date": x["date"], "decision": {k: v for k, v in x["decision"].items() if k != "chunk"},
                                    "path": str(TEXT / b / x["date"] / f"chunk-{x['decision']['chunk'].split(':')[2]}.md"),
                                    "already_listed": listed.get(x["date"], [])}) + "\n")
            review = [(i, x) for i, x in enumerate(merged["agreed"]) if STAFFING.search(f"{x['a'].get('what')} {x['a'].get('quote')}")]
            for i, x in review:
                h.write(json.dumps({"id": f"{b}:{x['date']}:agreed-{i}", "date": x["date"], "review": "both judges listed it: is it a "
                                    "buying decision, or staffing (pay, bonuses, positions), which is not?",
                                    "decision": {k: v for k, v in x["a"].items() if k != "chunk"},
                                    "path": str(TEXT / b / x["date"] / f"chunk-{x['a']['chunk'].split(':')[2]}.md"),
                                    "already_listed": []}) + "\n")
        say(f"{b}: {len(merged['disputes'])} disputes and {len(review)} agreed decisions that look like staffing for the "
            f"adjudicator → {folder(b) / 'judge_c.jsonl'}")


def t_import_c(bodies: list[str]) -> None:
    for b in bodies:
        merged = json.loads((folder(b) / "merged.json").read_text())
        verdicts = {}
        path = folder(b) / "judge_c.answers.jsonl"
        for line in (path.read_text().splitlines() if path.exists() else []):
            r = json.loads(line)
            verdicts[r["id"]] = r
        truth = [{"date": x["date"], "source": "both judges", **{k: x["a"].get(k) for k in ("item", "page", "type", "action", "vendor", "amount", "what", "quote")},
                  "b": {k: x["b"].get(k) for k in ("type", "action", "vendor", "amount")}} for i, x in enumerate(merged["agreed"])
                 if (verdicts.get(f"{b}:{x['date']}:agreed-{i}") or {}).get("is_decision", True)]  # staffing on review leaves
        undecided = 0
        for x in merged["disputes"]:
            v = verdicts.get(x["id"])
            if v is None:
                undecided += 1
            elif v.get("is_decision"):
                truth.append({"date": x["date"], "source": f"judge {x['only'].upper()} + adjudicator",
                              **{k: x["decision"].get(k) for k in ("item", "page", "type", "action", "vendor", "amount", "what", "quote")}})
        people = [{"date": x["date"], "source": "both judges", **{k: x["a"].get(k) for k in ("item", "page", "name", "role", "change", "action", "quote")}}
                  for x in merged.get("people_agreed") or []]
        (folder(b) / "truth.json").write_text(json.dumps({"body": b, "built_at": now(), **approval(), "decisions": truth,
                                                         "disputes_undecided": undecided, "people": people,
                                                         "people_one_judge": len(merged.get("people_single") or [])}, indent=1))
        say(f"{b}: truth {len(truth)} decisions over {len({t['date'] for t in truth})} meetings; disputes undecided {undecided}")


# ------------------------------------------------------------------------------------------- who pays (2026-11-03)
# The query asks what the body bought. The first judges were not asked who pays, and the truth kept agreements where the
# body sells, leases out, licenses or provides (Cortez: water sales, a hangar land lease, waste service to Dolores).
# Every non-budget decision gets one more question, from the same two blind judges, with its page; disagreements go to
# the adjudicator. Decisions with buyer = false leave the scored truth (they stay in truth.json, marked).
BUYER = """A public body's meeting minutes record the decision below. Decide whether {name} (a {kind} in {state}) is the BUYER in it: paying for or acquiring goods, services, construction, property or funding (a grant it receives counts; so does a purchase, lease or agreement it pays for), rather than selling, leasing out, licensing, giving funding to someone else, or providing a service to another party.

DECISION (from the minutes of {date}): {what}
Type: {type}. Vendor or other party: {vendor}. Amount: {amount}.
The minutes' words: {quote}

THE PAGE IT IS ON:
{page}

Reply with JSON only: {{"buyer": true | false | "unclear", "reason": "<one sentence>"}}"""


def page_text(body: str, date_: str, page) -> str:
    """The text of one page of a sampled meeting's minutes, from its chunk files ([page N] markers)."""
    out = []
    for f in sorted((TEXT / body / date_).glob("chunk-*.md")):
        for block in re.split(r"(?=^\[(?:minutes \d+ )?page \d+\]$)", f.read_text(), flags=re.M):
            m = re.match(r"\[(?:minutes \d+ )?page (\d+)\]", block)
            if m and str(m.group(1)) == str(page):
                out.append(block)
    return "\n".join(out)[:12000]


def around(page: str, quote: str | None, width: int = 2500) -> str:
    """The part of the page around the decision's own words (the whole page when they cannot be found), so a judge
    sees who pays whom without reading the whole page."""
    if not page:
        return ""
    words_ = re.findall(r"[A-Za-z0-9]+", quote or "")[:8]
    m = re.search(r"\W+".join(map(re.escape, words_)), page) if len(words_) >= 3 else None
    if not m:
        return page[: 2 * width]
    lo = max(0, m.start() - width)
    return ("…" if lo else "") + page[lo: m.end() + width] + ("…" if m.end() + width < len(page) else "")


def buyer_items(b: str) -> list[dict]:
    t = json.loads((folder(b) / "truth.json").read_text())
    card = json.loads((folder(b) / "sample.json").read_text()).get("card") or frame(b)["card"]
    items = []
    for i, d in enumerate(t["decisions"]):
        if d.get("type") == "budget":
            continue
        items.append({"id": f"{b}:{i}", "index": i, "prompt": BUYER.format(
            name=(card.get("name") or b).title(), kind=card.get("kind", "public body").replace("_", " "), state=card.get("state", ""),
            date=d["date"], what=d.get("what"), type=d.get("type"), vendor=d.get("vendor") or "none named",
            amount=d.get("amount") if d.get("amount") is not None else "none stated", quote=d.get("quote") or "",
            page=around(page_text(b, d["date"], d.get("page")), d.get("quote")) or "(page text not found)")})
    return items


def t_export_buyer(bodies: list[str]) -> None:
    for b in bodies:
        import textwrap
        items = buyer_items(b)
        out = TEXT / "buyer" / b
        out.mkdir(parents=True, exist_ok=True)
        with (folder(b) / "buyer_a.jsonl").open("w") as h:
            for it in items:
                path = out / f"{it['index']:04d}.md"  # one file per question, lines wrapped so a reader never cuts one
                path.write_text("\n".join(textwrap.fill(line, 900) if len(line) > 900 else line for line in it["prompt"].splitlines()))
                h.write(json.dumps({"id": it["id"], "path": str(path)}) + "\n")
        say(f"{b}: {len(items)} decisions for judge A (who pays) → {folder(b) / 'buyer_a.jsonl'}")


def t_judge_buyer_b(bodies: list[str]) -> None:
    run = SETTINGS["run"]
    st = State(run)
    providers = Providers(run_id=f"{run}-buyer-b", max_usd=SETTINGS["cap"], max_calls=None,
                          approved_by=SETTINGS["approved_by"])
    for b in bodies:
        out = folder(b) / "buyer_b.jsonl"
        done = {r["id"] for r in map(json.loads, out.read_text().splitlines()) if r.get("buyer") is not None} if out.exists() else set()
        todo = [it for it in buyer_items(b) if it["id"] not in done]

        def one(it):
            try:
                res = providers.reason([{"role": "user", "content": it["prompt"]}], JUDGE_B_MODEL, max_tokens=8000,
                                       temperature=0)
                content = (((res.response or {}).get("choices") or [{}])[0].get("message") or {}).get("content")
                parsed = parse_json(content) or {}
                return it, {"buyer": parsed.get("buyer"), "reason": parsed.get("reason"), "cost_usd": res.cost,
                            "error": None if "buyer" in parsed else f"no JSON: {str(content)[:200]}"}
            except Exception as error:
                return it, {"buyer": None, "cost_usd": 0.0, "error": f"{type(error).__name__}: {str(error)[:300]}"}

        with ThreadPoolExecutor(max_workers=6) as pool, out.open("a") as h:
            for it, r in pool.map(one, todo):
                st.put(f"{run}:buyer-b:{b}", "J" + it["id"].split(":")[-1].zfill(6)[:7], "judge", "done", tool="buyer-b",
                       cost=r["cost_usd"] or 0.0, budget="reading", data={"id": it["id"], "error": r["error"]})
                h.write(json.dumps({"id": it["id"], "model": JUDGE_B_MODEL, "at": now(), **r}) + "\n")
        rows = [json.loads(l) for l in out.read_text().splitlines()]
        say(f"{b}: judge B on {len(rows)} decisions: buyer {sum(1 for r in rows if r.get('buyer') is True)}, not "
            f"{sum(1 for r in rows if r.get('buyer') is False)}, unclear {sum(1 for r in rows if r.get('buyer') == 'unclear')}, "
            f"errors {sum(1 for r in rows if r.get('buyer') is None)}")
    say(f"reading cap: ${SETTINGS['cap'] - st.spent('reading'):.3f} left")


def t_merge_buyer(bodies: list[str]) -> None:
    """Both judges agree -> the flag; otherwise to the adjudicator (buyer_c.jsonl)."""
    for b in bodies:
        A = {}
        for p in sorted(folder(b).glob("buyer_a.answers*.jsonl")):
            for line in p.read_text().splitlines():
                if line.strip():
                    r = json.loads(line)
                    A[r["id"]] = r
        B = {r["id"]: r for r in map(json.loads, (folder(b) / "buyer_b.jsonl").read_text().splitlines())}
        items = {it["id"]: it for it in buyer_items(b)}
        agreed, disputes = {}, []
        for i in items:
            a, bb = (A.get(i) or {}).get("buyer"), (B.get(i) or {}).get("buyer")
            if a is not None and a == bb and a != "unclear":
                agreed[i] = a
            else:
                disputes.append({"id": i, "a": a, "b": bb, "prompt": items[i]["prompt"]})
        (folder(b) / "buyer_merged.json").write_text(json.dumps({"agreed": agreed, "disputes": [{k: v for k, v in d.items() if k != "prompt"} for d in disputes]}, indent=1))
        with (folder(b) / "buyer_c.jsonl").open("w") as h:
            for d in disputes:  # blind: neither judge's answer goes with it; the question is the file judge A read
                h.write(json.dumps({"id": d["id"], "path": str(TEXT / "buyer" / b / f"{int(d['id'].split(':')[-1]):04d}.md")}) + "\n")
        say(f"{b}: {len(items)} decisions; judges agree on {len(agreed)} (buyer {sum(1 for v in agreed.values() if v is True)}, "
            f"not {sum(1 for v in agreed.values() if v is False)}); {len(disputes)} to the adjudicator")


LABOR = re.compile(r"collective bargaining|bargaining (?:unit|agreement)|labor (?:agreement|contract)|union (?:contract|agreement)|"
                   r"\b(?:IAFF|AFSCME|SEIU|FOP|Teamsters|IBEW|OEA|NEA)\b|(?:teachers?|education|classified|support staff|employees?|"
                   r"firefighters?|police officers?) association|negotiated agreement with (?:the )?(?:\w+ ){0,3}(?:association|union)", re.I)


def t_import_buyer(bodies: list[str]) -> None:
    for b in bodies:
        merged = json.loads((folder(b) / "buyer_merged.json").read_text())
        C = {}
        path = folder(b) / "buyer_c.answers.jsonl"
        for line in (path.read_text().splitlines() if path.exists() else []):
            if line.strip():
                r = json.loads(line)
                C[r["id"]] = r
        t = json.loads((folder(b) / "truth.json").read_text())
        for i, d in enumerate(t["decisions"]):
            key = f"{b}:{i}"
            if d.get("type") == "budget":
                d["buyer"], d["buyer_source"] = True, "budget decision (always counted)"
            elif key in merged["agreed"]:
                d["buyer"], d["buyer_source"] = merged["agreed"][key], "both judges"
            elif key in C:
                d["buyer"], d["buyer_source"] = C[key].get("buyer"), "adjudicator"
            else:
                d["buyer"], d["buyer_source"] = None, "undecided"
        for d in t["decisions"]:  # labor agreements are staffing (the rulings' convention): kept, marked, out of the score
            if d.get("type") != "budget" and LABOR.search(f"{d.get('what') or ''} {d.get('quote') or ''} {d.get('vendor') or ''}"):
                d["buyer"], d["buyer_source"], d["staffing"] = False, "staffing convention: a labor or union agreement", True
        t["buyer_flag"] = "2026-11-03: who pays, two blind judges and an adjudicator; buyer = false leaves the scored truth"
        (folder(b) / "truth.json").write_text(json.dumps(t, indent=1))
        n = len(t["decisions"])
        say(f"{b}: {n} decisions; buyer {sum(1 for d in t['decisions'] if d['buyer'] is True)}, not the buyer "
            f"{sum(1 for d in t['decisions'] if d['buyer'] is False)}, unclear {sum(1 for d in t['decisions'] if d['buyer'] not in (True, False))}")


# ------------------------------------------------------------------------------------------- scoring the arms
def session_minutes(evs: list[dict]) -> dict:
    """Minutes from a session's first event to its last (as scored before), and the minutes of work: the time between
    consecutive tool calls with every gap over PAUSE_MINUTES left out, so a session paused and resumed is not counted
    for the hours it sat waiting. A long gap still counts the run time the next tool recorded (a long read is work)."""
    calls = sorted((datetime.fromisoformat(e["at"]), (e.get("ms") or 0) / 60000) for e in evs if e.get("tool"))
    working, pauses = 0.0, 0
    for (a, _), (b, ran) in zip(calls, calls[1:]):
        gap = (b - a).total_seconds() / 60
        working += gap if gap <= PAUSE_MINUTES else min(gap, ran)
        pauses += gap - ran > PAUSE_MINUTES
    return {"minutes": round((datetime.fromisoformat(evs[-1]["at"]) - datetime.fromisoformat(evs[0]["at"])).total_seconds() / 60, 1) if evs else None,
            "minutes_working": round(working, 1) if calls else None, "pauses": pauses}


def score(run: str, body: str, usage: dict | None = None) -> dict:
    """Each arm's recorded events on the sampled meetings against the truth: recall (truth decisions found), precision
    (the arm's events there that are truth decisions), fields on the matched ones, and what it cost."""
    every = json.loads((folder(body) / "truth.json").read_text())["decisions"]
    flagged = any("buyer" in d for d in every)
    # the query is what the body bought: scored truth = decisions the judges call buying (all of them before the flag);
    # an arm event matching a not-buying or unclear decision is set aside, neither a find nor an extra
    truth = [d for d in every if d.get("buyer") is True] if flagged else every
    aside = [d for d in every if d.get("buyer") is not True] if flagged else []
    dates = {m["date"] for m in json.loads((folder(body) / "sample.json").read_text())["sampled"]}
    st = State(run)
    out = {"run": run, "body": body, "scored_at": now(), "truth_decisions": len(truth), "truth_set_aside": len(aside),
           "meetings": len(dates), "arms": {}}
    for s in st.db.execute("SELECT session, arm FROM sessions WHERE body = ? ORDER BY arm", [body]).fetchall():
        sid, arm = s[0], s[1]
        events = [e["data"] for e in st.nodes(sid, "event", "recorded")]
        on_sample = [e for e in events if e.get("date") in dates]
        pairs, missed, extra, set_aside = [], [], [], 0
        for d in sorted(dates):
            p, t_only, a_only = match([t for t in truth if t["date"] == d], [e for e in on_sample if e.get("date") == d], arm_similarity)
            q, _, a_only = match([t for t in aside if t["date"] == d], a_only, arm_similarity)
            set_aside += len(q)
            pairs += p
            missed += t_only
            extra += a_only
        scored_events = len(pairs) + len(extra)
        amounts = [(t, e) for t, e in pairs if t.get("amount")]
        evs = st.events(sid)
        answer = (st.get(sid, "A") or {}).get("data") or {}
        cov = answer.get("coverage") or {}
        out["arms"][arm] = {
            "recall": round(len(pairs) / len(truth), 3) if truth else None, "found": len(pairs), "missed": len(missed),
            # decisions taken (approved), apart from items only introduced or on an agenda: arms may record a resolution
            # once, where it is adopted, which the query ("bought or committed to buy") allows
            "recall_approved": round(sum(1 for t, _ in pairs if t.get("action") == "approved") / n_appr, 3) if (n_appr := sum(1 for t in truth if t.get("action") == "approved")) else None,
            "found_approved": f"{sum(1 for t, _ in pairs if t.get('action') == 'approved')}/{n_appr}",
            "precision": round(len(pairs) / scored_events, 3) if scored_events else None, "events_on_sample": len(on_sample),
            "events_set_aside": set_aside,
            "events_all_meetings": len(events),
            "type_right": sum(1 for t, e in pairs if t.get("type") == e.get("type")),
            "action_right": sum(1 for t, e in pairs if t.get("action") == e.get("action")),
            "amount_right": f"{sum(1 for t, e in amounts if e.get('amount') and abs(e['amount'] - t['amount']) <= 0.01 * t['amount'])}/{len(amounts)}",
            "answered": bool(answer), "complete": answer.get("complete"), "incomplete_because": answer.get("incomplete_because"),
            "links": cov.get("links"), "documents_read": cov.get("documents_read"), "links_by_status": cov.get("links_by_status"),
            "tool_calls": len({(e["tool"], e["at"][:19]) for e in evs if e["tool"]}),
            "paid_usd": round(sum(e["cost_usd"] or 0 for e in evs), 5),
            **session_minutes(evs),
            "agent": (usage or {}).get(arm),
            "missed_examples": [{k: t.get(k) for k in ("date", "type", "what", "amount")} for t in missed[:8]],
            "extra_examples": [{k: e.get(k) for k in ("date", "type", "what", "amount")} for e in extra[:8]],
        }
    return out


def t_score(bodies: list[str], run: str) -> None:
    runs = paths.DISCOVERY / "nav-runs" / run
    runs.mkdir(parents=True, exist_ok=True)
    usage = json.loads((runs / "usage.json").read_text()) if (runs / "usage.json").exists() else {}
    for b in bodies:
        if not (folder(b) / "truth.json").exists():
            continue
        r = score(run, b, usage.get(b))
        if not r["arms"]:
            continue
        (runs / f"score-{b}.json").write_text(json.dumps(r, indent=1))
        say(f"{b}: {r['truth_decisions']} truth decisions in {r['meetings']} meetings")
        for arm, x in r["arms"].items():
            say(f"  {arm:<10} recall {x['recall']} ({x['found']}/{r['truth_decisions']}), approved {x['found_approved']}, precision {x['precision']} "
                f"({x['events_on_sample']} events on the sample, {x['events_all_meetings']} in all); type {x['type_right']}, "
                f"action {x['action_right']}, amount {x['amount_right']}; {'complete' if x['complete'] else 'INCOMPLETE' if x['answered'] else 'NO ANSWER'}; "
                f"{x['documents_read']} documents read; Jev ${x['paid_usd']}; {x['minutes_working']} min working ({x['minutes']} from "
                f"first to last call, {x['pauses']} pauses over {PAUSE_MINUTES} min); agent {x['agent']}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["frame", "sample", "collect", "export-a", "judge-b", "merge", "export-c", "import-c", "score",
                                       "export-buyer", "judge-buyer-b", "merge-buyer", "import-buyer"])
    p.add_argument("--bodies", default=",".join(BODIES))
    p.add_argument("--run", help="the navigation run to score")
    p.add_argument("--cap", type=float, default=DEFAULT_CAPS["reading"], help="the reading cap for judge B, USD, this run")
    p.add_argument("--approved-by", default="", help="who approved the run (free text, recorded)")
    p.add_argument("--truth-run", default=RUN, help="the truth run (spend and judge records), e.g. 20261105-heldout-truth")
    a = p.parse_args()
    SETTINGS.update(cap=a.cap, approved_by=a.approved_by.strip(), run=a.truth_run)
    bodies = [b for b in a.bodies.split(",") if b]
    if a.command == "score":
        return t_score(bodies, a.run)
    {"frame": t_frame, "sample": t_sample, "collect": t_collect, "export-a": t_export_a, "judge-b": t_judge_b,
     "merge": t_merge, "export-c": t_export_c, "import-c": t_import_c, "export-buyer": t_export_buyer,
     "judge-buyer-b": t_judge_buyer_b, "merge-buyer": t_merge_buyer, "import-buyer": t_import_buyer}[a.command](bodies)


if __name__ == "__main__":
    main()
