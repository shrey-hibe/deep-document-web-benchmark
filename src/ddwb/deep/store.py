"""The deep dataset: what the deep chain did for each body, step by step, as log tables (ddwb.runlog).

One run walks each body from its homepage to its meetings, documents, items and answers. Every request, candidate,
link, document and item is a row, with the plain rule's verdict and why; Jev's verdicts join later by call ID on the
same rows. Events are an append-only log with dates and quotes: state over time is derived from them later.
"""

from __future__ import annotations

from ..runlog import B, spec

D = "deep"
BODY = ("body_id", "VARCHAR", "the body (entities.bodies)", B)

STEPS = spec(D, "steps", "step of the chain for one body", "The trace: each step the chain took for a body, in order", [
    BODY, ("seq", "INTEGER", "order within the body, from 1", B),
    ("stage", "VARCHAR", "reach, locate, route, list, fetch, read, items, answer", B),
    ("action", "VARCHAR", "what was done", B), ("url", "VARCHAR", "the URL it acted on", B),
    ("outcome", "VARCHAR", "what came of it", B), ("detail", "JSON", "the step's facts", B),
    ("cost_usd", "DOUBLE", "paid cost of the step", B), ("elapsed_ms", "INTEGER", "time taken", B), ("at", "TIMESTAMPTZ", "when", B)])

FETCHES = spec(D, "fetches", "request", "Every request: how it was made, what came back, what it cost", [
    BODY, ("stage", "VARCHAR", "the chain stage", B), ("via", "VARCHAR", "http (ours), firecrawl (the scrape provider) or diligent_api", B),
    ("url", "VARCHAR", "requested", B), ("final_url", "VARCHAR", "after redirects", "copy"), ("status", "INTEGER", "HTTP status", "copy"),
    ("content_type", "VARCHAR", "Content-Type", "copy"), ("bytes", "BIGINT", "bytes received", "copy"),
    ("sha256", "VARCHAR", "of the bytes (or of the Markdown, for Firecrawl)", B), ("error", "VARCHAR", "when it failed", "copy"),
    ("elapsed_ms", "INTEGER", "time taken", B), ("from_cache", "BOOLEAN", "answered from our local cache", B),
    ("bot_wall", "BOOLEAN", "403, 429 or a bot-check page: the chain stops on this host", "label"),
    ("proxy", "VARCHAR", "Firecrawl's reported proxy, when given", "copy"), ("cost_usd", "DOUBLE", "paid cost", B),
    ("path", "VARCHAR", "local copy", B), ("fetched_at", "TIMESTAMPTZ", "when", B)])

CANDIDATES = spec(D, "candidates", "candidate meetings page for a body", "Pages that might hold the board's meetings, and the rule's score", [
    BODY, ("source", "VARCHAR", "homepage_link, search_open or search_site", B), ("rank", "INTEGER", "rank in its source", B),
    ("url", "VARCHAR", "the candidate", "copy"), ("title", "VARCHAR", "its title or link text", "copy"),
    ("score", "INTEGER", "the plain rule's score", "label"), ("reasons", "JSON", "why: the rule's parts", "label"),
    ("platform", "VARCHAR", "known meeting platform, if its host is one", "label"),
    ("checked", "BOOLEAN", "fetched to see if it lists meetings", B), ("is_listing", "BOOLEAN", "it lists dated meeting documents", "label"),
    ("dated_doc_links", "INTEGER", "dated document-like links on it", B), ("picked", "BOOLEAN", "used as a source", "label")])

LINKS = spec(D, "links", "link on a meetings list or a meeting's page", "Each link read on a listing: what the rules made of it", [
    BODY, ("page_url", "VARCHAR", "the page it is on", B), ("href", "VARCHAR", "the link", "split"),
    ("text", "VARCHAR", "its text", "split"), ("row", "VARCHAR", "the text of its table row or list item", "split"),
    ("heading", "VARCHAR", "the heading above it", "split"), ("found_date", "DATE", "date read from text, URL or row", "label"),
    ("kind", "VARCHAR", "agenda, packet, minutes, meeting_page, listing_page, video, document or other", "label"),
    ("governing", "BOOLEAN", "the governing board's (not a committee's)", "label"), ("reason", "VARCHAR", "the rule that decided", "label"),
    ("followed", "BOOLEAN", "fetched next", B)])

