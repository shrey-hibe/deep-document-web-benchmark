"""Phase 2: who should decide at each fork of the deep chain? The plumbing stays the same; only the decider changes:

    rules   the plain rules of Phase 1 (their answers are already logged)
    jev     Jev, typed yes/no and choice questions (provider layer: judge, pinned to Jev on the Vercel AI Gateway)
    small   a small model, the same questions answered in JSON (provider layer: reason, model named for the comparison)
    big     a bigger model (Opus) as a Claude Code subagent on the Max plan, from an exported file

Every decider sees exactly the same state for an item (its hash is logged with each answer). Forks:
    list_page     is this page the governing board's meetings list?       truth: the known lists (traces, PYLUSD)
    doc_link      what will this link open, and is it the governing board's?  truth: the downloaded document's own
                  first page, labelled by two blind judges (a decider sees only the link, as it would when navigating)
    buying_event  is this passage a buying event: type, action, vendor?   truth: two blind judges from different
                  model families; only items both agree on are scored

    uv run python -m ddwb.deep.forks build --run <phase-1 run>     items and the rules' answers
    uv run python -m ddwb.deep.forks jev|small --run <run> [--budget USD] [--approved-by "..."]   paid
    uv run python -m ddwb.deep.forks export --run <run>             files for the big-model and judge subagents
    uv run python -m ddwb.deep.forks import --run <run>             their answers
    uv run python -m ddwb.deep.forks judge-b --run <run>            the second judge (another model family)
    uv run python -m ddwb.deep.forks score --run <run>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import duckdb

from .. import paths
from ..evals.run import Runner, body_card
from ..runlog import now
from . import store, text, web
from .chain import BODIES

FORKS_DIR = paths.DISCOVERY / "forks"
SALT = "deep-forks-2026-11-02"
SETTINGS = {"approved_by": ""}  # who approved the run (free text, recorded with it when given): set by main()
SMALL_MODEL = "anthropic/claude-haiku-5.5"
JUDGE_B_MODEL = "deepseek/deepseek-v4.1-flash"

# ------------------------------------------------------------------------------------------- questions
LIST_PAGE = {
    "is_list": ("boolean", "Is this page a list of meetings of the body's governing board (its council, board of commissioners, "
                           "board of trustees or board of education), with links to their agendas, packets or minutes?"),
    "whose": ("choice", "Whose meetings does the page list?", {
        "governing_board": "the body's governing board", "committee_or_other_board": "only a committee, commission or another board",
        "mixed": "several boards, including the governing board", "not_a_meetings_list": "it is not a list of meetings"}),
}
DOC_LINK = {
    "kind": ("choice", "What will this link open?", {
        "agenda": "a meeting's agenda", "packet": "an agenda packet or meeting materials (the agenda with its attachments)",
        "minutes": "a meeting's minutes", "meeting_page": "a page for one meeting that lists its documents",
        "more_of_list": "more of the list of meetings (another year, month or page)", "video": "a recording or live stream",
        "other": "something else"}),
    "governing": ("boolean", "Is it for a meeting of the body's governing board, not of a committee, commission or another board?"),
}
# Calibrated 2026-11-05 against the truth sets (work/deep-discovery/jev-calibration): leaving staffing out and defining the
# types as the truth judges used them keeps 40% fewer passages for the agent and loses no more true decisions.
EVENT_TYPES = {
    "purchase": "buying goods or services: a purchase, a purchase contract or order, a lease of equipment, payments, "
                "invoices, warrants or claims lists",
    "contract": "a new contract or agreement for services, construction, design or cooperation (not a purchase contract)",
    "renewal": "a renewal, extension, amendment or change order of an existing contract or agreement",
    "bid": "a bid, RFP or RFQ: authorising, advertising, issuing, awarding or rejecting it",
    "grant": "a grant or outside funding: applying for it, accepting it, or awarding it",
    "budget": "a budget, appropriation, supplemental appropriation or transfer of funds",
    "none": "none of these (staffing, appointments, pay, policies, ceremonies, reports)",
}
ACTIONS = {"approved": "approved or adopted", "to_consider": "proposed or on the agenda, not yet decided",
           "rejected_or_tabled": "rejected, tabled or postponed", "reported_only": "a report or information only", "unclear": "cannot tell"}
BUYING_EVENT = {
    "is_event": ("boolean", "Does this passage record or propose a decision by the body to buy, contract, renew, bid, accept "
                            "or apply for a grant, or set a budget? Staffing decisions (hiring, appointments, pay, "
                            "resignations) are not."),
    "type": ("choice", "What kind of decision is it?", EVENT_TYPES),
    "action": ("choice", "What happened to it?", ACTIONS),
}
# People are their own label, asked in the same call as the buying questions (Shrey, 2026-11-05: staffing stays out of
# the buying question and is kept as a separate label).
PEOPLE_CHANGES = {
    "hire_or_appoint": "a hire, appointment or election to a position",
    "departure": "a resignation, retirement, dismissal, non-renewal or leave",
    "move": "a promotion, transfer or change of role",
    "pay_or_terms": "pay, a stipend or the contract terms of staff or officials",
    "none": "none of these",
}
PEOPLE = {
    "people": ("boolean", "Does this passage record or propose a staffing or people decision: hiring, appointing, electing, "
                          "promoting or transferring someone; a resignation, retirement, dismissal or leave; or the pay or "
                          "contract terms of staff or officials?"),
    "people_change": ("choice", "What kind of people decision is it?", PEOPLE_CHANGES),
}
QUESTIONS = {"list_page": LIST_PAGE, "doc_link": DOC_LINK, "buying_event": BUYING_EVENT}


def vendor_question(candidates: list[str]) -> dict:
    """The vendor as a choice among the organisation-like names found in the passage (text.vendor_candidates); {} when
    there are none. Answers come back as v1, v2, ... or none."""
    if not candidates:
        return {}
    options = {f"v{i}": v for i, v in enumerate(candidates, start=1)}
    return {"vendor": ("choice", "Which of these is the vendor, contractor or grantor in the decision?", options | {"none": "none of these"})}


def questions_for(item: dict) -> dict:
    qs = dict(QUESTIONS[item["fork"]])
    if item["fork"] == "buying_event":
        qs |= vendor_question(item["state"]["passage"].get("vendor_candidates") or [])
    return qs


def jev_questions(qs: dict) -> dict:
    out = {}
    for name, q in qs.items():
        out[name] = {"type": "boolean", "instructions": q[1]} if q[0] == "boolean" else \
            {"type": "choice", "instructions": q[1], "criteria": q[2]}
    return out


def prompt(item: dict) -> str:
    """The same questions in words, for a model answering in JSON."""
    lines = ["You make decisions for a pipeline that reads public bodies' meeting records. Read the state and answer every "
             "question. Reply with one JSON object only, no other text.", "", "STATE:", json.dumps(item["state"], indent=1), "",
             "QUESTIONS (answer with these keys):"]
    for name, q in questions_for(item).items():
        if q[0] == "boolean":
            lines.append(f'- "{name}": true or false. {q[1]}')
        else:
            lines.append(f'- "{name}": one of {json.dumps(list(q[2]))}. {q[1]} ' + "; ".join(f"{k} = {v}" for k, v in q[2].items()))
    return "\n".join(lines)


def sha(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()


def item_id(fork: str, key: str) -> str:
    return f"{fork}:{hashlib.sha1((SALT + key).encode()).hexdigest()[:10]}"


# ------------------------------------------------------------------------------------------- items
KNOWN_LISTS = {  # (body, host, path or query fragment) -> truth; pages not named here are not scored on list_page
    "summit-county-oh": [("council.summitoh.net", "/pages/Council-Agendas", True, "governing_board"),
                         ("council.summitoh.net", "/pages/Council-Minutes", True, "governing_board"),
                         ("council.summitoh.net", "/pages/Committee-", False, "committee_or_other_board")],
    "cortez-co": [("public.destinyhosted.com", "agenda_publish.cfm", True, "mixed")],
    "pueblo-library-co": [("pueblolibrary.org", "boardpackets", True, "governing_board"),
                          ("pueblolibrary.org", "/boardoftrustees", False, "not_a_meetings_list")],
    "pylusd-ca": [("pylusd.org", "pREC_ID=385068", True, "governing_board"), ("pylusd.org", "pREC_ID=385077", True, "governing_board"),
                  ("pylusd.org", "board_agendas.jsp", True, "governing_board"), ("pylusd.org", "board_minutes.jsp", True, "governing_board")],
}


def known_list(short: str, url: str):
    for host, frag, is_list, whose in KNOWN_LISTS.get(short, []):
        if web.host_of(url).endswith(host) and frag.lower() in url.lower():
            return {"is_list": is_list, "whose": whose}
    return None


def _rows(con, sql, params=()):
    cur = con.execute(sql, list(params))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def build(run: str) -> Path:
    """Fork items from a Phase 1 run, with the rules' answers and the known-list truth. Pages are re-read from the
    caches (scrapes repeat from the provider layer's cache at $0; our own GETs from data/cache/deep)."""
    import pdf_inspector
    con = duckdb.connect()
    t = lambda name: f"read_parquet('{paths.TABLES / 'deep' / name}/{run}*.parquet', union_by_name = true)"  # noqa: E731
    short_of = {v["body_id"]: k for k, v in BODIES.items()}
    cards = {b: body_card(duckdb.connect(), b) for b in short_of}
    items: list[dict] = []
    runner = Runner("deep-P2-build", "Phase 2 fork items (cache reads only)", 0.05, SETTINGS["approved_by"])

    # list_page: every page the chain judged as a possible list, where the truth is known
    pages = _rows(con, f"SELECT DISTINCT body_id, url, is_listing, dated_doc_links FROM {t('candidates')} WHERE checked")
    for p in pages:
        short = short_of[p["body_id"]]
        truth = known_list(short, p["url"])
        if truth is None:
            continue
        page, facts = web.scrape(runner.providers, p["url"])  # cached
        if not page:
            continue
        doc_rows = [{"text": l.text, "row": l.row[:200], "url": l.href} for l in page.links
                    if text.keep_for_documents(l.href, l.text, l.row)][:25]
        state = {"body": cards[p["body_id"]], "page": {"url": p["url"], "title": page.title, "links_total": len(page.links),
                                                       "document_like_links": doc_rows}}
        items.append({"fork": "list_page", "item_id": item_id("list_page", f"{short}|{p['url']}"), "body_id": p["body_id"],
                      "state": state, "rules": {"is_list": bool(p["is_listing"]), "whose": None}, "truth": truth})

    # doc_link: links to documents the chain downloaded; the decider sees the link, the judges see the document
    docs = _rows(con, f"""SELECT d.body_id, d.url, d.path, d.pages, d.kind AS rule_kind, d.governing AS rule_governing,
                                 any_value(l."text") AS link_text, any_value(l."row") AS link_row, any_value(l.heading) AS heading,
                                 any_value(l.page_url) AS page_url
                          FROM {t('documents')} d JOIN {t('links')} l ON l.href = d.url AND l.body_id = d.body_id
                          WHERE d.downloaded AND d.path IS NOT NULL GROUP BY ALL""")
    for d in docs:
        short = short_of[d["body_id"]]
        full = paths.stored(d["path"])
        try:
            head = (pdf_inspector.process_pdf(str(full)).markdown or "") if full.suffix == ".pdf" else \
                web.read_html(full.read_text(errors="replace"), d["url"]).text
        except Exception:
            continue
        state = {"body": cards[d["body_id"]], "page": {"url": d["page_url"]},
                 "link": {"text": d["link_text"], "url": d["url"], "row": (d["link_row"] or "")[:300], "heading": d["heading"]}}
        rule_kind = {"document": "other", "listing_page": "more_of_list"}.get(d["rule_kind"], d["rule_kind"])
        items.append({"fork": "doc_link", "item_id": item_id("doc_link", f"{short}|{d['url']}"), "body_id": d["body_id"],
                      "state": state, "rules": {"kind": rule_kind, "governing": bool(d["rule_governing"])},
                      "judge_view": {"body": cards[d["body_id"]]["name"], "pages": d["pages"], "first_page": head[:1500]}})

    # buying_event: a stratified sample of passages (each body, each rule type, plus ones the rules call none)
    passages = _rows(con, f"""SELECT body_id, doc_url, seq, meeting_date, doc_kind, "text", vendor_candidates, event_type, action, vendor
                              FROM {t('items')} WHERE first_in_meeting""")
    rng = random.Random(SALT)
    by = {}
    for p in passages:
        by.setdefault((p["body_id"], p["event_type"]), []).append(p)
    sample = []
    for (body, _), group in sorted(by.items()):
        rng.shuffle(group)
        sample += group[:12]
    for p in sample:
        cands = json.loads(p["vendor_candidates"]) if isinstance(p["vendor_candidates"], str) else (p["vendor_candidates"] or [])
        state = {"body": cards[p["body_id"]], "meeting": {"date": str(p["meeting_date"]), "document": p["doc_kind"]},
                 "passage": {"text": p["text"], "vendor_candidates": cands}}
        vendor_key = next((f"v{i}" for i, v in enumerate(cands, start=1) if v == p["vendor"]), "none")
        items.append({"fork": "buying_event", "item_id": item_id("buying_event", f"{p['doc_url']}|{p['seq']}"), "body_id": p["body_id"],
                      "state": state, "rules": {"is_event": p["event_type"] != "none", "type": p["event_type"], "action": p["action"],
                                                "vendor": vendor_key if cands else None}})
    runner.finish()

    out = FORKS_DIR / run
    out.mkdir(parents=True, exist_ok=True)
    for it in items:
        it["state_sha256"] = sha(it["state"])
    (out / "items.jsonl").write_text("\n".join(json.dumps(it, default=str) for it in items) + "\n")
    counts = {}
    for it in items:
        counts[it["fork"]] = counts.get(it["fork"], 0) + 1
    print(f"{len(items)} items {counts} -> {out / 'items.jsonl'}")
    return out


def load(run: str) -> list[dict]:
    return [json.loads(l) for l in (FORKS_DIR / run / "items.jsonl").read_text().splitlines() if l.strip()]


# ------------------------------------------------------------------------------------------- deciders
def parse_json(textual: str) -> dict | None:
    m = re.search(r"\{.*\}", textual or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def jev_answer(answers: dict, qs: dict) -> dict:
    """Jev's answers as the fork's fields. A question Jev did not answer is None (unknown), never False."""
    out = {}
    for name, q in qs.items():
        a = answers.get(name) or {}
        p = a.get("probability")
        out[name] = (None if p is None else p > 0.5) if q[0] == "boolean" else a.get("choice")
        if q[0] == "boolean":
            out[f"{name}_p"] = a.get("probability")
    return out


def decide(run: str, arm: str, budget: float) -> None:
    items = load(run)
    r = Runner(f"deep-P2-{arm}", f"Phase 2 forks: {arm} decides on {len(items)} items (identical states)", budget, SETTINGS["approved_by"],
               extra_specs=store.PHASE2)
    if arm == "rules":
        for it in items:
            r.log.add(store.DECISIONS, {"body_id": it["body_id"], "fork": it["fork"], "item_id": it["item_id"], "arm": "rules",
                                        "state_sha256": it["state_sha256"], "answer": it["rules"], "cost_usd": 0.0, "latency_ms": 0})
        r.finish()
        return

    def one(it):
        qs = questions_for(it)
        started, row = time.time(), {"body_id": it["body_id"], "fork": it["fork"], "item_id": it["item_id"], "arm": arm,
                                     "state_sha256": it["state_sha256"]}
        try:
            if arm == "jev":
                answers = r.jev(it["item_id"], "fork", it["fork"], it["state"], candidate_url=it["item_id"], questions=jev_questions(qs))
                row |= {"model": "typesafe-ai/jev-1.14.0", "answer": jev_answer(answers, qs) if answers else None}
                if not answers:  # the call failed (logged in eval.jev_calls): an error, never a "no"
                    row["error"] = "no Jev answer"
            else:
                if r.spent >= r.budget:
                    raise RuntimeError("budget reached")
                res = r.providers.reason([{"role": "user", "content": prompt(it)}], SMALL_MODEL, max_tokens=8000, temperature=0)
                resp = res.response or {}
                content = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content")
                usage = resp.get("usage") or {}
                with r.log.lock:
                    r.spent += res.cost or 0.0
                    r.calls += 1
                row |= {"model": SMALL_MODEL, "answer": parse_json(content), "cost_usd": res.cost,
                        "input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens")}
                if row["answer"] is None:
                    row["error"] = f"no JSON: {str(content)[:200]}"
        except Exception as error:
            row["error"] = f"{type(error).__name__}: {str(error)[:300]}"
        row["latency_ms"] = int((time.time() - started) * 1000)
        r.log.add(store.DECISIONS, row)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(one, items))
    r.finish()


