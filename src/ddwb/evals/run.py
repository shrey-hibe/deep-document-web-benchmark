"""Run the wide-tier Jev tests on the frozen samples, logging everything to the eval dataset.

    uv run python -m ddwb.evals.run w1|r|triage|s|n|n2 [--budget USD] [--approved-by "..."]

Every paid call goes through the provider layer (ddwb.providers), pinned to the provider each test measures: Jev on the
Vercel AI Gateway, Exa and Parallel search on their APIs (keys from the environment). Each run has a dollar budget;
who approved it, if given, is recorded in eval.runs. Question sets are versioned; the probe of 2026-10-31 fixed their
shapes (score levels are sent as a list, low to high; Jev returns them numbered from 0).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from datetime import UTC, datetime

import duckdb

from .. import paths, tables
from ..providers import EXA, JEV, PARALLEL, PriceDrift, Providers
from . import log, samples

CARD_VERSION = "card-v1"


def q_bool(text):
    return {"type": "boolean", "instructions": text}


def q_choice(text, options):
    return {"type": "choice", "instructions": text, "criteria": options}


def q_score(text, levels):
    return {"type": "score", "instructions": text, "criteria": levels}


QUESTIONS = {
    "website-v1": {
        "official_site": q_bool("Is this page the official website of the body described in 'body' (the same organisation, not a parent, "
                                "vendor, regulator or namesake)?"),
        "whose_site": q_choice("Whose website is this page?", {
            "this_body": "the body described in 'body' itself",
            "government_running_it": "the city, county or other government that runs the body",
            "other_public_body": "a different public body",
            "state_or_regulator_listing": "a state agency or regulator's page listing the body",
            "company_directory_or_law_firm": "a company, vendor, property manager, directory or law firm",
            "unclear": "cannot tell from the page"}),
        "page_kind": q_choice("What kind of page is this?", {
            "homepage": "the homepage of an organisation's website", "subpage": "a department or inner page of a website",
            "directory_listing": "a listing about the body on a directory, aggregator or registry site",
            "empty_or_error": "an empty, error or placeholder page", "other": "anything else"}),
        "names_same_place": q_bool("Does the page name the same city, county or state as the body?"),
        "links_to_meetings": q_bool("Does the page link to or mention board or council meetings, agendas or minutes?"),
        "maintained": q_score("How actively maintained does the site look?",
                              ["abandoned or placeholder", "sparse and old", "current but basic", "current and well kept"]),
    },
    "relation-v1": {
        "same_organisation": q_bool("Are record_A and record_B the same organisation?"),
        "b_runs_a": q_bool("Is record_A run by record_B (for example a city's water department run by the city)?"),
        "relation": q_choice("How are record_A and record_B related?", {
            "same_organisation": "they are the same organisation", "b_runs_a": "B runs A", "a_runs_b": "A runs B",
            "related_but_separate": "related but separate organisations", "unrelated": "not related"}),
        "same_place": q_bool("Are record_A and record_B in the same place (city, county or state)?"),
    },
    "triage-v1": {
        "run_by_city_or_county": q_bool("Is this body run by a city, town, village or county government?"),
        "volunteer_or_nonprofit": q_bool("Is this body a volunteer group or nonprofit rather than a government?"),
        "private_company": q_bool("Is this body a private company or private property owner rather than a public body?"),
        "where_published": q_choice("Where would this body's public notices most likely be published?", {
            "own_website": "its own website", "parent_government_website": "the website of the government that runs it",
            "state_listing": "only on a state agency's listing", "none": "nowhere online (too small or private)",
            "unclear": "cannot tell"}),
        "own_site_likelihood": q_score("How likely is it that this body has its own website?",
                                       ["very unlikely", "unlikely", "likely", "very likely"]),
    },
}


# ------------------------------------------------------------------------------------------- cards
def body_card(con, body_id: str) -> dict:
    row = con.execute(f"""SELECT b.name, b.entity_type, b.state, r.city, r.county_name, r.function, r.owner_type, r.legal_basis,
                                 r.organization_type, r.department_type, r.population, r.unit_type
                          FROM {tables.latest('entities', 'bodies')} b JOIN {tables.latest('standard', 'records')} r
                            ON r.record_id = b.anchor_record_id WHERE b.body_id = ?""", [body_id]).fetchone()
    keys = ("name", "kind", "state", "city", "county", "function", "owner_type", "legal_basis", "organization_type",
            "department_type", "population_served", "unit_type")
    return {k: v for k, v in zip(keys, row) if v is not None}


def record_card(con, record_id: str) -> dict:
    row = con.execute(f"""SELECT name, entity_type, state, city, county_name, function, owner_type, legal_basis, organization_type, website
                          FROM {tables.latest('standard', 'records')} WHERE record_id = ?""", [record_id]).fetchone()
    keys = ("name", "kind", "state", "city", "county", "function", "owner_type", "legal_basis", "organization_type", "website")
    return {k: v for k, v in zip(keys, row) if v is not None}


def page_card(con, host: str) -> dict | None:
    row = con.execute(f"""SELECT final_url, status, title, description, site_name, text FROM {tables.latest('web', 'fetches')}
                          WHERE host = ? ORDER BY fetched_at DESC LIMIT 1""", [host]).fetchone()
    if not row:
        return None
    links = [a for (a,) in con.execute(f"""SELECT anchor_text FROM {tables.latest('web', 'links')} WHERE host = ? AND anchor_text <> ''
                                            ORDER BY position LIMIT 40""", [host]).fetchall()]
    keys = ("url", "status", "title", "description", "site_name")
    card = {k: v for k, v in zip(keys, row[:5]) if v is not None}
    card["text_excerpt"] = (row[5] or "")[:2000]
    card["link_texts"] = links
    return card


# ------------------------------------------------------------------------------------------- calls
class Runner:
    def __init__(self, test: str, purpose: str, budget_usd: float, approved_by: str = "", extra_specs: tuple = (),
                 providers: Providers | None = None) -> None:
        """budget_usd: this run's cap (also the provider layer's dollar limit for the run). approved_by: free text,
        recorded with the run when given."""
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%MZ")
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=paths.REPO, capture_output=True, text=True).stdout.strip()
        self.run_id, self.test, self.budget = f"{stamp}-{test}", test, budget_usd
        self.providers = providers or Providers(run_id=self.run_id, max_usd=budget_usd, max_calls=None, approved_by=approved_by)
        self.log = log.Log(self.run_id, specs=log.ALL + extra_specs)
        self.con = duckdb.connect()
        self.spent, self.calls, self.items, self.seen, self.stopped = 0.0, 0, 0, set(), None
        self.run_row = {"test": test, "purpose": purpose, "code_commit": commit, "router_run_id": self.run_id,
                        "approved_by": approved_by.strip() or None, "budget_usd": budget_usd, "started_at": log.now(),
                        "status": "running"}

    def jev(self, item_id: str, variant: str, qset: str, state: dict, candidate_url: str | None = None,
            questions: dict | None = None) -> dict:
        """questions: this call's own questions (a choice whose options come from the item), else the set's."""
        if self.spent >= self.budget:
            raise RuntimeError(f"budget ${self.budget} reached")
        if self.stopped:
            raise RuntimeError(f"run stopped: {self.stopped}")
        questions = questions or QUESTIONS[qset]
        call_id = f"{self.run_id}:{item_id}:{variant}:{hashlib.sha1((candidate_url or '').encode()).hexdigest()[:8]}"
        started, row = time.time(), {"call_id": call_id, "item_id": item_id, "variant": variant, "candidate_url": candidate_url,
                                     "question_set": qset, "state": state, "questions": questions, "called_at": log.now(),
                                     "state_sha256": hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()}
        try:
            try:
                res = self.providers.judge(state, questions, provider=JEV)
            except PriceDrift as drift:  # the price guard: the call was paid, so keep its answer and stop the run
                if drift.result is None:
                    raise
                res, self.stopped = drift.result, str(drift)[:300]
                row["error"] = f"PriceDrift (answer kept, run stopped): {str(drift)[:300]}"
            resp = res.response or {}
            meta = (resp.get("providerMetadata") or {}).get("gateway", {})
            attempt = (((meta.get("routing") or {}).get("modelAttempts") or [{}])[0].get("providerAttempts") or [{}])[0]
            row |= {"model": resp.get("model"), "route": res.route, "cost_usd": res.cost, "est_cost_usd": res.est_cost,
                    "cost_source": res.cost_source, "cache_hit": res.cached, "status": res.status,
                    "input_tokens": (resp.get("usage") or {}).get("inputTokens"), "output_tokens": (resp.get("usage") or {}).get("outputTokens"),
                    "generation_id": meta.get("generationId"),
                    "provider_ms": (attempt.get("endTime") - attempt.get("startTime")) if attempt.get("endTime") else None}
            answers = resp.get("answers") or {}
        except Exception as error:  # logged, not hidden
            row |= {"status": type(error).__name__, "error": str(error)[:500]}
            answers = {}
        row["latency_ms"] = int((time.time() - started) * 1000)
        with self.log.lock:  # several threads call jev() at once
            self.spent += row.get("cost_usd") or 0.0
            self.calls += 1
        self.log.add(log.JEV_CALLS, row)
        for name, ans in answers.items():
            self.log.add(log.JEV_ANSWERS, {"call_id": call_id, "item_id": item_id, "question": name, "type": ans.get("type"),
                                           "probability": ans.get("probability"), "choice": ans.get("choice"),
                                           "score": ans.get("score"), "confidence": ans.get("confidence"),
                                           "probabilities": ans.get("probabilities"), "raw": ans})
        return answers

    def item(self, row: dict) -> bool:
        """Log an item once per run; False if the sample repeats it (the same pair proposed twice)."""
        if row["item_id"] in self.seen:
            return False
        self.seen.add(row["item_id"])
        self.items += 1
        if row.get("body_id") and not row.get("record_id"):
            row["record_id"] = self.con.execute(f"SELECT anchor_record_id FROM {tables.latest('entities', 'bodies')} WHERE body_id = ?",
                                                [row["body_id"]]).fetchone()[0]
        self.log.add(log.ITEMS, {"card_version": CARD_VERSION, **row})
        return True

    def decide(self, row: dict) -> None:
        self.log.add(log.DECISIONS, {"test": self.test, **row})

    def finish(self, status: str = "finished") -> None:
        self.log.add(log.RUNS, self.run_row | {"finished_at": log.now(), "status": status, "items": self.items,
                                               "calls": self.calls, "spend_usd": round(self.spent, 6)})
        self.log.close()
        print(f"{self.run_id}: {self.items} items, {self.calls} calls, ${self.spent:.5f}", flush=True)


def _sample(name: str) -> list[dict]:
    con = duckdb.connect()
    cur = con.execute(f"SELECT * FROM read_csv('{samples.SAMPLES / f'{name}.csv'}', all_varchar = true) ORDER BY stratum, CAST(draw_rank AS INT)")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ------------------------------------------------------------------------------------------- tests
def run_w1(budget: float = 0.10, approved_by: str = "") -> None:
    r = Runner("W1", "Is the chosen website the body's own? Page facts only (title, description, text excerpt, link texts)", budget,
               approved_by)
    for s in _sample("W"):
        item_id = f"W:{s['body_id']}"
        state = {"body": body_card(r.con, s["body_id"]), "page": page_card(r.con, s["host"])}
        if not r.item({"item_id": item_id, "sample": "W", "stratum": s["stratum"], "body_id": s["body_id"], "card": state,
                "draw_rank": int(s["draw_rank"])}):
            continue
        a = r.jev(item_id, "W1", "website-v1", state, candidate_url=s["website"])
        p = (a.get("official_site") or {}).get("probability")
        r.decide({"item_id": item_id, "decision": None if p is None else ("found" if p >= 0.5 else "not_own"),
                  "website": s["website"], "probability": p, "n_calls": 1, "path": {"whose_site": (a.get("whose_site") or {}).get("choice")}})
    r.finish()


def run_r(budget: float = 0.05, approved_by: str = "") -> None:
    r = Runner("R", "Relation candidates: same organisation, or does B run A? Both records' facts", budget, approved_by)
    for s in _sample("R"):
        item_id = f"R:{s['stratum']}:{s['from_record_id']}>{s['to_record_id']}"  # a pair can come from two rules
        # Jev judges from the two records only, not from the name of the rule that proposed the link.
        state = {"record_A": record_card(r.con, s["from_record_id"]), "record_B": record_card(r.con, s["to_record_id"])}
        if not r.item({"item_id": item_id, "sample": "R", "stratum": s["stratum"], "from_record_id": s["from_record_id"],
                "to_record_id": s["to_record_id"], "card": state, "draw_rank": int(s["draw_rank"])}):
            continue
        a = r.jev(item_id, "R", "relation-v1", state)
        rel = a.get("relation") or {}
        r.decide({"item_id": item_id, "decision": rel.get("choice"), "probability": (rel.get("probabilities") or {}).get(rel.get("choice")),
                  "n_calls": 1, "path": {"same_organisation": (a.get("same_organisation") or {}).get("probability"),
                                         "b_runs_a": (a.get("b_runs_a") or {}).get("probability")}})
    r.finish()


def run_triage(budget: float = 0.05, approved_by: str = "") -> None:
    r = Runner("N_triage", "Before any search: where would this body's notices be published? Registry facts only", budget,
               approved_by)
    for s in _sample("N"):
        item_id = f"N:{s['body_id']}"
        state = {"body": body_card(r.con, s["body_id"])}
        if not r.item({"item_id": item_id, "sample": "N", "stratum": s["stratum"], "body_id": s["body_id"], "card": state,
                "draw_rank": int(s["draw_rank"])}):
            continue
        a = r.jev(item_id, "triage", "triage-v1", state)
        wp = a.get("where_published") or {}
        r.decide({"item_id": item_id, "decision": None, "triage": wp.get("choice"),
                  "probability": (wp.get("probabilities") or {}).get(wp.get("choice")), "n_calls": 1,
                  "path": {k: (v.get("probability") if v.get("type") == "boolean" else v.get("score") if v.get("type") == "score" else v.get("choice"))
                           for k, v in a.items()}})
    r.finish()




# ------------------------------------------------------------------------------------------- search tests
KIND_WORDS = {"water_system": "water", "fire_department": "fire department", "library_system": "library",
              "special_district": "district", "town_or_township": "town", "municipality": "city", "county": "county",
              "school_district": "school district", "dependent_school_system": "schools", "transit_reporter": "transit"}
TOP_N = 5


def _query(card: dict) -> str:
    words = [card.get("name", ""), card.get("city", ""), card.get("state", ""), KIND_WORDS.get(card.get("kind", ""), ""), "official website"]
    return " ".join(w for w in words if w)


def _fetch_pages(urls: list[str]) -> dict[str, dict]:
    import asyncio
    import httpx
    from ..fetch import live_check

    async def go():
        async with httpx.AsyncClient(headers={"User-Agent": live_check.USER_AGENT}, follow_redirects=True, max_redirects=5,
                                     timeout=httpx.Timeout(20.0, connect=8.0)) as client:
            results = await asyncio.gather(*(live_check._check(client, live_check.re.sub(r"^https?://", "", u).split("/")[0], u) for u in urls))
            return {u: r for u, r in zip(urls, results)}
    return asyncio.run(go())


# Words too common in public bodies' names to show that a page is about this body (the name check below)
NAME_GENERIC = set("""township town of the city village v volunteer fire department dept company co district water library public
    memorial housing authority municipal sanitary rural rwd mud county no number and rescue protection service services utility
    utilities system school schools consolidated unified independent isd csd sd community township's twp board""".split())
