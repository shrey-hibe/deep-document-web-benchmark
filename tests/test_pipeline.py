"""The pipeline (ddwb.pipeline), offline: a fake model, a fake Jev, documents served from memory. Nothing is fetched or
paid: the conftest blocks the provider layer's transports, and these tests block httpx too."""

import argparse
import dataclasses
import json
import re
import subprocess
from types import SimpleNamespace

import httpx
import pytest

from ddwb.deep import state as nav_state
from ddwb.deep import profile, tools, truth, web
from ddwb.deep.state import State, nid
from ddwb.pipeline import __main__ as cli
from ddwb.pipeline import evaluate, form, replay
from ddwb.pipeline.backends import Backend, CacheOnlyBackend, ClaudeCodeBackend, ModelDecider, Reply, ResponseCache
from ddwb.pipeline.driver import ARMS, PipeCx, Pipeline, plan_date
from ddwb.pipeline.items import adapter_items

CARD = {"name": "CITY OF CORTEZ", "kind": "municipality", "state": "CO"}
MINUTES = """CITY COUNCIL REGULAR MEETING MINUTES
1. Call to order. The meeting was called to order at 7 pm.
2. CONSENT AGENDA
a. Approval of the minutes of the meeting of May 1, 2025.
b. Approval of a contract with Acme Paving Inc. for street repair in the amount of $125,000.
c. Approval of the purchase of a dump truck from Valley Ford for $98,500.
Motion carried 5-0.
3. NEW BUSINESS
a. Resolution 2025-14 accepting a library services grant from the State Library for $45,000. Motion carried 5-0.
b. Discussion of a new budget software subscription with Tyler Technologies for $12,000; staff will report back.
4. Adjournment at 9 pm."""


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a pipeline test tried to reach the network")
    monkeypatch.setattr(httpx.Client, "send", refuse)
    monkeypatch.setattr(httpx.Client, "stream", refuse)


class FakeHttp:
    """Documents from memory; records what was asked, in order."""

    def __init__(self, docs: dict[str, str]):
        self.docs, self.asked = docs, []

    def get(self, url, use_cache=True, fresh_since=None):
        self.asked.append(url)
        if url not in self.docs:
            return web.Response(url, error="not served")
        kind = "text/html" if self.docs[url].lstrip().startswith("<html") else "text/plain"
        return web.Response(url, final_url=url, status=200, content_type=kind, body=self.docs[url].encode())


class FakeJev:
    """p by a word in the passage."""

    def __init__(self, rules: dict[str, float], default=0.1):
        self.rules, self.default, self.calls = rules, default, 0

    def many(self, cx, items, qs, budget):
        out = {}
        for key, state, *_ in items:
            self.calls += 1
            p = next((v for w, v in self.rules.items() if w.lower() in state["passage"].lower()), self.default)
            out[key] = ({"is_event": p, "type": "contract", "action": "approved", "people": 0.0}, 0.0, None)
        return out


def cards_of(prompt: str) -> list[tuple[int, str, str]]:
    """(card number, its note, its text) for each card in a prompt."""
    out = []
    for m in re.finditer(r"^\[(\d+)\][^\n]*\n(.*?)(?=\n\n\[\d+\]|\Z)", prompt.split("ITEM CARDS:", 1)[1], re.S | re.M):
        body = m.group(2)
        note = re.search(r"CHECK AGAIN: ([^\n]*)", body)
        out.append((int(m.group(1)), note.group(1) if note else "", body.split("text:\n", 1)[-1]))
    return out


class FakeModel(Backend):
    """A model that answers by a rule on (tier, card text, note); every prompt is kept."""

    family, live = "fake", False

    def __init__(self, rule, cost=0.0):
        super().__init__(ResponseCache(":memory:"))
        self.rule, self.cost, self.prompts = rule, cost, []

    def ask(self, prompt, tier):
        self.prompts.append((tier, prompt))
        objs = []
        for n, note, body in cards_of(prompt):
            objs += [o | {"item": n} for o in self.rule(tier, body, note)]
        return Reply(json.dumps(objs), self.cost, model=f"fake:{tier}")


def event(quote, kind="contract", amount=None, vendor=None, what="thing", unsure=False):
    return {"event": True, "type": kind, "action": "approved", "vendor": vendor, "amount": amount, "what": what, "quote": quote,
            "unsure": unsure}


def plain_rule(tier, body, note):
    if "Acme" in body:
        return [event("contract with Acme Paving Inc.", amount=125000, vendor="Acme Paving Inc.", what="street repair")]
    if "dump truck" in body:
        return [event("purchase of a dump truck", "purchase", 98500, "Valley Ford", "dump truck")]
    if "grant" in body:
        return [event("accepting a library services grant", "grant", 45000, "State Library", "library services grant")]
    return [{"event": False}]


def session(run="r1", arm="haiku-jev", body="cortez-co", links=(), http=None, jev=None, offline=True):
    sid = f"{run}:pipe-{arm}:{body}"
    st = State(run)
    st.start(sid, run_id=run, arm=f"pipe-{arm}", body=body, body_id="b1", query="q1", since="2025-01-01",
             started_at=nav_state.now(), card=CARD, homepage="https://x", status="started")
    st.put(sid, "B", "body", "started", data={})
    st.put(sid, "G0000001", "page", "mapped", url="https://x/meetings", data={"notes": []})
    for url, when, kind, extra in links:
        st.put(sid, nid("K", url), "link", "listed", url=url, parent="G0000001",
               data={"date": when, "rule_kind": kind, "rule_governing": True, "text": extra or f"{kind} {when}", "row": extra or ""})
    return PipeCx(sid, jev_on=ARMS[arm].jev, http=http, jev=jev, offline=offline)