# ------------------------------------------------------------------------------------------- subagents (big model, judge A)
def export(run: str, batch: int = 60) -> None:
    """Files for Claude Code subagents: the big-model decider (states and questions, as every decider sees them) and
    judge A (the document's first page or the passage, never a decider's answer)."""
    items, out = load(run), FORKS_DIR / run
    deciders, judges = [], []
    for it in items:
        deciders.append({"item_id": it["item_id"], "prompt": prompt(it)})
        if it["fork"] == "doc_link":
            judges.append({"item_id": it["item_id"], "task": "document", "body": it["judge_view"]["body"],
                           "pages": it["judge_view"]["pages"], "first_page": it["judge_view"]["first_page"]})
        elif it["fork"] == "buying_event":
            judges.append({"item_id": it["item_id"], "task": "passage", "body": it["state"]["body"].get("name"),
                           "passage": it["state"]["passage"]["text"]})
    for name, rows in (("big", deciders), ("judge_a", judges)):
        for i in range(0, len(rows), batch):
            path = out / f"{name}-{i // batch + 1:02d}.jsonl"
            path.write_text("\n".join(json.dumps(r) for r in rows[i:i + batch]) + "\n")
    print(f"big: {len(deciders)} items, judge_a: {len(judges)} items, in batches of {batch} under {out}")