PICK_RULE = "pick-v3"  # v2 added the name check; it failed on the fresh N2 sample (FINDINGS), so v3 drops it


def names_body(name: str, text: str) -> bool:
    """True if the page text contains every distinctive word of the body's name (a name of generic words only passes)."""
    import re
    core = [w for w in re.findall(r"[a-z0-9']+", (name or "").lower()) if w not in NAME_GENERIC]
    text = (text or "").lower()
    return all(re.search(r"\b" + re.escape(w) + r"\b", text) for w in core)


def readable(p: dict) -> bool:
    return bool(p.get("status")) and 200 <= p["status"] < 400 and not p.get("bot_wall") and len(p.get("text") or "") > 200


def pick(card: dict, urls: list[str], pages: dict, verdicts: dict, name_check: bool = False) -> tuple[str | None, float, str | None]:
    """The decision rule (FINDINGS, 2026-10-31): only a page that loaded; Jev's whose_site is this body or the government
    running it; best official_site first. name_check (pick-v2) also required a governing body's page to name the body: it
    cut wrong picks on S and N but on fresh bodies removed more right picks than wrong ones, because registry names
    ("KEYTESVILLE PWS") are not how bodies name themselves. Off by default. Returns (url, official_site, whose_site)."""
    best = (None, 0.0, None)
    for u in urls:
        a, p = verdicts.get(u) or {}, pages.get(u) or {}
        whose = (a.get("whose_site") or {}).get("choice")
        official = (a.get("official_site") or {}).get("probability") or 0.0
        if not readable(p) or whose not in ("this_body", "government_running_it"):
            continue
        if name_check and whose == "government_running_it" and not names_body(card.get("name"), f"{p.get('title') or ''} {p.get('text') or ''}"):
            continue
        if best[0] is None or official > best[1]:
            best = (u, official, whose)
    return best