def pipeline(cx, arm, rule=plain_rule, big_rule=None, cost=0.0):
    model = FakeModel(rule, cost)
    big = FakeModel(big_rule or rule, cost)
    small_d = ModelDecider(model, ARMS[arm].small)
    big_d = ModelDecider(big, ARMS[arm].big) if ARMS[arm].big else None
    return Pipeline(cx, ARMS[arm], small_d, big_d, out=lambda *a: None), model, big


# ------------------------------------------------------------------------------------------- items
@pytest.fixture
def stand_in_items(monkeypatch):
    """The driver tests below were written against the stand-in items (exact procedural counts); the driver now uses
    the structure layer (tested in test_structure.py), so these pin the stand-in they were written for."""
    from ddwb.pipeline import driver, items as pipeline_items
    monkeypatch.setattr(driver, "document_items", pipeline_items.adapter_items)


def test_items_regroup_votes_carry_the_consent_vote_and_tag_procedural_items():
    items = adapter_items([(1, MINUTES, False)])
    by = {it["number"]: it for it in items}
    assert by["1"]["procedural"] == "procedural" and by["4"]["procedural"] == "fragment"
    assert by["2.b"]["section"] == "2. CONSENT AGENDA" and by["2.b"]["amounts"] == [125000.0]
    assert "Motion carried 5-0" in by["2.c"]["outcome"]  # its own (the vote line joined it)
    assert "the vote on the whole" in by["2.b"]["outcome"]  # carried from the consent vote
    assert by["2.b"]["passages"][0]["text"].startswith("b. Approval of a contract")  # the navigator's passage, untouched


def test_an_item_cut_at_a_page_break_is_one_item_with_both_passages():
    items = adapter_items([(1, "3. NEW BUSINESS\na. Approval of a contract with Acme Paving Inc. for", False),
                           (2, "street repair for $125,000.\nMotion carried 5-0.\nb. Next item about a policy update for staff.", False)])
    acme = next(it for it in items if "Acme" in it["text"])
    assert acme["pages"] == [1, 2] and len(acme["passages"]) == 2 and acme["amounts"] == [125000.0]
    assert acme["outcome"] == "Motion carried 5-0."


# ------------------------------------------------------------------------------------------- form
def test_the_form_checks_quote_amount_type_and_action():
    items = {1: {"text": "Approval of a contract with Acme Paving Inc. for $125,000."}}
    ok, why = form.check({"item": 1, "event": True, "type": "contract", "action": "approved", "amount": "$125,000",
                          "what": "paving", "quote": "contract with  ACME Paving"}, items)
    assert why is None and ok["amount"] == 125000.0
    assert "does not occur" in form.check({"item": 1, "event": True, "type": "contract", "action": "approved",
                                          "what": "x", "quote": "a contract with Zeta Corp"}, items)[1]
    assert "type" in form.check({"item": 1, "event": True, "type": "staffing", "action": "approved", "what": "x",
                                 "quote": "contract with Acme"}, items)[1]
    assert "action" in form.check({"item": 1, "event": True, "type": "contract", "action": "done", "what": "x",
                                   "quote": "contract with Acme"}, items)[1]
    assert "not a number" in form.check({"item": 1, "event": True, "type": "contract", "action": "approved", "what": "x",
                                         "amount": "lots", "quote": "contract with Acme"}, items)[1]
    assert form.check({"item": 2, "event": False}, items)[1].startswith("item 2")
    good, bad = form.sort_answers([{"item": 1, "event": False}], {1: items[1], 2: items[1]})
    assert good == {1: [{"item": 1, "event": False, "unsure": False}]} and bad == {2: ["no answer for this item"]}
    assert form.parse('```json\n[{"item": 1, "event": false}]\n```')[0] == [{"item": 1, "event": False}]
    assert form.parse("I cannot answer")[0] is None


def test_a_quote_from_the_items_title_or_outcome_passes_the_check():
    items = {1: {"title": "Award of contract to Acme Paving Inc. for street repair", "outcome": "approved 7-0",
                 "text": "Staff recommends approval."}}
    ok, why = form.check({"item": 1, "event": True, "type": "contract", "action": "approved", "what": "paving",
                          "quote": "Award of contract to Acme Paving"}, items)
    assert why is None and ok["quote"].startswith("Award")


def test_items_a_model_leaves_out_of_a_long_batch_are_asked_again_a_few_at_a_time():
    class Dropper(FakeModel):  # answers only the first three cards of any prompt, as a small model does on a long batch
        def ask(self, prompt, tier):
            self.prompts.append((tier, prompt))
            objs = [{"item": n, "event": False} for i, (n, note, body) in enumerate(cards_of(prompt)) if i < 3]
            return Reply(json.dumps(objs), self.cost, model=f"fake:{tier}")
    cx = session()
    p, _, _ = pipeline(cx, "haiku-jev")
    model = Dropper(plain_rule)
    cards = [(n, {"title": f"item {n}", "text": f"Report number {n} on library hours."}) for n in range(1, 9)]
    answers, failed = p.ask_all(ModelDecider(model, ARMS["haiku-jev"].small), {"name": "Cortez", "date": "2025-06-10", "url": "https://x/m.txt"}, cards, None, "decide")
    assert sorted(answers) == list(range(1, 9)) and failed == {}
    assert p.stats["split_items"] == 2 and len(model.prompts) == 3  # the batch, the retry, then the last two together