JUDGE_DOC = ("Read the first page of a public body's document. Say what it is and whose meeting it belongs to. Reply with JSON only: "
             '{"kind": "agenda" | "packet" | "minutes" | "other", "governing": true | false, "board": "<the board or committee named>"}. '
             "packet = an agenda with its attached materials (usually many pages); governing = the body's own council, board of "
             "commissioners, board of trustees or board of education, not a committee, commission or another board.")
JUDGE_PASSAGE = ("Read a passage from a public body's meeting document. Reply with JSON only: "
                 '{"is_event": true | false, "type": ' + " | ".join(f'"{k}"' for k in EVENT_TYPES) + ', "action": '
                 + " | ".join(f'"{k}"' for k in ACTIONS) + ', "vendor": "<the vendor, contractor or grantor named, or empty>"}. '
                 "is_event = it records or proposes a purchase, contract, renewal, bid, grant or budget decision (staffing is not). "
                 + "; ".join(f"{k} = {v}" for k, v in EVENT_TYPES.items()) + ". " + "; ".join(f"{k} = {v}" for k, v in ACTIONS.items()) + ".")


def judge_b(run: str, budget: float) -> None:
    """The second judge, from another model family, on exactly what judge A sees."""
    r = Runner("deep-P2-judge-b", f"Phase 2 truth: second blind judge ({JUDGE_B_MODEL})", budget, SETTINGS["approved_by"],
               extra_specs=store.PHASE2)
    rows = []
    for path in sorted(p for p in (FORKS_DIR / run).glob("judge_a-*.jsonl") if re.fullmatch(r"judge_a-\d+\.jsonl", p.name)):
        rows += [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    body_of = {it["item_id"]: it["body_id"] for it in load(run)}

    def one(j):
        shown = {k: v for k, v in j.items() if k not in ("item_id",)}
        content = (JUDGE_DOC if j["task"] == "document" else JUDGE_PASSAGE) + "\n\n" + json.dumps(shown, indent=1)
        try:
            res = r.providers.reason([{"role": "user", "content": content}], JUDGE_B_MODEL, max_tokens=8000, temperature=0)
            with r.log.lock:
                r.spent += res.cost or 0.0
                r.calls += 1
            label = parse_json((((res.response or {}).get("choices") or [{}])[0].get("message") or {}).get("content"))
        except Exception as error:
            label = {"error": f"{type(error).__name__}: {str(error)[:200]}"}
        fork = "doc_link" if j["task"] == "document" else "buying_event"
        r.log.add(store.GOLD, {"body_id": body_of.get(j["item_id"]), "fork": fork, "item_id": j["item_id"],
                               "source": f"{'document_header' if fork == 'doc_link' else 'passage'}_judge_b", "label": label,
                               "evidence": JUDGE_B_MODEL, "shown": shown})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(one, rows))
    r.finish()