LISTINGS = spec(D, "listings", "meetings list as fetched", "Each meetings list: its fingerprint, for knowing when to look again", [
    BODY, ("url", "VARCHAR", "the list", B), ("via", "VARCHAR", "http, firecrawl or diligent_api", B),
    ("platform", "VARCHAR", "the meeting platform, or own_site", "label"), ("fingerprint", "VARCHAR", "hash of its document-like links", B),
    ("n_links", "INTEGER", "links on it", B), ("n_doc_links", "INTEGER", "document-like links", B),
    ("years", "JSON", "years its dated links cover", B), ("fetched_at", "TIMESTAMPTZ", "when", B)])

MEETINGS = spec(D, "meetings", "meeting found", "Meetings found for a body since 2024, from listings or a portal", [
    BODY, ("meeting_date", "DATE", "the date", "label"), ("title", "VARCHAR", "as listed", "copy"),
    ("governing", "BOOLEAN", "the governing board's", "label"), ("source_url", "VARCHAR", "where it was listed", B),
    ("platform", "VARCHAR", "the platform", "label"), ("platform_key", "VARCHAR", "the platform's ID, when it has one", "copy"),
    ("n_documents", "INTEGER", "documents linked", B)])

DOCUMENTS = spec(D, "documents", "document (agenda, packet, minutes) of a meeting", "Each document found, downloaded and read", [
    BODY, ("meeting_date", "DATE", "its meeting", "label"), ("kind", "VARCHAR", "agenda, packet, minutes, attachment", "label"),
    ("url", "VARCHAR", "the file", "copy"), ("title", "VARCHAR", "as linked", "copy"), ("listing_url", "VARCHAR", "where it was found", B),
    ("governing", "BOOLEAN", "the governing board's", "label"), ("downloaded", "BOOLEAN", "fetched in this run", B),
    ("path", "VARCHAR", "local copy (data/deep/...)", B), ("bytes", "BIGINT", "size", B), ("sha256", "VARCHAR", "of the file", B),
    ("content_type", "VARCHAR", "Content-Type", "copy"), ("pdf_type", "VARCHAR", "text_based, scanned, image_based, mixed", "label"),
    ("pages", "INTEGER", "pages", "copy"), ("pages_needing_ocr", "INTEGER", "pages with no text layer", "label"),
    ("text_chars", "INTEGER", "characters of text read", B), ("read_ms", "INTEGER", "time to read", B), ("error", "VARCHAR", "why not read", B)])

ITEMS = spec(D, "items", "passage of a document that may record a buying event", "Items cut from documents and what the rules read in them", [
    BODY, ("meeting_date", "DATE", "its meeting", "label"), ("doc_kind", "VARCHAR", "agenda, packet, minutes", "label"),
    ("doc_url", "VARCHAR", "the document", B), ("seq", "INTEGER", "order in the document", B), ("text", "VARCHAR", "the passage (up to 1,200 characters)", "split"),
    ("text_hash", "VARCHAR", "hash of the normalised passage", B), ("amounts", "JSON", "dollar amounts in it", "split"),
    ("amount_max", "DOUBLE", "the largest", "split"), ("vendor_candidates", "JSON", "organisation-like names in it", "split"),
    ("vendor", "VARCHAR", "the rule's vendor: the first candidate", "label"), ("term_text", "VARCHAR", "a stated term ('through June 30, 2027')", "split"),
    ("term_end_year", "INTEGER", "the year the term ends, if stated", "split"),
    ("event_type", "VARCHAR", "contract, renewal, bid, grant, budget, purchase, staffing or none (keyword rule)", "label"),
    ("action", "VARCHAR", "approved, to_consider or unclear (keyword rule)", "label"),
    ("event_key", "VARCHAR", "same event within one meeting (date, amount, vendor): agenda, packet and minutes merge", B),
    ("first_in_meeting", "BOOLEAN", "the event's row to count (minutes preferred, then agenda, then packet)", B)])

ANSWERS = spec(D, "answers", "standard question for one body", "Answers to the standard questions, from the stored tables", [
    BODY, ("question_id", "VARCHAR", "Q1 bought, Q2 open, Q3 next", B), ("question", "VARCHAR", "as asked", B),
    ("as_of", "DATE", "the date the answer holds for", B), ("n_rows", "INTEGER", "rows in the answer", B),
    ("rows", "JSON", "the answer, each row with its quote and source", "label"), ("answer_ms", "INTEGER", "time to answer from the tables", B)])

