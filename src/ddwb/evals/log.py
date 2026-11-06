"""The eval dataset: everything a test run asked, got back, decided and cost, written in batches as it runs.

Inputs are kept exactly (the state text Jev saw, the questions, the search queries), so any result can be re-scored
later or re-run on another model or search provider with identical inputs. Truth (eval.gold) is written only by the
blind checker and never read by the runner. Cost and latency go with every call; the provider layer's ledger stays the
record of spend.
"""

from __future__ import annotations

from .. import runlog
from ..runlog import B, now  # noqa: F401  (now: used by the runners)


def _spec(name: str, grain: str, description: str, columns: list[tuple[str, str, str, str]], dataset: str = "eval"):
    """columns: (name, DuckDB type, meaning, how)."""
    return runlog.spec(dataset, name, grain, description, columns)


RUNS = _spec("runs", "test run's status (a run gets a row when it finishes, stops or is superseded; its latest row is current)",
             "Each test run: what it tested, who approved it, its budget, what it spent and its status", [
    ("test", "VARCHAR", "W1, W2, R, N_triage, S_search, N_search, probe, ...", B),
    ("purpose", "VARCHAR", "what the run is for", B), ("code_commit", "VARCHAR", "the repo commit that ran it", B),
    ("router_run_id", "VARCHAR", "the run ID in the provider ledger", B), ("approved_by", "VARCHAR", "who said yes", B),
    ("approval_words", "VARCHAR", "the yes, in their words", B), ("decision", "VARCHAR", "a reference for the approval, if any", B),
    ("budget_usd", "DOUBLE", "the cap for the run", B), ("started_at", "TIMESTAMPTZ", "start", B),
    ("finished_at", "TIMESTAMPTZ", "end", B), ("status", "VARCHAR", "finished, stopped (why) or superseded (by which run)", B),
    ("items", "INTEGER", "items in the run", B), ("calls", "INTEGER", "paid calls made", B),
    ("spend_usd", "DOUBLE", "provider-reported spend", B), ("notes", "VARCHAR", "anything else", B)])

ITEMS = _spec("items", "item in a test run", "The sample row and the exact fact card built for it", [
    ("item_id", "VARCHAR", "the item, stable across runs and arms", B),
    ("sample", "VARCHAR", "W, S, N or R", B), ("stratum", "VARCHAR", "the sample's group", B),
    ("body_id", "VARCHAR", "the body (W, S, N)", B),
    ("record_id", "VARCHAR", "the body's anchor record (W, S, N): its agency ID, stable across rebuilds, for joining later", B),
    ("from_record_id", "VARCHAR", "R: record A", B),
    ("to_record_id", "VARCHAR", "R: record B", B), ("card_version", "VARCHAR", "the fact-card template's version", B),
    ("card", "JSON", "the fact card, as given to Jev", B), ("draw_rank", "INTEGER", "its rank in the frozen sample", B)])

JEV_CALLS = _spec("jev_calls", "Jev call", "Each Jev call: the exact state and questions, model, tokens, cost and latency", [
    ("call_id", "VARCHAR", "the call", B), ("item_id", "VARCHAR", "the item", B),
    ("variant", "VARCHAR", "W1, W2, pick (a search result), triage, ...", B), ("candidate_url", "VARCHAR", "the page judged, when the call is about a page", B),
    ("question_set", "VARCHAR", "the question set and its version", B), ("state", "JSON", "the exact state Jev saw", B),
    ("state_sha256", "VARCHAR", "its hash", B), ("questions", "JSON", "the exact questions sent", B),
    ("model", "VARCHAR", "the model", B), ("route", "VARCHAR", "the provider (and model) that answered", B),
    ("input_tokens", "INTEGER", "provider-reported", B), ("output_tokens", "INTEGER", "provider-reported", B),
    ("cost_usd", "DOUBLE", "provider-reported (0 on a cache hit)", B), ("est_cost_usd", "DOUBLE", "the pre-call estimate (for the run's dollar limit)", B),
    ("cost_source", "VARCHAR", "where the cost came from", B), ("latency_ms", "INTEGER", "end to end, our clock", B),
    ("provider_ms", "INTEGER", "the provider's own time, from its metadata", B), ("cache_hit", "BOOLEAN", "answered from the cache", B),
    ("status", "VARCHAR", "ok or the error kind", B), ("error", "VARCHAR", "the error", B),
    ("generation_id", "VARCHAR", "the provider's ID, for audit", B), ("called_at", "TIMESTAMPTZ", "when", B)])

JEV_ANSWERS = _spec("jev_answers", "answer to one question in a Jev call", "Jev's answers: probabilities, choices, scores", [
    ("call_id", "VARCHAR", "the call", B), ("item_id", "VARCHAR", "the item", B),
    ("question", "VARCHAR", "the question's name", B), ("type", "VARCHAR", "boolean, choice or score", B),
    ("probability", "DOUBLE", "boolean: probability of yes", "label"), ("choice", "VARCHAR", "choice: the option picked", "label"),
    ("score", "DOUBLE", "score: the expected level, 0 = lowest", "label"), ("confidence", "DOUBLE", "Jev's confidence (choice, score)", "label"),
    ("probabilities", "JSON", "choice and score: the full spread", "label"), ("raw", "JSON", "the answer as returned", B)])