def import_answers(run: str) -> None:
    """Answers the subagents wrote next to their batches: big-NN.answers.jsonl and judge_a-NN.answers.jsonl."""
    items = {it["item_id"]: it for it in load(run)}
    r = Runner("deep-P2-import", "Phase 2: answers from the Claude Code subagents (Max plan)", 0.0, SETTINGS["approved_by"],
               extra_specs=store.PHASE2)
    usage = json.loads((FORKS_DIR / run / "usage.json").read_text()) if (FORKS_DIR / run / "usage.json").exists() else {}
    for path in sorted((FORKS_DIR / run).glob("*.answers.jsonl")):
        name = path.name.split(".")[0]  # big-01, judge_a-02
        rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        per_item_tokens = (usage.get(name) or 0) / max(len(rows), 1)
        for a in rows:
            it = items.get(a.get("item_id"))
            if not it:
                continue
            answer = {k: v for k, v in a.items() if k != "item_id"}
            if name.startswith("big"):
                r.log.add(store.DECISIONS, {"body_id": it["body_id"], "fork": it["fork"], "item_id": it["item_id"], "arm": "big",
                                            "model": "claude-opus (Claude Code subagent, Max plan)", "state_sha256": it["state_sha256"],
                                            "answer": answer, "cost_usd": 0.0, "input_tokens": int(per_item_tokens)})
            else:
                r.log.add(store.GOLD, {"body_id": it["body_id"], "fork": it["fork"], "item_id": it["item_id"],
                                       "source": f"{'document_header' if it['fork'] == 'doc_link' else 'passage'}_judge_a",
                                       "label": answer, "evidence": "claude-sonnet (Claude Code subagent, blind)",
                                       "shown": None})
    for it in items.values():  # the known lists are truth for list_page
        if it["fork"] == "list_page":
            r.log.add(store.GOLD, {"body_id": it["body_id"], "fork": "list_page", "item_id": it["item_id"], "source": "known_list",
                                   "label": it["truth"], "evidence": "work/deep-discovery traces; PYLUSD index pages", "shown": None})
    r.finish()