CHECKS = spec(D, "checks", "check of one step for one body", "Phase 1 scorecard: the chain against what we already know", [
    BODY, ("check", "VARCHAR", "what is checked", B), ("expected", "VARCHAR", "the known answer", B),
    ("got", "VARCHAR", "what the chain found", B), ("passed", "BOOLEAN", "they match", "label"), ("note", "VARCHAR", "source of the known answer", B)])

ALL = (STEPS, FETCHES, CANDIDATES, LINKS, LISTINGS, MEETINGS, DOCUMENTS, ITEMS, ANSWERS, CHECKS)

# Phase 2: who decides at each fork (rules, Jev, a small model, a bigger model), on identical logged inputs
DECISIONS = spec(D, "decisions", "decision at one fork, by one decider", "Each decider's answer to each fork item, with cost and time", [
    BODY, ("fork", "VARCHAR", "list_page, doc_link or buying_event", B), ("item_id", "VARCHAR", "the fork item, stable across deciders", B),
    ("arm", "VARCHAR", "rules, jev, small or big", B), ("model", "VARCHAR", "the model, when there is one", B),
    ("state_sha256", "VARCHAR", "hash of exactly what the decider saw (the same for every arm)", B),
    ("answer", "JSON", "the answer: the fork's fields", "label"), ("call_id", "VARCHAR", "the paid call, if any", B),
    ("input_tokens", "INTEGER", "provider- or agent-reported", B), ("output_tokens", "INTEGER", "provider- or agent-reported", B),
    ("cost_usd", "DOUBLE", "cash cost (0 for rules and for the Max plan)", B), ("api_equivalent_usd", "DOUBLE", "what the tokens would cost on the API", B),
    ("latency_ms", "INTEGER", "time to answer", B), ("error", "VARCHAR", "when there was no usable answer", B)])

GOLD = spec(D, "gold", "fork item, as labelled by one source of truth", "Truth for fork items: known lists, the document itself, or blind judges", [
    BODY, ("fork", "VARCHAR", "the fork", B), ("item_id", "VARCHAR", "the item", B),
    ("source", "VARCHAR", "known_list, document_header_judge_a, document_header_judge_b, passage_judge_a, passage_judge_b", B),
    ("label", "JSON", "the true fields", "label"), ("evidence", "VARCHAR", "what it was decided from", B),
    ("shown", "JSON", "exactly what the judge saw (to audit blindness)", B)])

PHASE2 = (DECISIONS, GOLD)

# Navigation (2026-11-03): the agents' state store (ddwb.deep.state), copied here when a session answers
NAV_EVENTS = spec(D, "nav_events", "event in one agent session's navigation state",
                  "Every node an agent touched (page, list, link, passage, event, answer) and what it did, in order", [
    ("session", "VARCHAR", "run:arm:body", B), ("arm", "VARCHAR", "haiku, haiku-jev, opus, opus-jev (or test)", B), BODY,
    ("seq", "BIGINT", "order in the run's store", B), ("at", "TIMESTAMPTZ", "when", B),
    ("node", "VARCHAR", "the node: G page, K link, M meeting, P passage, E event, A answer, B body", B),
    ("kind", "VARCHAR", "page, link, meeting, passage, event, answer, body", B), ("parent", "VARCHAR", "the node it came from", B),
    ("url", "VARCHAR", "its address, when it has one", "copy"),
    ("status", "VARCHAR", "found, fetched, checked, accepted, listed, scored, peeked, read, kept, labelled, recorded, skipped, "
                          "unreachable, answered", "label"),
    ("data", "JSON", "the node's facts after this event (each event carries the whole state)", B),
    ("jev", "JSON", "Jev's answers for the node, or its error", "label"), ("reason", "VARCHAR", "why (required to skip)", "label"),
    ("tool", "VARCHAR", "the tool that wrote it", B), ("cost_usd", "DOUBLE", "paid cost of this event", B),
    ("budget", "VARCHAR", "which approved cap paid: map or reading", B), ("ms", "INTEGER", "time taken", B)])