def test_a_failed_check_is_asked_once_more_then_left_undecided_and_counted():
    cx = session(links=[("https://x/m.txt", "2025-06-10", "minutes", None)], http=FakeHttp({"https://x/m.txt": MINUTES}),
                 jev=FakeJev({}, default=0.5))

    def rule(tier, body, note):
        if "Acme" in body:  # a quote not in the text first; right when asked again
            q = "contract with Acme Paving Inc." if "failed a check" in note else "contract with Acme Corp"
            return [event(q, amount=125000, vendor="Acme Paving Inc.", what="street repair")]
        if "dump truck" in body:  # never a number
            return [event("purchase of a dump truck", "purchase", "lots", "Valley Ford", "dump truck")]
        return plain_rule(tier, body, note)
    p, model, _ = pipeline(cx, "haiku-jev", rule)
    p.big = None  # no escalation in this test
    answer = p.run()
    # Acme and the truck retried once; the truck, a consent item still undecided, is asked again by the check (and retried)
    assert p.stats["retried_items"] == 3 and p.stats["check_asked"] == 2  # and Tyler: an amount, said "no event"
    assert p.undecided == {"form check failed twice": 1}
    whats = sorted(e["what"] for e in answer["events"])
    assert whats == ["library services grant", "street repair"]
    truck = next(n for n in cx.st.nodes(cx.sid, "passage") if "dump truck" in n["data"]["text"])
    assert truck["status"] == "kept" and "form check failed twice" in truck["data"]["undecided"]  # undecided, never guessed
    assert answer["complete"] is False and "1 passages undecided" in "; ".join(answer["incomplete_because"])


# ------------------------------------------------------------------------------------------- order of work
def test_a_date_listed_only_as_a_meetings_own_page_is_read_through_that_page():
    page = ('<html><body><h1>Regular Meeting, June 10, 2025</h1>'
            '<a href="https://x/files/minutes-2025-06-10.txt">Minutes</a></body></html>')
    http = FakeHttp({"https://x/meetings/june-10": page, "https://x/files/minutes-2025-06-10.txt": MINUTES})
    cx = session(links=[("https://x/meetings/june-10", "2025-06-10", "meeting_page", "Regular Meeting June 10, 2025")],
                 http=http, jev=FakeJev({}, default=0.5))
    p, _, _ = pipeline(cx, "haiku-jev")
    p.big = None
    answer = p.run()
    assert p.stats["meeting_pages_opened"] == 1 and p.stats["dates_read_from_minutes"] == 1
    assert "street repair" in {e["what"] for e in answer["events"]}  # the minutes the page links were read


@pytest.mark.usefixtures("stand_in_items")
def test_meetings_are_read_newest_first_by_the_reading_plan_one_document_per_model_context():
    docs = {"https://x/0301-min.txt": MINUTES, "https://x/0301-ws.txt": "1. Work session on goals for $5,000 of training.",
            "https://x/0201-ag.txt": MINUTES.replace("MINUTES", "AGENDA"), "https://x/0115-pk.txt": MINUTES.replace("MINUTES", "PACKET")}
    http = FakeHttp(docs)
    cx = session(links=[
        ("https://x/0115-pk.txt", "2025-01-15", "packet", None),
        ("https://x/0120-report.pdf", "2025-01-20", "item", "Financial report January"),
        ("https://x/0201-pk.txt", "2025-02-01", "packet", None),
        ("https://x/0201-ag.txt", "2025-02-01", "agenda", None),
        ("https://x/0301-ag.txt", "2025-03-01", "agenda", "Regular meeting agenda"),
        ("https://x/0301-ws.txt", "2025-03-01", "minutes", "Work Session Minutes"),
        ("https://x/0301-min.txt", "2025-03-01", "minutes", "Regular meeting minutes"),
    ], http=http, jev=FakeJev({}, default=0.9))
    p, model, _ = pipeline(cx, "haiku-jev")
    answer = p.run()
    assert http.asked == ["https://x/0301-min.txt", "https://x/0201-ag.txt", "https://x/0115-pk.txt"]  # newest first, minutes > agenda > packet
    status = {n["url"]: n for n in cx.st.nodes(cx.sid, "link")}
    assert status["https://x/0301-ag.txt"]["status"] == "skipped" and status["https://x/0301-ag.txt"]["data"]["covered_by"]
    assert status["https://x/0301-ws.txt"]["status"] == "skipped" and "work or study session" in status["https://x/0301-ws.txt"]["reason"]
    assert status["https://x/0201-pk.txt"]["data"].get("covered_by")  # the agenda covers its packet
    assert status["https://x/0120-report.pdf"]["data"]["plan"] == "no_meeting_document"
    small_prompts = [pr for tier, pr in model.prompts if tier == "haiku"]
    for pr in small_prompts:  # one document per model context
        assert len({d for d in ("2025-03-01", "2025-02-01", "2025-01-15") if f"meeting of {d}" in pr}) == 1
    assert sum("agenda of the meeting of 2025-02-01" in pr for pr in small_prompts) >= 1
    dates = {e["date"]: e["action"] for e in answer["events"]}
    assert set(dates) == {"2025-03-01", "2025-02-01", "2025-01-15"}
    assert answer["coverage"]["governing_meetings_unopened"] == ["2025-01-20"]  # said so, never claimed covered
    assert p.stats["items_procedural"] == 3 * 5  # title line, call to order, two bare headings, adjournment: per document


def test_plan_date_keeps_a_work_session_only_when_the_date_has_nothing_else():
    ws = {"node": "K1", "status": "listed", "data": {"rule_kind": "minutes", "text": "Work Session Minutes"}}
    reg = {"node": "K2", "status": "listed", "data": {"rule_kind": "agenda", "text": "Regular Meeting Agenda"}}
    assert plan_date([ws, reg])[:2] == ("agenda", [reg])
    assert plan_date([ws])[:2] == ("minutes", [ws])