def run_search(sample: str, budget: float = 1.50, approved_by: str = "") -> None:
    from concurrent.futures import ThreadPoolExecutor
    r = Runner(f"{sample}_search", f"Set {sample}: one query per body, Exa and Parallel side by side, free fetch of the top {TOP_N} "
               f"results each, Jev picks the body's own site ({PICK_RULE})", budget, approved_by)
    for s in _sample(sample):
        item_id = f"{sample}:{s['body_id']}"
        card = body_card(r.con, s["body_id"])
        query = _query(card)
        if not r.item({"item_id": item_id, "sample": sample, "stratum": s["stratum"], "body_id": s["body_id"], "card": {"body": card, "query": query},
                "draw_rank": int(s["draw_rank"])}):
            continue
        if r.spent >= r.budget:
            break
        arms = {}
        objective = (f"The official website of {card.get('name')}, a {card.get('kind', 'public body').replace('_', ' ')} "
                     f"in {card.get('city', '')} {card.get('state', '')}")
        for provider, kind, payload, call in (  # the payload is logged as the request each provider was sent
                ("exa", "search_open", {"query": query, "numResults": TOP_N},
                 lambda: r.providers.search(query, n=TOP_N, provider=EXA)),
                ("parallel", "search_site", {"objective": objective, "search_queries": [query]},
                 lambda: r.providers.search(query, objective=objective, provider=PARALLEL))):
            call_id, started = f"{r.run_id}:{item_id}:{provider}", time.time()
            row = {"call_id": call_id, "item_id": item_id, "provider": provider, "kind": kind, "query": query, "payload": payload,
                   "called_at": log.now()}
            try:
                res = call()
                results = res.response
                row |= {"route": res.route, "cost_usd": res.cost, "cost_source": res.cost_source, "cache_hit": res.cached,
                        "status": res.status, "n_results": len(results)}
            except Exception as error:
                results, row = [], row | {"status": type(error).__name__, "error": str(error)[:500], "n_results": 0}
            row["latency_ms"] = int((time.time() - started) * 1000)
            r.spent += row.get("cost_usd") or 0.0
            r.calls += 1
            r.log.add(log.SEARCH_CALLS, row)
            for rank, x in enumerate(results, start=1):
                url = x.get("url")
                r.log.add(log.SEARCH_RESULTS, {"call_id": call_id, "item_id": item_id, "provider": provider, "rank": rank, "url": url,
                                               "host": live_check_host(url), "title": x.get("title"),
                                               "published_date": x.get("published_date"),
                                               "excerpt": (x.get("excerpt") or "")[:2000] or None, "raw": x.get("raw")})
            arms[provider] = [x.get("url") for x in results[:TOP_N] if x.get("url")]
        urls = sorted({u for us in arms.values() for u in us})
        started = time.time()
        pages = _fetch_pages(urls) if urls else {}
        fetch_ms = int((time.time() - started) * 1000)
        for u, p in pages.items():
            r.log.add(log.PAGE_FETCHES, {"url": u, "host": p["host"], "purpose": f"eval {sample} search result", "eval_run_id": r.run_id,
                                         **{k: p.get(k) for k in ("status", "final_url", "content_type", "bytes", "sha256", "error",
                                                                  "elapsed_ms", "title", "description", "site_name", "text", "bot_wall",
                                                                  "parked", "fetched_at")}, "link_count": len(p.get("links") or [])})

        def judge(u):
            p = pages[u]
            page = {k: v for k, v in {"url": p.get("final_url") or u, "status": p.get("status"), "title": p.get("title"),
                                      "description": p.get("description"), "site_name": p.get("site_name"),
                                      "text_excerpt": (p.get("text") or "")[:2000],
                                      "link_texts": [t for _, t in (p.get("links") or []) if t][:40]}.items() if v is not None}
            return u, r.jev(item_id, "pick", "website-v1", {"body": card, "page": page}, candidate_url=u)
        with ThreadPoolExecutor(max_workers=8) as pool:
            verdicts = dict(pool.map(judge, urls))
        for provider, us in arms.items():
            ranked = sorted(((((verdicts.get(u) or {}).get("official_site") or {}).get("probability") or 0.0, u) for u in us), reverse=True)
            url, official, whose = pick(card, us, pages, verdicts)
            alt = pick(card, us, pages, verdicts, name_check=True)
            second = next(((p, u) for p, u in ranked if u != url), (None, None))
            r.decide({"item_id": item_id, "test": f"{sample}_{provider}", "decision": "found" if url else "none",
                      "website": url, "probability": official if url else None, "runner_up": second[1],
                      "runner_up_probability": second[0], "n_calls": 1 + len(us), "latency_ms": fetch_ms,
                      "path": {"rule": PICK_RULE, "whose_site": whose, "pick_with_name_check": alt[0],
                               "candidates": [{"url": u, "p": p, "whose_site": ((verdicts.get(u) or {}).get("whose_site") or {}).get("choice"),
                                               "readable": readable(pages.get(u) or {}),
                                               "names_body": names_body(card.get("name"), f"{(pages.get(u) or {}).get('title') or ''} {(pages.get(u) or {}).get('text') or ''}")}
                                              for p, u in ranked]}})
    r.finish()


def live_check_host(url: str | None) -> str | None:
    from ..labels.websites import host
    return host(url)


BUDGETS = {"w1": 0.10, "r": 0.05, "triage": 0.05, "s": 1.50, "n": 1.50, "n2": 0.60}  # each test's default cap, USD


def main(argv: list[str] | None = None) -> None:
    import argparse
    ap = argparse.ArgumentParser(prog="python -m ddwb.evals.run")
    ap.add_argument("test", choices=sorted(BUDGETS))
    ap.add_argument("--budget", type=float, help="this run's cap in USD (default: the test's own)")
    ap.add_argument("--approved-by", default="", help="who approved the run (free text, recorded with it)")
    a = ap.parse_args(argv)
    budget = BUDGETS[a.test] if a.budget is None else a.budget
    {"w1": lambda: run_w1(budget, a.approved_by), "r": lambda: run_r(budget, a.approved_by),
     "triage": lambda: run_triage(budget, a.approved_by), "s": lambda: run_search("S", budget, a.approved_by),
     "n": lambda: run_search("N", budget, a.approved_by), "n2": lambda: run_search("N2", budget, a.approved_by)}[a.test]()


if __name__ == "__main__":
    main()