# ------------------------------------------------------------------------------------------- scoring
def _norm(v) -> str:
    return re.sub(r"[^a-z0-9 ]", "", str(v or "").lower()).strip()


def _same_vendor(a, b) -> bool:
    a, b = _norm(a), _norm(b)
    return bool(a and b and (a in b or b in a))


def score(run: str) -> dict:
    """Per fork and decider: accuracy against the truth, cost and time. Truth: the known lists; for documents and
    passages, the fields both blind judges agree on (disagreements are counted, not scored)."""
    items = {it["item_id"]: it for it in load(run)}
    con = duckdb.connect()
    dec = _rows(con, f"""SELECT arm, item_id, answer, cost_usd, latency_ms, input_tokens, error, written_at::VARCHAR AS written_at
                         FROM read_parquet('{paths.TABLES / 'deep' / 'decisions'}/*.parquet', union_by_name = true)""")
    gold = _rows(con, f"""SELECT item_id, source, label, written_at::VARCHAR AS written_at
                          FROM read_parquet('{paths.TABLES / 'deep' / 'gold'}/*.parquet', union_by_name = true)""")
    latest = {}
    for d in sorted(dec, key=lambda d: str(d["written_at"])):
        if d["item_id"] in items:
            latest[(d["arm"], d["item_id"])] = d
    labels = {}
    for g in sorted(gold, key=lambda g: str(g["written_at"])):
        if g["item_id"] in items:
            labels.setdefault(g["item_id"], {})[g["source"].rsplit("_", 1)[-1] if "judge" in g["source"] else "known"] = \
                json.loads(g["label"]) if isinstance(g["label"], str) else g["label"]

    def truth(it) -> dict:
        lab = labels.get(it["item_id"], {})
        if it["fork"] == "list_page":
            return lab.get("known") or it.get("truth") or {}
        a, b = lab.get("a") or {}, lab.get("b") or {}
        out, split = {}, []
        if it["fork"] == "doc_link":
            fields = ("kind", "governing")
        else:
            fields = ("is_event", "type", "action")
        for f in fields:
            if f in a and f in b and a[f] == b[f]:
                out[f] = a[f]
            elif f in a or f in b:
                split.append(f)
        if it["fork"] == "buying_event" and _same_vendor(a.get("vendor"), b.get("vendor")):
            out["vendor"] = a.get("vendor")
        out["_split"] = split
        return out

    def correct(it, ans, field, t) -> bool:
        got = (ans or {}).get(field)
        if it["fork"] == "doc_link" and field == "kind":
            return got == t or ({got, t} <= {"agenda", "packet"} and FIELD_LENIENT)
        if field == "vendor":
            cands = it["state"]["passage"].get("vendor_candidates") or []
            name = dict((f"v{i}", v) for i, v in enumerate(cands, start=1)).get(got)
            return _same_vendor(name, t)
        return got == t

    report = {}
    for fork in ("list_page", "doc_link", "buying_event"):
        fields = {"list_page": ("is_list", "whose"), "doc_link": ("kind", "governing"),
                  "buying_event": ("is_event", "type", "action", "vendor")}[fork]
        fork_items = [it for it in items.values() if it["fork"] == fork]
        report[fork] = {"items": len(fork_items), "judges_split": {f: sum(1 for it in fork_items if f in truth(it).get("_split", []))
                                                                   for f in fields}}
        for arm in ("rules", "jev", "small", "big"):
            row = {}
            for f in fields:
                scored = [(it, latest.get((arm, it["item_id"]))) for it in fork_items
                          if f in truth(it) and truth(it)[f] is not None and (arm, it["item_id"]) in latest]
                if f == "type":  # type and action only where both judges call it an event
                    scored = [(it, d) for it, d in scored if truth(it).get("is_event") is True]
                if not scored:
                    continue
                ok = sum(1 for it, d in scored if correct(it, json.loads(d["answer"]) if isinstance(d["answer"], str) else d["answer"],
                                                         f, truth(it)[f]))
                row[f] = {"n": len(scored), "correct": ok, "accuracy": round(ok / len(scored), 3)}
            ds = [latest[(arm, it["item_id"])] for it in fork_items if (arm, it["item_id"]) in latest]
            if ds:
                lat = sorted(d["latency_ms"] or 0 for d in ds)
                row["_cost_usd"] = round(sum(d["cost_usd"] or 0 for d in ds), 5)
                row["_median_ms"] = lat[len(lat) // 2]
                row["_errors"] = sum(1 for d in ds if d["error"])
                report[fork][arm] = row
    (FORKS_DIR / run / "scores.json").write_text(json.dumps(report, indent=1))
    print(json.dumps(report, indent=1))
    return report


FIELD_LENIENT = True  # an agenda and its packet count as the same document kind; the strict count is in the notes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["build", "rules", "jev", "small", "export", "judge-b", "import", "score"])
    parser.add_argument("--run", required=True, help="the Phase 1 run the items come from")
    parser.add_argument("--budget", type=float, default=0.5, help="this run's cap, USD")
    parser.add_argument("--approved-by", default="", help="who approved the run (free text, recorded with it)")
    args = parser.parse_args()
    SETTINGS["approved_by"] = args.approved_by.strip()
    {"build": lambda: build(args.run), "rules": lambda: decide(args.run, "rules", 0.0), "jev": lambda: decide(args.run, "jev", args.budget),
     "small": lambda: decide(args.run, "small", args.budget), "export": lambda: export(args.run),
     "judge-b": lambda: judge_b(args.run, args.budget), "import": lambda: import_answers(args.run),
     "score": lambda: score(args.run)}[args.command]()


if __name__ == "__main__":
    main()