@pytest.mark.usefixtures("stand_in_items")
def test_items_jev_sets_aside_never_reach_the_model_and_procedural_items_are_counted():
    cx = session(links=[("https://x/m.txt", "2025-06-10", "minutes", None)], http=FakeHttp({"https://x/m.txt": MINUTES}),
                 jev=FakeJev({"Acme": 0.95, "dump truck": 0.9, "grant": 0.9, "Tyler": 0.9}, default=0.05))
    p, model, _ = pipeline(cx, "haiku-jev")
    p.run()
    sent = "\n".join(pr for _, pr in model.prompts)
    assert "Acme" in sent and "minutes of the meeting of May 1" not in sent  # set aside by Jev (p 0.05)
    assert p.stats["items_set_aside_by_jev"] == 1 and p.stats["items_procedural"] == 5
    aside = next(n for n in cx.st.nodes(cx.sid, "passage") if "May 1" in n["data"]["text"])
    assert aside["status"] == "skipped" and aside["data"]["skipped_on_jev"] and aside["data"]["pipeline"]["verdict"] == "set_aside_jev"


# ------------------------------------------------------------------------------------------- the check and escalation
def test_the_check_asks_again_about_likely_purchases_the_model_called_no_event():
    cx = session(links=[("https://x/m.txt", "2025-06-10", "minutes", None)], http=FakeHttp({"https://x/m.txt": MINUTES}),
                 jev=FakeJev({"dump truck": 0.92, "Acme": 0.95, "grant": 0.95, "Tyler": 0.1}, default=0.1))

    def rule(tier, body, note):
        if "dump truck" in body and "Jev rates it" not in note:
            return [{"event": False}]
        return plain_rule(tier, body, note)
    p, model, _ = pipeline(cx, "haiku-jev", rule)
    answer = p.run()
    assert p.stats["check_asked"] == 1 and p.stats["check_changed"] == 1
    check_prompt = next(pr for _, pr in model.prompts if "CHECK AGAIN" in pr)
    assert "Jev rates it a likely buying decision (p=0.92)" in check_prompt and "states an amount ($98,500.00)" in check_prompt
    assert len(cards_of(check_prompt)) == 1  # only the item to check
    assert any(e["what"] == "dump truck" for e in answer["events"])


def test_unsure_items_and_jev_middling_items_escalate_to_the_bigger_model_with_their_cost(monkeypatch):
    from ddwb.pipeline import driver as _driver
    monkeypatch.setattr(_driver, "ESCALATE", (0.3, 0.7))  # the band mechanism, whatever policy.toml sets
    cx = session(links=[("https://x/m.txt", "2025-06-10", "minutes", None)], http=FakeHttp({"https://x/m.txt": MINUTES}),
                 jev=FakeJev({"Acme": 0.95, "dump truck": 0.95, "grant": 0.5, "Tyler": 0.9, "May 1": 0.9}, default=0.9))

    def small(tier, body, note):
        if "Tyler" in body:
            return [{"event": False, "unsure": True}]
        if "grant" in body:
            return [{"event": False}]
        return plain_rule(tier, body, note)

    def big(tier, body, note):
        if "Tyler" in body:
            return [event("budget software subscription with Tyler Technologies", "purchase", 12000, "Tyler Technologies",
                          "budget software") | {"action": "to_consider"}]
        return plain_rule(tier, body, note)
    p, model, bigm = pipeline(cx, "haiku-jev", small, big, cost=0.01)
    answer = p.run()
    assert p.stats["escalated"] == 2 and p.stats["escalation_changed"] == 2
    assert [t for t, _ in bigm.prompts] == ["sonnet"] and len(cards_of(bigm.prompts[0][1])) == 2
    assert p.money["escalation_usd"] == pytest.approx(0.01)
    tyler = next(e for e in cx.st.nodes(cx.sid, "event", "recorded") if e["data"]["vendor"] == "Tyler Technologies")
    assert tyler["data"]["escalated"] and tyler["data"]["by"] == "fake:sonnet" and tyler["data"]["action"] == "to_consider"
    assert {e["what"] for e in answer["events"]} >= {"library services grant", "budget software"}


# ------------------------------------------------------------------------------------------- the store and the score
def write_truth(body, decisions, dates):
    f = truth.folder(body)
    (f / "truth.json").write_text(json.dumps({"decisions": decisions}))
    (f / "sample.json").write_text(json.dumps({"sampled": [{"date": d} for d in dates]}))


@pytest.mark.usefixtures("stand_in_items")
def test_events_are_scored_by_truth_score_and_every_miss_and_extra_has_a_reason():
    cx = session(run="r2", links=[("https://x/m.txt", "2025-06-10", "minutes", None),
                                  ("https://x/r.pdf", "2025-07-01", "item", "Financial report")],
                 http=FakeHttp({"https://x/m.txt": MINUTES}),
                 jev=FakeJev({"Acme": 0.95, "dump truck": 0.9, "grant": 0.95, "Tyler": 0.1, "May 1": 0.9}, default=0.1))

    def rule(tier, body, note):
        if "dump truck" in body:
            return [{"event": False}]
        if "May 1" in body:  # an extra: approving minutes is no purchase
            return [event("Approval of the minutes of the meeting of May 1", "purchase", None, None, "minutes of may meeting")]
        if "grant" in body:  # recorded, but with fields that match nothing
            return [event("Resolution 2025-14", "budget", 1.0, "Nobody", "zz")]
        return plain_rule(tier, body, note)
    p, _, _ = pipeline(cx, "haiku-jev", rule)
    p.arm = dataclasses.replace(ARMS["haiku-jev"], check=False)  # so the dump truck stays "not an event"
    p.run()
    t = lambda **k: {"type": "contract", "action": "approved", "vendor": "", "amount": None, "buyer": True, **k}  # noqa: E731
    write_truth("cortez-co", [
        t(date="2025-06-10", item="2b", what="Acme Paving street repair contract", vendor="Acme Paving", amount=125000,
          quote="Approval of a contract with Acme Paving Inc. for street repair"),
        t(date="2025-06-10", item="2b", what="street repair materials", vendor="Acme Paving", amount=5000,
          quote="Approval of a contract with Acme Paving Inc. for street repair"),  # a second decision in the same item
        t(date="2025-06-10", item="2c", type="purchase", what="dump truck purchase", vendor="Valley Ford", amount=98500,
          quote="Approval of the purchase of a dump truck from Valley Ford"),
        t(date="2025-06-10", item="3a", type="grant", what="State Library grant acceptance", vendor="State Library", amount=45000,
          quote="Resolution 2025-14 accepting a library services grant"),
        t(date="2025-06-10", item="9z", what="sewer lining contract", vendor="Pipe Co", amount=777000,
          quote="Approval of a sewer lining contract with Pipe Co"),
        t(date="2025-07-01", what="fleet fuel contract", vendor="Fuel Co", amount=5000, quote="a fleet fuel contract"),
        t(date="2025-08-01", what="audit services", vendor="Audit LLP", amount=30000, quote="audit services with Audit LLP"),
    ], ["2025-06-10", "2025-07-01", "2025-08-01"])
    ev = evaluate.evaluate("r2", "cortez-co", cx.sid)
    b = ev["breakdown"]
    assert ev["score"]["found"] == b["found"] == 1  # the same event nodes truth.score reads
    assert b["missed_because"] == {"model said not an event": 1, "recorded with wrong fields": 1, "item not found in the document": 1,
                                   "no document opened": 1, "meeting not on the map": 1,
                                   "item holds several decisions; fewer recorded": 1}
    assert b["extra_events"] == {"not a decision": 2}  # the minutes approval and the mis-recorded grant
    assert any("missed because" in line for line in evaluate.lines("cortez-co", ev))