SEARCH_CALLS = _spec("search_calls", "search call", "Each search: provider, exact query and settings, cost and latency", [
    ("call_id", "VARCHAR", "the call", B), ("item_id", "VARCHAR", "the item", B),
    ("provider", "VARCHAR", "exa or parallel", B), ("route", "VARCHAR", "the provider (and model) that answered", B), ("kind", "VARCHAR", "search_open or search_site", B),
    ("query", "VARCHAR", "the query text", B), ("payload", "JSON", "the exact request", B), ("n_results", "INTEGER", "results returned", B),
    ("cost_usd", "DOUBLE", "provider-reported", B), ("cost_source", "VARCHAR", "where the cost came from", B),
    ("latency_ms", "INTEGER", "end to end, our clock", B), ("cache_hit", "BOOLEAN", "answered from the cache", B),
    ("status", "VARCHAR", "ok or the error kind", B), ("error", "VARCHAR", "the error", B), ("called_at", "TIMESTAMPTZ", "when", B)])

SEARCH_RESULTS = _spec("search_results", "result returned by a search call", "Each result, as the provider returned it", [
    ("call_id", "VARCHAR", "the search call", B), ("item_id", "VARCHAR", "the item", B),
    ("provider", "VARCHAR", "exa or parallel", B), ("rank", "INTEGER", "its rank, from 1", B), ("url", "VARCHAR", "the result", B),
    ("host", "VARCHAR", "its host", B), ("title", "VARCHAR", "its title", B), ("published_date", "VARCHAR", "as returned", B),
    ("excerpt", "VARCHAR", "text the provider returned with it (Parallel's excerpts)", B), ("raw", "JSON", "the result as returned", B)])

DECISIONS = _spec("decisions", "item, per test run", "What the system concluded for each item, and what it cost to get there", [
    ("item_id", "VARCHAR", "the item", B), ("test", "VARCHAR", "the test", B),
    ("decision", "VARCHAR", "found, on_parent_site, none, unclear (website tests); same_body, run_by, separate (R)", "label"),
    ("website", "VARCHAR", "the website picked, if any", "label"), ("probability", "DOUBLE", "Jev's probability for the pick", "label"),
    ("runner_up", "VARCHAR", "the next best candidate", "label"), ("runner_up_probability", "DOUBLE", "its probability", "label"),
    ("triage", "VARCHAR", "N: triage's prediction, recorded before search", "label"),
    ("n_calls", "INTEGER", "paid calls behind the decision", B), ("cost_usd", "DOUBLE", "their total cost", B),
    ("latency_ms", "INTEGER", "their total time, plus page fetches", B), ("path", "JSON", "the steps taken", B)])

GOLD = _spec("gold", "item, as labelled by a checker", "Truth: written only by the blind checker, never read by the runner", [
    ("item_id", "VARCHAR", "the item", B), ("sample", "VARCHAR", "W, S, N or R", B), ("checker", "VARCHAR", "which checker (model and role)", B),
    ("labelled_at", "TIMESTAMPTZ", "when", B), ("true_website", "VARCHAR", "the body's own website, or empty if it has none", "label"),
    ("public_page", "VARCHAR", "else the best public page about it (parent's page, state listing)", "label"),
    ("whose_site", "VARCHAR", "for W: whose site the given website is", "label"), ("same_organisation", "BOOLEAN", "R: A and B are one", "label"),
    ("b_runs_a", "BOOLEAN", "R: B runs A", "label"), ("evidence_urls", "JSON", "pages the checker read", "label"),
    ("evidence_quote", "VARCHAR", "a short line from a page that settles it", "label"), ("confidence", "VARCHAR", "high, medium or low", "label"),
    ("notes", "VARCHAR", "anything else", "label"), ("shown", "JSON", "exactly what the checker was given (to audit blindness)", B)])

PAGE_FETCHES = _spec("page_fetches", "check of one URL", "Pages we fetched other than homepages (search results now; agendas and documents later)", [
    ("url", "VARCHAR", "the URL requested", B), ("host", "VARCHAR", "its host", B), ("purpose", "VARCHAR", "why it was fetched", B),
    ("eval_run_id", "VARCHAR", "the eval run that fetched it, if any", B), ("status", "INTEGER", "HTTP status", "copy"),
    ("final_url", "VARCHAR", "after redirects", "copy"), ("content_type", "VARCHAR", "Content-Type", "copy"), ("bytes", "BIGINT", "bytes read", "copy"),
    ("sha256", "VARCHAR", "of the bytes read", B), ("error", "VARCHAR", "when there was no response", "copy"),
    ("elapsed_ms", "INTEGER", "time taken", B), ("title", "VARCHAR", "its <title>", "split"), ("description", "VARCHAR", "meta description", "split"),
    ("site_name", "VARCHAR", "og:site_name", "split"), ("text", "VARCHAR", "visible text, first 15,000 characters", "split"),
    ("link_count", "INTEGER", "links on the page", B), ("bot_wall", "BOOLEAN", "403, 429 or a bot check", "label"),
    ("parked", "BOOLEAN", "a parked domain page", "label"), ("fetched_at", "TIMESTAMPTZ", "when", B)], dataset="web")

ALL = (RUNS, ITEMS, JEV_CALLS, JEV_ANSWERS, SEARCH_CALLS, SEARCH_RESULTS, DECISIONS, GOLD, PAGE_FETCHES)


def Log(run_id: str, flush_every: int = 200, specs: tuple = ALL) -> runlog.Log:
    """The eval tables' run log (ddwb.runlog), plus any extra tables a runner passes."""
    return runlog.Log(run_id, specs, flush_every)