def test_the_breakdown_is_for_the_development_bodies_only():
    with pytest.raises(SystemExit):
        evaluate.truth_of("some-heldout-body")
    with pytest.raises(SystemExit):
        cli.main(["replay", "--from-run", "x", "--arm", "jev-only", "--bodies", "some-heldout-body"])


# ------------------------------------------------------------------------------------------- the replay
def test_the_replay_never_fetches_a_document_missing_from_the_cache(tmp_path):
    http = replay.CacheOnlyHttp(tmp_path / "httpcache")
    r = http.get("https://x/m.pdf")
    assert not r.ok and "not in the HTTP cache" in r.error and http.misses == ["https://x/m.pdf"]
    cx = session(run="r3", links=[("https://x/m.pdf", "2025-06-10", "minutes", None)], http=http, jev=FakeJev({}))
    p, model, _ = pipeline(cx, "haiku-jev")
    answer = p.run()
    assert model.prompts == [] and answer["coverage"]["unreachable"] and not answer["complete"]
    with pytest.raises(tools.Stop):  # and a replay's context makes no paid call
        cx.providers


def test_the_replay_reads_documents_from_the_http_cache(tmp_path):
    w = web.Http(tmp_path / "httpcache")
    key = w._key("https://x/m.txt")
    key.with_suffix(".bin").write_bytes(MINUTES.encode())
    key.with_suffix(".json").write_text(json.dumps({"url": "https://x/m.txt", "final_url": "https://x/m.txt", "status": 200,
                                                    "content_type": "text/plain", "error": None, "elapsed_ms": 1,
                                                    "bot_wall": False, "fetched_at": "2026-11-05T00:00:00"}))
    http = replay.CacheOnlyHttp(tmp_path / "httpcache")
    assert http.get("https://x/m.txt").text == MINUTES and http.hits == 1 and not http.misses


def test_replay_jev_and_models_answer_from_caches_only_and_count_misses(tmp_path):
    jev = replay.ReplayJev(cache_path=tmp_path / "none.sqlite")
    cx = session(run="r4", links=[("https://x/m.txt", "2025-06-10", "minutes", None)], http=FakeHttp({"https://x/m.txt": MINUTES}),
                 jev=jev)
    responses = ResponseCache(tmp_path / "responses.sqlite", read_only=True)
    small = ModelDecider(CacheOnlyBackend(responses), "haiku")
    p = Pipeline(cx, ARMS["haiku-jev"], small, ModelDecider(CacheOnlyBackend(responses), "sonnet"), out=lambda *a: None)
    answer = p.run()
    assert jev.counts["miss"] > 0 and not any(k != "miss" for k in jev.counts)
    assert p.stats["model_calls_missed"] >= 1 and p.stats["model_calls_sent"] == 0 and not answer["events"]
    assert set(p.undecided) == {"no cached answer"}
    with pytest.raises(RuntimeError):
        responses.put("m", "p", "r", {})  # a replay never writes


def test_replay_jev_finds_a_label_by_exact_request_or_by_passage_text(tmp_path):
    from ddwb.deep.forks import BUYING_EVENT, PEOPLE, jev_questions
    from ddwb.providers import JEV, core
    from ddwb.providers import cost as cost_mod
    c = cost_mod.Cache(tmp_path / "cache.sqlite")
    model = replay.settings_mod.load().judge_model
    state = {"body": CARD, "document": {"kind": "minutes", "date": "2025-06-10"}, "passage": "b. Approval of a contract"}
    req = {"model": model, "state": state, "questions": core.check_questions(jev_questions(BUYING_EVENT | PEOPLE))}
    answers = {"is_event": {"type": "boolean", "probability": 0.9}}
    c.put(cost_mod.Cache.key(JEV, "judge", req), JEV, "judge", req, {"answers": answers}, None, {}, 0.0, "router:x")
    old = {"model": model, "state": {"body": CARD, "passage": "c.  Approval of a truck", "document": {}},
           "questions": jev_questions(BUYING_EVENT)}
    c.put(cost_mod.Cache.key(JEV, "judge", old), JEV, "judge", old, {"answers": {"is_event": {"type": "boolean", "probability": 0.7}}},
          None, {}, 0.0, "jev-calibration-B")
    c._conn().close()
    jev = replay.ReplayJev(cache_path=tmp_path / "cache.sqlite")
    got = jev.many(None, [("P1", state), ("P2", state | {"passage": "c. Approval of a truck"}), ("P3", state | {"passage": "z"})],
                   BUYING_EVENT | PEOPLE, "reading")
    assert got["P1"][0] == {"is_event": 0.9, "via": "exact"}
    assert got["P2"][0] == {"is_event": 0.7, "via": "text:jev-calibration-B"}
    assert got["P3"][0] is None and "not in the provider cache" in got["P3"][2]
    assert jev.counts == {"exact": 1, "text:jev-calibration-B": 1, "miss": 1}
    exact_only = replay.ReplayJev(cache_path=tmp_path / "cache.sqlite", match="exact")
    assert exact_only.many(None, [("P2", state | {"passage": "c. Approval of a truck"})], BUYING_EVENT, "reading")["P2"][0] is None


def test_the_map_is_copied_as_the_map_left_it(tmp_path, monkeypatch):
    src = State("srcrun")
    sid = "srcrun:opus:cortez-co"
    src.start(sid, run_id="srcrun", arm="opus", body="cortez-co", body_id="b1", query="q1", since="2025-01-01",
              started_at=nav_state.now(), card=CARD, homepage="https://x", status="answered")
    src.put(sid, "G0000001", "page", "mapped", url="https://x/list", data={"notes": []}, tool="map")
    src.put(sid, "Kaaaaaaa", "link", "listed", url="https://x/a.pdf", data={"date": "2025-03-01", "rule_kind": "minutes"}, tool="map")
    src.put(sid, "Kaaaaaaa", "link", "scored", jev={"relevant": 0.8}, tool="map:jev")
    src.put(sid, "Kaaaaaaa", "link", "read", data={"looked": True}, tool="read")  # the agent's: not copied
    src.put(sid, "Kbbbbbbb", "link", "listed", url="https://x/b.pdf", data={"date": "2025-03-01"}, tool="read")  # found by a read
    src.put(sid, "Mccccccc", "meeting", "listed", data={"date": "2025-03-01"}, tool="map")
    src.put(sid, "Pddddddd", "passage", "recorded", data={"text": "x"}, tool="record")
    st = State("dst")
    got = replay.copy_map(nav_state.NAV / "srcrun.sqlite", sid, st, "dst:pipe-jev-only:cortez-co", "dst", "pipe-jev-only")
    assert got == {"lists": 1, "views": 0, "meetings": 1, "links": 1}
    k = st.get("dst:pipe-jev-only:cortez-co", "Kaaaaaaa")
    assert k["status"] == "scored" and k["jev"] == {"relevant": 0.8} and "looked" not in k["data"]
    assert not st.nodes("dst:pipe-jev-only:cortez-co", "passage")
    with pytest.raises(SystemExit):  # a session is never replayed into twice
        replay.copy_map(nav_state.NAV / "srcrun.sqlite", sid, st, "dst:pipe-jev-only:cortez-co", "dst", "pipe-jev-only")


# ------------------------------------------------------------------------------------------- backends
def test_live_answers_are_cached_by_model_and_prompt_and_replayed_at_no_cost(tmp_path):
    calls = []

    def runner(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"result": '[{"item": 1, "event": false}]', "total_cost_usd": 0.002,
                                                               "usage": {"input_tokens": 900, "output_tokens": 20}}), "")
    cache = ResponseCache(tmp_path / "responses.sqlite")
    cc = ClaudeCodeBackend(cache, runner=runner)
    r = cc.ask("PROMPT", "haiku")
    assert r.text == '[{"item": 1, "event": false}]' and r.cost == 0.0 and r.api_usd == 0.002 and not r.cached
    cmd, kw = calls[0]
    assert cmd[:4] == ["claude", "-p", "--model", "haiku"] and "--tools" in cmd and kw["input"] == "PROMPT"
    assert cc.ask("PROMPT", "haiku").cached and len(calls) == 1  # the second time from the cache
    again = CacheOnlyBackend(ResponseCache(tmp_path / "responses.sqlite", read_only=True), "claude-code")
    assert again.ask("PROMPT", "haiku").text == r.text and again.ask("OTHER", "haiku").miss


def test_a_live_model_call_stops_at_the_model_cap(monkeypatch):
    cx = session(run="r5", links=[("https://x/m.txt", "2025-06-10", "minutes", None)], http=FakeHttp({"https://x/m.txt": MINUTES}),
                 jev=FakeJev({}, default=0.9))
    model = FakeModel(plain_rule)
    model.live = True
    p = Pipeline(cx, ARMS["haiku-jev"], ModelDecider(model, "haiku"), None, out=lambda *a: None)
    answer = p.run()  # no model cap was fixed for the run: $0, so nothing is sent
    assert model.prompts == [] and not answer["complete"] and "model cap" in answer["incomplete_because"][-1]


def test_a_replay_keeps_its_document_copies_beside_the_stores_not_in_the_work_root(tmp_path):
    from ddwb import paths
    w = web.Http(tmp_path / "httpcache")
    key = w._key("https://x/m.txt")
    key.with_suffix(".bin").write_bytes(MINUTES.encode())
    key.with_suffix(".json").write_text(json.dumps({"url": "https://x/m.txt", "final_url": "https://x/m.txt", "status": 200,
                                                    "content_type": "text/plain", "error": None, "elapsed_ms": 1,
                                                    "bot_wall": False, "fetched_at": "2026-11-05T00:00:00"}))
    cx = session(run="r6", links=[("https://x/m.txt", "2025-06-10", "minutes", None)],
                 http=replay.CacheOnlyHttp(tmp_path / "httpcache"), jev=FakeJev({}, default=0.9))
    p, _, _ = pipeline(cx, "haiku-jev")
    answer = p.run()
    assert answer["events"] and not paths.MANIFEST.exists() and not paths.DOCS.exists()
    assert list((nav_state.NAV.parent / "pipeline" / "replay-docs" / "cortez-co").glob("*.txt"))


def test_a_live_gateway_run_refuses_a_model_with_no_price(monkeypatch):
    from ddwb.providers import cost
    monkeypatch.setattr(cli, "load_settings", lambda: SimpleNamespace(prices={"models": {}}))
    monkeypatch.delitem(cost.PRICES["models"], "anthropic/claude-sonnet-5.5")  # a model the price table does not know
    with pytest.raises(SystemExit, match="no price for anthropic/claude-sonnet-5.5"):
        cli.main(["run", "--map-from", "src:opus:cortez-co", "--run", "x", "--arm", "haiku-jev", "--backend", "gateway",
                  "--cap-reading", "0.1", "--cap-model", "0.1", "--approved-by", "test"])
    with pytest.raises(SystemExit, match="approved-by"):
        cli.main(["run", "--map-from", "src:opus:cortez-co", "--run", "x", "--arm", "haiku-jev", "--backend", "claude-code",
                  "--cap-reading", "0.1", "--cap-model", "0.1", "--approved-by", " "])


def test_a_meeting_whose_minutes_cannot_be_read_is_read_from_its_agenda():
    http = FakeHttp({"https://x/ag.txt": MINUTES.replace("MINUTES", "AGENDA")})  # the minutes are not served
    cx = session(links=[("https://x/min.txt", "2025-06-10", "minutes", None), ("https://x/ag.txt", "2025-06-10", "agenda", None)],
                 http=http, jev=FakeJev({}, default=0.9))
    p, _, _ = pipeline(cx, "haiku-jev")
    answer = p.run()
    assert http.asked == ["https://x/min.txt", "https://x/ag.txt"]
    assert p.stats["dates_minutes_unreadable"] == 1 and p.stats["dates_read_from_agenda"] == 1
    assert answer["events"] and answer["coverage"]["unreachable"] == [nid("K", "https://x/min.txt")]


def test_the_structure_layer_plugs_in_by_one_line(monkeypatch):
    """`structure_items` maps ddwb.structure's Document (here a stand-in with its API) to the pipeline's items: one
    passage per item, its number kept, items it does not send to the buying question set aside and counted."""
    import sys
    from ddwb.pipeline import driver, items as items_mod
    seen = {}

    def structure(pages, *, kind=None, pdf_path=None, context=None):
        seen["kind"] = kind
        it = lambda number, title, text, procedural=None, kind="item", outcome=None, amounts=(): SimpleNamespace(  # noqa: E731
            number=number, section="2. CONSENT AGENDA" if number and number.startswith("2.") else None, title=title, text=text,
            pages=(1, 1), kind=kind, procedural=procedural, outcome=outcome, amounts=list(amounts), orgs=[], people=[], refs=[])
        vote = SimpleNamespace(text="Motion carried 5-0.", consent=True, from_item="2.c")
        all_items = [it("1", "Call to order", "1. Call to order at 7 pm.", procedural="call to order"),
                     it("2.b", "Acme contract", "b. Approval of a contract with Acme Paving Inc. for street repair, $125,000.",
                        outcome=vote, amounts=[125000.0]),
                     it("2.b", "Exhibit A", "Exhibit A: the Acme Paving Inc. contract terms", kind="attachment")]
        return SimpleNamespace(items=all_items, buying_items=lambda procedural_max=None: [all_items[1]])
    monkeypatch.setitem(sys.modules, "ddwb.structure", SimpleNamespace(structure=structure))
    monkeypatch.setattr(driver, "document_items", items_mod.structure_items)  # the SWITCH, as the merge will make it
    cx = session(links=[("https://x/m.txt", "2025-06-10", "minutes", None)], http=FakeHttp({"https://x/m.txt": "any text"}),
                 jev=FakeJev({}, default=0.9))
    p, model, _ = pipeline(cx, "haiku-jev")
    answer = p.run()
    assert seen["kind"] == "minutes" and p.stats["items"] == 3 and p.stats["items_procedural"] == 2
    assert "the consent vote under 2.c" in model.prompts[0][1] and len(cards_of(model.prompts[0][1])) == 1
    e = cx.st.nodes(cx.sid, "event", "recorded")[0]
    assert e["data"]["item_no"] == "2.b" and answer["events"][0]["amount"] == 125000
    aside = {n["data"]["procedural"] for n in cx.st.nodes(cx.sid, "passage", "skipped") if n["data"].get("procedural")}
    assert aside == {"call to order", "not sent (attachment)"}


def test_the_ledger_chain_counts_survivors_and_leaks_stage_by_stage():
    from ddwb.pipeline import ledger
    b = {"truth": 10, "found": 6, "type_right": 5, "action_right": 6,
         "missed_because": {"no document opened": 2, "set aside by Jev": 1, "left undecided (form)": 1}}
    ch = ledger.chain(b)
    assert [s for s, _, _ in ch] == list(ledger.STAGES)
    reached = {s: r for s, r, _ in ch}
    assert reached["on the map"] == 10 and reached["document opened"] == 8 and reached["sent to the model"] == 7
    assert reached["event recorded"] == 6 and reached["fields right"] == 5
    assert "biggest leak: document opened (2" in "\n".join(ledger.lines(ch, {"documents_read": 3, "items": 9, "items_by_verdict": {},
        "model_calls": 2, "model_calls_cached": 1, "model_calls_by_outcome": {}, "events": 6, "models": {}, "cost_usd_by_budget": {}}))


def test_ci_flags_a_drop_past_tolerance_a_missing_body_and_cache_misses():
    from ddwb.pipeline.__main__ import ci_compare
    base = {"a": {"recall": 0.80, "precision": 0.90}, "b": {"recall": 0.50, "precision": 0.70}, "c": {"recall": 0.6, "precision": 0.6}}
    cur = {"a": {"recall": 0.79, "precision": 0.90, "cache_misses": 0}, "b": {"recall": 0.45, "precision": 0.70, "cache_misses": 3}}
    bad = ci_compare(cur, base, 0.02, 0.03)
    assert not any(x.startswith("a:") for x in bad)  # within tolerance
    assert any("b: recall" in x for x in bad) and any("cache misses" in x for x in bad) and "c: not scored" in bad


def test_the_policy_file_drives_the_thresholds_and_writes_least_privilege_agents(tmp_path):
    from ddwb import policy
    from ddwb.pipeline import driver
    d = policy.decide()
    assert driver.ESCALATE == tuple(d["escalate_band"]) and driver.AUDIT == policy.audit()
    assert {"set_aside_p", "check_p", "procedural_max", "gap_days"} <= set(policy.profile())
    files = policy.write_agents(tmp_path)
    reader = (tmp_path / "public-records-reader.md").read_text()
    assert len(files) == 3 and "model: haiku" in reader and "mcp__plugin_public-records_public-records__record" in reader
    assert "public-records__fetch" not in reader  # a reader cannot browse: only its stage's tools
    assert "Never: solving or getting around a CAPTCHA" in reader


def test_the_default_policy_escalates_only_what_the_model_is_unsure_of():
    from ddwb import policy
    from ddwb.pipeline import driver
    assert policy.decide()["escalate_band"] == [] and not driver.ESCALATE  # replayed 2026-11-06: the band bought no recall


# ------------------------------------------------------------------------------------------- per-body tuning (decision #126)
def audited(tier, body, note):
    """The stronger reader finds the grant; the small model never sees it."""
    if "grant" in body:
        return [event("accepting a library services grant", "grant", 45000, "State Library", "library services grant")]
    return plain_rule(tier, body, note)


def test_the_margin_audit_finds_a_decision_under_the_cut_and_lowers_this_bodys_cut():
    cx = session(arm="haiku-jev-audit", links=[("https://x/m.txt", "2025-06-10", "minutes", None)],
                 http=FakeHttp({"https://x/m.txt": MINUTES}),
                 jev=FakeJev({"Acme": 0.95, "dump truck": 0.9, "grant": 0.17, "Tyler": 0.9}, default=0.02))
    cx.profiled = True
    p, model, _ = pipeline(cx, "haiku-jev-audit")
    p.auditor = ModelDecider(FakeModel(audited), "sonnet")
    p.run()
    assert "library services grant" not in "\n".join(pr for _, pr in model.prompts)  # set aside at 0.17 ≤ 0.2
    assert p.stats["audit_items_read"] == 1 and p.stats["audit_decisions_found"] == 1  # 0.02 items: below every band
    assert any(e["data"].get("type") == "grant" for e in cx.st.nodes(cx.sid, "event", "recorded"))
    assert profile.value(cx, "set_aside_p") == 0.16
    assert profile.log(cx)[-1]["source"] == "audit"  # the band below (0.11 to 0.16) holds no item: the audit ends
    saved = json.loads(profile.path("cortez-co").read_text())
    assert saved["values"]["set_aside_p"] == 0.16  # the body's next session starts from it


def test_a_fixed_arm_reads_the_defaults_and_moves_nothing():
    cx = session(arm="haiku-jev-solo", links=[("https://x/m.txt", "2025-06-10", "minutes", None)],
                 http=FakeHttp({"https://x/m.txt": MINUTES}), jev=FakeJev({"Acme": 0.95, "grant": 0.17}, default=0.02))
    assert not cx.profiled and profile.move(cx, "set_aside_p", 0.1, source="t", evidence=None, reason="r") is None
    p, _, _ = pipeline(cx, "haiku-jev-solo")
    p.run()
    assert profile.value(cx, "set_aside_p") == 0.2 and p.stats["audit_items_read"] == 0


def test_knobs_stay_in_bounds_and_the_gap_guard_follows_the_boards_rhythm():
    cx = session(arm="haiku-jev-audit")
    cx.profiled = True
    assert profile.move(cx, "set_aside_p", 0.9, source="t", evidence=None, reason="r") == 0.35
    assert profile.cadence(cx, ["2025-01-10", "2025-02-14"]) is None  # too few meetings to say
    monthly = [f"2025-{m:02d}-1{d}" for m in range(1, 13) for d in (0,)]  # twelve monthly meetings
    assert profile.cadence(cx, monthly) == 62  # twice its usual longest interval (31 days)


def test_a_nav_session_starts_from_the_bodys_carried_profile(tmp_path):
    first = session(run="r1", arm="haiku-jev-audit")
    first.profiled = True
    profile.move(first, "procedural_max", 900, source="t", evidence=None, reason="r")
    later = session(run="r2", arm="haiku-jev-audit")
    later.profiled, later.carry = True, True
    assert profile.value(later, "procedural_max") == 900 and profile.log(later)[0]["source"] == "carried"


def test_an_agent_moves_a_knob_only_with_evidence_it_read():
    cx = session(arm="haiku-jev-audit", links=[("https://x/m.txt", "2025-06-10", "minutes", None)])
    cx.profiled = True
    with pytest.raises(tools.Stop, match="not a node"):
        tools.t_profile(cx, argparse.Namespace(action="set", knob="set_aside_p", value=0.1, evidence="P0000000", reason="r"))
    k = nid("K", "https://x/m.txt")
    tools.t_profile(cx, argparse.Namespace(action="set", knob="set_aside_p", value=0.1, evidence=k, reason="a grant at 0.12"))
    assert profile.value(cx, "set_aside_p") == 0.1 and profile.log(cx)[-1]["source"] == "agent"
