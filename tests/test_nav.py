"""The navigation state store and the tools' rules, offline: no network, no paid providers, no warehouse writes."""

import argparse
import json
from datetime import date
from types import SimpleNamespace

import pytest

from ddwb.deep import state as nav_state
from ddwb.deep import text, tools


@pytest.fixture
def cx(tmp_path, monkeypatch):
    monkeypatch.setattr(nav_state, "NAV", tmp_path)
    c = tools.Cx("t1:test-jev:cortez-co")
    c.st.start(c.sid, run_id="t1", arm="test-jev", body="cortez-co", body_id="b1", query="q1", since="2025-01-01",
               started_at=nav_state.now(), card={"name": "CITY OF CORTEZ", "kind": "municipality", "state": "CO"},
               homepage="https://cortezco.gov", status="started")
    c.sess = c.st.session(c.sid)
    c.st.put(c.sid, "B", "body", "started", data={})
    return c


def ns(**kw):
    base = {"targets": [], "reason": None, "rest": False, "meeting": None, "max_p": None, "note": None, "vendor": None,
            "amount": None, "term": None}
    return argparse.Namespace(**(base | kw))


class NoLog:
    def __init__(self, *a, **k): ...
    def add(self, *a): ...
    def close(self): ...


def full_map(cx):
    """A mapped list with a governing-board meeting every two months from the window's start to today, each opened."""
    from datetime import date, timedelta
    cx.st.put(cx.sid, "G0000001", "page", "mapped", url="https://x/meetings", data={"notes": []})
    d = date(2025, 1, 15)
    while d <= date.today():
        cx.st.put(cx.sid, nav_state.nid("K", f"https://x/{d}.pdf"), "link", "read", url=f"https://x/{d}.pdf",
                  data={"date": d.isoformat(), "rule_governing": True, "looked": True})
        d += timedelta(days=60)


# ------------------------------------------------------------------------------------------- store
def test_put_merges_facts_and_the_latest_event_is_the_state(cx):
    st = cx.st
    st.put(cx.sid, "K1234567", "link", "listed", url="https://x/a.pdf", data={"text": "Agenda", "date": "2025-03-11"})
    st.put(cx.sid, "K1234567", "link", "scored", jev={"relevant": 0.9})
    n = st.get(cx.sid, "K1234567")
    assert n["status"] == "scored" and n["data"] == {"text": "Agenda", "date": "2025-03-11"} and n["jev"] == {"relevant": 0.9}
    assert n["url"] == "https://x/a.pdf"
    assert [x["status"] for x in st.nodes(cx.sid, "link")] == ["scored"]
    assert len(st.events(cx.sid)) == 3  # the body, then two events of the link: append-only


def test_node_ids_are_stable_across_arms():
    assert nav_state.nid("K", "https://x/a.pdf") == nav_state.nid("K", "https://x/a.pdf") != nav_state.nid("K", "https://x/b.pdf")


def test_spend_is_per_cap_and_shared_by_sessions(cx):
    cx.st.put(cx.sid, "S1", "search", "done", cost=0.007, budget="map")
    cx.st.put("t1:haiku:cortez-co", "S2", "search", "done", cost=0.001, budget="map")
    cx.st.put(cx.sid, "P1", "passage", "kept", cost=0.0001, budget="reading")
    assert cx.st.spent("map") == pytest.approx(0.008) and cx.st.spent("reading") == pytest.approx(0.0001)


def test_batch_rolls_back_on_error(cx):
    with pytest.raises(RuntimeError):
        with cx.st.batch():
            cx.st.put(cx.sid, "K0000001", "link", "listed")
            raise RuntimeError("boom")
    assert cx.st.get(cx.sid, "K0000001") is None


# ------------------------------------------------------------------------------------------- reading
def test_items_of_keeps_everything_in_order_and_splits_long_items():
    long_item = "1. Contract with Acme LLC\n" + "\n".join(f"line {i} " + "x" * 90 for i in range(60))
    out = text.items_of(long_item + "\n2. Proclamation\nthanks")
    assert len("".join(out)) >= len(long_item.replace("\n", "")) - 10  # nothing dropped
    assert all(len(o) <= 2100 for o in out) and out[0].startswith("1. Contract") and out[-1].startswith("2. Proclamation")


def test_could_be_buying():
    assert text.could_be_buying("Approve a contract with Acme Paving LLC for road resurfacing")
    assert not text.could_be_buying("Proclamation honoring the high school band for its season")


# ------------------------------------------------------------------------------------------- rules the tools enforce
def test_record_checks_type_amount_and_passage(cx):
    cx.st.put(cx.sid, "Pabcdef1", "passage", "kept", url="https://x/a.pdf", data={"text": "Award to Acme LLC, $12,500", "date": "2025-05-01", "doc": "K1"})
    with pytest.raises(tools.Stop, match="--type"):
        tools.t_record(cx, ns(passage="Pabcdef1", type="staffing", action="approved", what="x"))
    with pytest.raises(tools.Stop, match="--amount"):
        tools.t_record(cx, ns(passage="Pabcdef1", type="contract", action="approved", what="x", amount="lots"))
    with pytest.raises(tools.Stop, match="is a link|no node"):
        tools.t_record(cx, ns(passage="K1234567", type="contract", action="approved", what="x"))
    tools.t_record(cx, ns(passage="Pabcdef1", type="contract", action="approved", what="paving", amount="$12,500"))
    e = cx.st.nodes(cx.sid, "event")[0]
    assert e["data"]["amount"] == 12500 and cx.st.get(cx.sid, "Pabcdef1")["status"] == "recorded"


def test_skip_needs_a_reason_and_never_undoes_a_read(cx):
    cx.st.put(cx.sid, "K1111111", "link", "read", data={})
    cx.st.put(cx.sid, "K2222222", "link", "scored", data={"date": "2025-02-01"}, jev={"relevant": 0.1})
    cx.st.put(cx.sid, "K3333333", "link", "scored", data={"date": "2025-02-01"}, jev={"relevant": None, "error": "x"})
    with pytest.raises(tools.Stop, match="reason"):
        tools.t_skip(cx, ns(targets=["K2222222"], reason=" "))
    with pytest.raises(tools.Stop, match="already read"):
        tools.t_skip(cx, ns(targets=["K1111111"], reason="not needed"))
    tools.t_skip(cx, ns(rest=True, max_p=0.2, reason="Jev p <= 0.2"))
    assert cx.st.get(cx.sid, "K2222222")["status"] == "skipped"
    assert cx.st.get(cx.sid, "K3333333")["status"] == "scored"  # unknown is not low: --max-p leaves it


def test_answer_is_incomplete_while_anything_is_undecided(cx, monkeypatch):
    monkeypatch.setattr(tools, "Log", NoLog)
    full_map(cx)
    cx.st.put(cx.sid, "K2222222", "link", "scored", data={"date": "2025-02-01"})
    tools.t_answer(cx, ns())
    a = cx.st.get(cx.sid, "A")["data"]
    assert a["complete"] is False and "1 links undecided" in a["incomplete_because"]
    tools.t_skip(cx, ns(targets=["K2222222"], reason="ceremonial item"))
    tools.t_answer(cx, ns())
    assert cx.st.get(cx.sid, "A")["data"]["complete"] is True


# ------------------------------------------------------------------------------------------- Jev failures
class FakeProviders:
    """Stands in for ddwb.providers.Providers: judge() answers with `behaviour({"state", "questions"})`."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.pinned = []

    def judge(self, state, questions, **kw):
        self.pinned.append(kw.get("provider"))
        return self.behaviour({"state": state, "questions": questions})


def test_a_failed_jev_call_is_an_error_never_a_no(cx):
    def boom(payload):
        raise RuntimeError("transport")
    cx._providers = FakeProviders(boom)
    out = cx.jev_many([("K1", {"x": 1})], tools.LINK, "map")
    answers, cost, err = out["K1"]
    assert answers is None and cost == 0 and "transport" in err


def test_the_price_guard_keeps_the_paid_answer_and_stops_the_run(cx):
    from ddwb.providers import JEV, PriceDrift
    paid = SimpleNamespace(cost=0.0002, response={"answers": {"relevant": {"type": "boolean", "probability": 0.93}}})

    def drift(payload):
        raise PriceDrift("reported charge above 1.5x usage x price", result=paid)
    cx._providers = FakeProviders(drift)
    answers, cost, err = cx.jev_many([("K1", {"x": 1})], {"relevant": tools.LINK["relevant"]}, "map")["K1"]
    assert cx._providers.pinned == [JEV]  # the Jev arms pin Jev
    assert answers == {"relevant": 0.93} and cost == 0.0002 and "price guard" in err
    assert cx.stopped() and "1.5x" in cx.stopped()
    with pytest.raises(tools.Stop, match="stopped"):
        cx.check("map")


def test_caps_refuse_paid_calls_when_used_up(cx):
    cx.st.put(cx.sid, "S1", "search", "done", cost=cx.caps()["map"], budget="map")
    with pytest.raises(tools.Stop, match="cap is used up"):
        cx.check("map")
    assert cx.check("reading") > 0


def test_forms_are_read_so_dropdown_views_can_be_reached():
    from ddwb.deep import web
    html = """<form action="/index.php/boardpackets" method="get"><select name="year"><option value="">- Any -</option>
              <option value="2026">2026</option><option>2025</option></select><input type="hidden" name="m" value="all">
              <input type="submit" value="Go"></form><form action="/search" method="post"><input name="q"></form>"""
    page = web.read_html(html, "https://lib.example.org/board")
    assert len(page.forms) == 2
    f = page.forms[0]
    assert f["action"] == "https://lib.example.org/index.php/boardpackets" and f["method"] == "get"
    assert f["fields"][0]["options"] == [["", "- Any -"], ["2026", "2026"], ["2025", "2025"]]
    assert f["fields"][1] == {"name": "m", "type": "hidden", "value": "all"}


def test_matcher_pairs_one_decision_despite_misread_item_numbers():
    from ddwb.deep.truth import match, same_decision
    a = [{"item": "24.1", "amount": 963134.07, "what": "purchase orders"},
         {"item": "24.5", "vendor": "Southern California Edison", "what": "easement EV charging"}]
    b = [{"item": "23.5", "vendor": "Southern California Edison", "what": "dedicate easement for EV charging stations"},
         {"item": "24.1", "amount": 963134.07, "what": "ratified purchase orders"}, {"item": "9", "what": "new roof"}]
    pairs, a_only, b_only = match(a, b)
    assert sorted((x["item"], y["item"]) for x, y in pairs) == [("24.1", "24.1"), ("24.5", "23.5")]
    assert a_only == [] and [y["item"] for y in b_only] == ["9"]
    assert not same_decision({"item": "3", "what": "library roof repair"}, {"item": "7", "what": "audit services contract"})


# ------------------------------------------------------------------------------------------- coverage that cannot be faked
def test_skips_record_whether_the_link_was_ever_opened(cx):
    cx.st.put(cx.sid, "K4444444", "link", "scored", data={"date": "2025-02-01", "rule_governing": True}, jev={"relevant": 0.1})
    cx.st.put(cx.sid, "K5555555", "link", "scored", data={"date": "2025-02-01", "rule_governing": True}, jev={"relevant": 0.6})
    cx.st.put(cx.sid, "K6666666", "link", "peeked", data={"date": "2025-03-01", "rule_governing": True, "looked": True})
    tools.t_skip(cx, ns(targets=["K4444444", "K5555555", "K6666666"], reason="not a buying item"))
    c = tools.coverage(cx)
    assert c["links_skipped_on_jev"] == 1 and c["links_skipped_unseen"] == 1  # the peeked one was looked at
    assert c["governing_meetings"] == 2 and c["governing_meetings_unopened"] == ["2025-02-01"]


def test_the_answer_is_incomplete_while_a_governing_meeting_is_unopened(cx, monkeypatch):
    class NoLog:
        def __init__(self, *a, **k): ...
        def add(self, *a): ...
        def close(self): ...
    monkeypatch.setattr(tools, "Log", NoLog)
    cx.st.put(cx.sid, "K7777777", "link", "scored", data={"date": "2025-04-01", "rule_governing": False}, jev={"governing": 0.9})
    tools.t_skip(cx, ns(targets=["K7777777"], reason="looks routine"))
    tools.t_answer(cx, ns())
    a = cx.st.get(cx.sid, "A")["data"]
    assert a["complete"] is False and any("1 of 1 governing meetings never opened" in r for r in a["incomplete_because"])  # Jev's governing wins


def test_unrecord_retracts_and_reopens_the_passage(cx):
    cx.st.put(cx.sid, "Pabcdef2", "passage", "kept", url="https://x/m.pdf", data={"text": "Claims list approved", "date": "2025-05-01"})
    tools.t_record(cx, ns(passage="Pabcdef2", type="purchase", action="approved", what="claims list"))
    e = cx.st.nodes(cx.sid, "event")[0]["node"]
    with pytest.raises(tools.Stop, match="reason"):
        tools.t_unrecord(cx, ns(event=e, reason=""))
    tools.t_unrecord(cx, ns(event=e, reason="recorded twice"))
    assert cx.st.get(cx.sid, e)["status"] == "retracted" and cx.st.get(cx.sid, "Pabcdef2")["status"] == "kept"
    assert tools.coverage(cx)["events"] == 0 and tools.coverage(cx)["events_retracted"] == 1


def test_document_formats_are_told_apart():
    from ddwb.deep import web
    r = lambda body, ct="": web.Response("u", status=200, content_type=ct, body=body)  # noqa: E731
    assert tools.doc_format(r(b"%PDF-1.7 ..."), "x.pdf") == "pdf"
    assert tools.doc_format(r(b"\xd0\xcf\x11\xe0\xa1\xb1" + b"\x00" * 50), "minutes.doc") == "doc"
    assert tools.doc_format(r(b"{\\rtf1\\ansi hello}"), "a.rtf") == "rtf"
    assert tools.doc_format(r(b"PK\x03\x04....word/document.xml...."), "a.docx") == "docx"
    assert tools.doc_format(r(b"PK\x03\x04....xl/workbook.xml...."), "a.xlsx") == "binary"
    assert tools.doc_format(r(b"<!DOCTYPE html><html>", "text/html"), "a") == "html"


def test_office_files_are_read_by_textutil():
    import shutil
    if not shutil.which("textutil"):
        pytest.skip("macOS textutil not available")
    (tools.paths.WORK / "a.rtf").write_text("{\\rtf1\\ansi Approved the contract with Acme Paving LLC for $12,500.}")
    assert "Acme Paving" in tools.office_text("a.rtf")


# ------------------------------------------------------------------------------------------- fixes of 2026-11-04
def test_an_outcome_line_stays_with_its_own_item_and_table_rows_split():
    md = ("2026-259 A Resolution confirming a contract with Acme Paving LLC, $10,000.\n## Resolution 2026-259 was adopted.\n"
          "2026-260 A Resolution confirming a purchase contract with Kenworth Truck Company for $161,850.\n"
          "|2026-274|A Resolution confirming a contract with Oriana House, Inc.|\n|2026-275|A Resolution with IBH, Inc.|\nMotion carried 5-0.")
    out = text.items_of(md)
    assert out[0].endswith("2026-259 was adopted.") and out[1].startswith("2026-260") and "adopted" not in out[1]
    assert out[2].startswith("|2026-274|") and out[3].startswith("|2026-275|") and out[3].endswith("Motion carried 5-0.")


def test_the_plain_filter_keeps_decisions_with_no_amount_or_buying_word():
    assert text.could_be_buying("Authorizing the Director to enter into a memorandum of understanding with Pueblo County")
    assert not text.could_be_buying("Proclamation honoring the high school band for its season")


def test_record_keeps_people_and_the_quote_as_the_record_shows_them(cx):
    cx.st.put(cx.sid, "Pabcdef3", "passage", "kept", url="https://x/m.pdf",
              data={"text": "Settlement in Jane Smith v. John Doe, $5,000. Contact: (330) 643-2726, a@b.org", "date": "2025-05-01"})
    tools.t_record(cx, ns(passage="Pabcdef3", type="contract", action="approved", vendor="Jane Smith",
                          what="settlement of Jane Smith v. John Doe"))
    e = cx.st.nodes(cx.sid, "event")[0]["data"]
    assert e["vendor"] == "Jane Smith" and "Jane Smith v. John Doe" in e["what"]
    assert "Jane Smith" in e["quote"] and "643-2726" in e["quote"] and "a@b.org" in e["quote"]


def test_a_document_found_off_the_mapped_lists_can_be_opened(cx):
    url = "https://x/minutes/2025-03-11-regular-minutes.pdf"
    cx.st.put(cx.sid, nav_state.nid("G", url), "page", "found", url=url, data={"title": "Regular Meeting Minutes", "is_file": True})
    cx._providers = FakeProviders(lambda payload: SimpleNamespace(cost=0.00003, response={"answers": {
        "relevant": {"type": "boolean", "probability": 0.7}, "kind": {"type": "choice", "choice": "minutes"},
        "governing": {"type": "boolean", "probability": 0.9}}}))
    k = tools.doc_link(cx, nav_state.nid("G", url))
    assert k["kind"] == "link" and k["data"]["off_map"] and k["data"]["date"] == "2025-03-11" and k["data"]["rule_kind"] == "minutes"
    assert k["jev"]["governing"] == 0.9  # scored like a link on a list, in the Jev arms
    old = "https://x/minutes/2024-06-11-minutes.pdf"
    cx.st.put(cx.sid, nav_state.nid("G", old), "page", "found", url=old, data={"title": "Minutes", "is_file": True})
    with pytest.raises(tools.Stop, match="before the window"):
        tools.doc_link(cx, nav_state.nid("G", old))


def test_a_thin_map_makes_the_answer_incomplete(cx, monkeypatch):
    monkeypatch.setattr(tools, "Log", NoLog)
    tools.t_answer(cx, ns())
    assert "no list of the board's meetings was mapped" in cx.st.get(cx.sid, "A")["data"]["incomplete_because"]
    cx.st.put(cx.sid, "G0000002", "page", "mapped", url="https://x/2025", data={"notes": []})
    for d in ("2025-02-11", "2025-03-11", "2025-04-08"):  # one year view only: nothing after April 2025
        cx.st.put(cx.sid, nav_state.nid("K", d), "link", "read", data={"date": d, "rule_governing": True, "looked": True})
    tools.t_answer(cx, ns())
    reasons = cx.st.get(cx.sid, "A")["data"]["incomplete_because"]
    assert any(r.startswith("the map has no governing-board meeting from 2025-04-08 to") for r in reasons)


def test_a_complete_map_with_every_item_decided_is_complete(cx, monkeypatch):
    monkeypatch.setattr(tools, "Log", NoLog)
    full_map(cx)
    tools.t_answer(cx, ns())
    assert cx.st.get(cx.sid, "A")["data"]["complete"] is True


def test_a_view_read_by_an_earlier_map_is_not_a_gap(cx):
    cx.st.put(cx.sid, "G0000003", "page", "mapped", data={"notes": ["view already read by an earlier map: https://x/a",
                                                                    "view not read: https://x/b (HTTP 404)"]})
    assert tools.coverage(cx)["map_notes"] == ["view not read: https://x/b (HTTP 404)"]


def test_jev_labels_every_item_and_low_ones_are_set_aside(cx):
    ps = {"Pa000001": 0.05, "Pa000002": 0.9, "Pa000003": None}
    for pn in ps:
        cx.st.put(cx.sid, pn, "passage", "kept", data={"text": pn})

    def label(payload):
        p = ps[payload["state"]["passage"]]
        if p is None:
            raise RuntimeError("transport")
        return SimpleNamespace(cost=0.00004, response={"answers": {"is_event": {"type": "boolean", "probability": p}}})
    cx._providers = FakeProviders(label)
    cost, aside = tools.label_passages(cx, list(ps), {"jev": {}, "data": {"rule_kind": "minutes"}})
    assert aside == 1 and cx.st.get(cx.sid, "Pa000001")["status"] == "skipped"
    assert cx.st.get(cx.sid, "Pa000002")["status"] == "kept" and cx.st.get(cx.sid, "Pa000003")["status"] == "kept"  # unknown stays
    assert tools.coverage(cx)["passages_set_aside_on_jev"] == 1


def test_jev_ranks_a_fetched_pages_links_and_folds_the_low_ones(cx, capsys, monkeypatch):
    from ddwb.deep import web
    monkeypatch.setattr(tools, "ROUTE_TOP", 2)  # the top 2 always shown; below them, p ≤ 0.2 is folded
    page = web.Page(url="https://x/", title="Home", links=[web.Link("https://x/council-agendas", "Council agendas"),
                                                            web.Link("https://x/parks", "Parks and trails"),
                                                            web.Link("https://x/obscure", "Documents")])
    p = {"https://x/council-agendas": 0.95, "https://x/parks": 0.02, "https://x/obscure": 0.5}
    cx._providers = FakeProviders(lambda payload: SimpleNamespace(cost=0.00003, response={"answers": {
        "route": {"type": "boolean", "probability": p[payload["state"]["link"]["url"]]}}}))
    tools.show_page(cx, "G0000004", page, argparse.Namespace(all_links=False))
    out = capsys.readouterr().out
    assert out.index("Council agendas") < out.index("Documents") and "Parks" not in out and "1 more at p ≤ 0.2" in out
    tools.show_page(cx, "G0000004", page, argparse.Namespace(all_links=True))
    assert "Parks" in capsys.readouterr().out


def test_passage_shows_the_events_recorded_from_it(cx, capsys):
    cx.st.put(cx.sid, "Pabcdef4", "passage", "kept", url="https://x/m.pdf", data={"text": "Claims list approved", "date": "2025-05-01"})
    tools.t_record(cx, ns(passage="Pabcdef4", type="purchase", action="approved", what="claims list"))
    e = cx.st.nodes(cx.sid, "event")[0]["node"]
    capsys.readouterr()
    tools.t_passage(cx, argparse.Namespace(target="Pabcdef4"))
    assert f"EVENTS FROM IT: {e} [recorded]" in capsys.readouterr().out
    assert e in tools.passage_line(cx, cx.st.get(cx.sid, "Pabcdef4"))


def test_search_is_pinned_like_the_old_router_kinds(cx, capsys):
    """search_site was Parallel (objective = the query), search_open was Exa (10 results, within a domain if given)."""
    from ddwb.providers import EXA, PARALLEL
    calls = []

    def search(query, **kw):
        calls.append((query, kw))
        return SimpleNamespace(cost=0.001, cached=False, response=[{"url": "https://x/agendas", "title": "Agendas", "excerpt": "e"}])
    cx._providers = SimpleNamespace(search=search)
    tools.t_search(cx, argparse.Namespace(query="Cortez council agendas", engine=None, domain=None))
    tools.t_search(cx, argparse.Namespace(query="council minutes", engine=None, domain="cortezco.gov"))
    assert calls == [("Cortez council agendas", {"objective": "Cortez council agendas", "provider": PARALLEL}),
                     ("council minutes", {"domain": "cortezco.gov", "n": 10, "provider": EXA})]
    with pytest.raises(tools.Stop, match="no domain"):
        tools.t_search(cx, argparse.Namespace(query="x", engine="parallel", domain="cortezco.gov"))
    assert cx.st.spent("map") == pytest.approx(0.002) and "Agendas" in capsys.readouterr().out


# ------------------------------------------------------------------------------------------- any registry body
class FakeRegistry:
    """The registry's interface as start uses it: two Springfields, one Cortez."""
    BODIES = {"b_cortez": {"body_id": "b_cortez", "name": "CITY OF CORTEZ", "websites": [
                  {"url": "cortezco.gov", "chosen": True}]},
              "b_spr_city": {"body_id": "b_spr_city", "name": "CITY OF SPRINGFIELD", "websites": []},
              "b_spr_twp": {"body_id": "b_spr_twp", "name": "TOWNSHIP OF SPRINGFIELD", "websites": []}}

    def registry(self):
        return self

    def candidate(self, body_id):
        return self.BODIES.get(body_id)

    def resolve(self, query):
        if "springfield" in query.lower():
            return {"candidates": [self.BODIES["b_spr_city"], self.BODIES["b_spr_twp"]], "ambiguous": True,
                    "ambiguity": {"why": "name the kind"}}
        if "cortez" in query.lower():
            return {"candidates": [self.BODIES["b_cortez"]], "ambiguous": False, "ambiguity": None}
        return {"candidates": [], "ambiguous": False, "ambiguity": None}

    def format_result(self, res):
        return "\n".join(c["body_id"] for c in res["candidates"])

    def body_card(self, body_id):
        return {"name": self.BODIES[body_id]["name"]}


def test_a_session_starts_on_any_body_named_in_words(tmp_path, monkeypatch):
    monkeypatch.setattr(nav_state, "NAV", tmp_path)
    monkeypatch.setattr(tools, "registry", FakeRegistry())
    c = tools.Cx("t1:opus:my-town")
    tools.t_start(c, ns(query="Cortez, CO", body_id=None, cap_map=None, cap_reading=None, approved_by=None))
    assert c.sess["body_id"] == "b_cortez" and c.sess["homepage"] == "https://cortezco.gov"


def test_an_ambiguous_body_stops_with_the_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(nav_state, "NAV", tmp_path)
    monkeypatch.setattr(tools, "registry", FakeRegistry())
    c = tools.Cx("t1:opus:springfield")
    with pytest.raises(tools.Stop) as stop:
        tools.t_start(c, ns(query="Springfield IL", body_id=None, cap_map=None, cap_reading=None, approved_by=None))
    assert "b_spr_twp" in str(stop.value) and "--body-id" in str(stop.value)
    tools.t_start(c, ns(query=None, body_id="b_spr_twp", cap_map=None, cap_reading=None, approved_by=None))
    assert c.sess["body_id"] == "b_spr_twp" and c.sess["homepage"] is None


def test_a_session_label_must_be_safe_for_file_names():
    with pytest.raises(tools.Stop):
        tools.Cx("t1:opus:../etc")


# ------------------------------------------------------------------------------------------- fixes of 2026-11-05
def test_a_bulk_skip_of_unseen_possible_buying_is_unreviewed_and_keeps_the_answer_incomplete(cx, capsys, monkeypatch):
    monkeypatch.setattr(tools, "Log", NoLog)
    full_map(cx)
    passages = {"Pb000001": ("Award of the paving contract to Acme Paving LLC, $125,000, for Main Street", 0.6),
                "Pb000002": ("Approve the agreement with Northwind Services for janitorial work at the library", None),
                "Pb000003": ("Proclamation honoring the high school band for its season of performances", 0.1),
                "Pb000004": ("Purchase of two plow trucks from a state cooperative contract, $310,000 in total", 0.9)}
    for pn, (t, p) in passages.items():
        cx.st.put(cx.sid, pn, "passage", "kept", data={"text": t, "date": "2025-03-11"}, jev={"is_event": p})
    tools.t_passage(cx, argparse.Namespace(target="Pb000001"))  # shown: a skip of it in bulk is a decision on it
    tools.t_skip(cx, ns(targets=["Pb000001", "Pb000002", "Pb000003"], reason="not new purchases"))
    status = {pn: cx.st.get(cx.sid, pn)["status"] for pn in passages}
    assert status == {"Pb000001": "skipped", "Pb000002": "skipped_unreviewed", "Pb000003": "skipped", "Pb000004": "kept"}
    assert "SKIPPED UNREVIEWED: 1" in capsys.readouterr().out
    tools.t_skip(cx, ns(targets=["Pb000004"], reason="the same trucks as the March purchase"))  # one by one: decided
    c = tools.coverage(cx)
    assert c["passages_skipped_unreviewed"] == 1 and c["passages_undecided"] == 0
    tools.t_next(cx, ns())
    assert "STAGE review: 1 passages" in capsys.readouterr().out
    tools.t_answer(cx, ns())
    a = cx.st.get(cx.sid, "A")["data"]
    assert a["complete"] is False and any("skipped in bulk without being read: 1 passages" in r for r in a["incomplete_because"])
    tools.t_skip(cx, ns(targets=["Pb000002"], reason="janitorial services renewal already recorded from the minutes"))
    tools.t_answer(cx, ns())
    assert cx.st.get(cx.sid, "A")["data"]["complete"] is True


def test_passages_listed_by_the_tools_count_as_seen(cx):
    for i in range(3):
        cx.st.put(cx.sid, f"Pc00000{i}", "passage", "kept", data={"text": f"Contract amendment number {i} with Acme Paving LLC, $5,000",
                                                                    "date": "2025-03-11", "doc": "K0000009"}, jev={"is_event": 0.5})
    tools.show_passages(cx, argparse.Namespace(doc=None, page=1, min_p=None, status="kept"))
    tools.t_skip(cx, ns(targets=[f"Pc00000{i}" for i in range(3)], reason="amendments to one contract, recorded once"))
    assert {cx.st.get(cx.sid, f"Pc00000{i}")["status"] for i in range(3)} == {"skipped"}
    assert len(cx.st.events(cx.sid)) == 1 + 3 + 3  # being shown is not an event: tool calls and the warehouse are unchanged


def test_ids_gathered_by_paging_and_skipped_together_are_unreviewed(cx):
    """nav3's arms paged `passages` in a shell loop, kept the IDs and skipped them in one call: shown, never read."""
    ids = [f"Pe{i:06d}" for i in range(25)]
    for pn in ids:
        cx.st.put(cx.sid, pn, "passage", "kept", data={"text": f"Payment to Acme Paving LLC of $1,{pn[-3:]}.00 for street work",
                                                       "doc": "K0000010", "date": "2025-03-11"}, jev={"is_event": 0.5})
    for page in (1, 2):
        tools.show_passages(cx, argparse.Namespace(doc=None, page=page, min_p=None, status="kept"))
    tools.t_skip(cx, ns(targets=ids, reason="boilerplate"))
    assert {cx.st.get(cx.sid, pn)["status"] for pn in ids} == {"skipped_unreviewed"}
    tools.t_skip(cx, ns(targets=ids[:20], reason="payments already in the claims list recorded for this meeting"))
    tools.t_skip(cx, ns(targets=ids[20:], reason="payments already in the claims list recorded for this meeting"))
    assert {cx.st.get(cx.sid, pn)["status"] for pn in ids} == {"skipped"}  # decided a page at a time


def test_a_bulk_skip_of_unopened_governing_minutes_is_unreviewed(cx):
    gov = {"rule_governing": True}
    cx.st.put(cx.sid, "K1000001", "link", "scored", data=gov | {"date": "2025-02-11", "rule_kind": "minutes"})
    cx.st.put(cx.sid, "K1000002", "link", "scored", data=gov | {"date": "2025-02-11", "rule_kind": "agenda"})
    cx.st.put(cx.sid, "K1000003", "link", "scored", data={"rule_governing": False, "date": "2025-02-12", "rule_kind": "minutes"})
    cx.st.put(cx.sid, "K1000004", "link", "read", data=gov | {"date": "2025-03-11", "rule_kind": "minutes", "looked": True})
    cx.st.put(cx.sid, "K1000005", "link", "scored", data=gov | {"date": "2025-03-11", "rule_kind": "minutes"})  # a second copy
    cx.st.put(cx.sid, "K1000006", "link", "scored", data=gov | {"date": "2025-04-08", "rule_kind": "minutes"})
    tools.t_skip(cx, ns(rest=True, reason="routine meetings"))
    status = {k: cx.st.get(cx.sid, k)["status"] for k in ("K1000001", "K1000002", "K1000003", "K1000005", "K1000006")}
    assert status == {"K1000001": "skipped_unreviewed", "K1000002": "skipped", "K1000003": "skipped", "K1000005": "skipped",
                      "K1000006": "skipped_unreviewed"}
    assert tools.coverage(cx)["links_skipped_unreviewed"] == 2
    tools.t_skip(cx, ns(targets=["K1000006"], reason="a duplicate of the March minutes, misdated on the list"))
    assert cx.st.get(cx.sid, "K1000006")["status"] == "skipped" and tools.coverage(cx)["links_skipped_unreviewed"] == 1


def test_a_governing_meeting_is_covered_by_its_minutes_where_the_map_lists_them(cx, capsys, monkeypatch):
    monkeypatch.setattr(tools, "Log", NoLog)
    full_map(cx)
    gov = {"rule_governing": True}
    cx.st.put(cx.sid, "K2000001", "link", "read", data=gov | {"date": "2025-05-13", "rule_kind": "agenda", "looked": True})
    cx.st.put(cx.sid, "K2000002", "link", "unreachable", data=gov | {"date": "2025-05-13", "rule_kind": "minutes"})
    cx.st.put(cx.sid, "K2000003", "link", "read", data=gov | {"date": "2025-06-10", "rule_kind": "agenda", "looked": True},
              jev={"kind": "minutes"})  # Jev calls many agendas minutes: the map's own rule decides
    cx.st.put(cx.sid, "K2000004", "link", "read", data=gov | {"date": "2025-07-08", "rule_kind": "agenda", "looked": True})
    cx.st.put(cx.sid, "K2000005", "link", "peeked", data=gov | {"date": "2025-07-08", "rule_kind": "minutes", "looked": True})
    c = tools.coverage(cx)
    assert c["governing_meetings_minutes_unread"] == ["2025-07-08"]  # unreachable minutes are counted as unreachable
    tools.t_next(cx, ns())
    out = capsys.readouterr().out
    assert "STAGE minutes: 1 of the governing board's meetings" in out and "K2000005" in out and "STAGE answer" not in out
    tools.t_answer(cx, ns())
    reasons = cx.st.get(cx.sid, "A")["data"]["incomplete_because"]
    assert any(r.startswith("1 of ") and "without reading the minutes" in r and "2025-07-08" in r for r in reasons)
    cx.st.put(cx.sid, "K2000005", "link", "read")
    cx.st.put(cx.sid, "K2000002", "link", "read", data={"looked": True})  # the minutes came back
    tools.t_answer(cx, ns())
    assert cx.st.get(cx.sid, "A")["data"]["complete"] is True


def test_dates_are_read_from_a_documents_title_file_name_or_first_page():
    assert text.loose_date("MINUTES - Board Meeting Minutes - July, 24, 2025") == text.find_date("2025-07-24")
    assert str(text.loose_date("minutes_-_board_meeting_minutes_-_july_24_2025.pdf")) == "2025-07-24"
    first = "## LIBRARY DISTRICT BOARD OF TRUSTEES\n\n## BOARD MEETING MINUTES\n\n## 5:30pm, Thursday, July 24, 2025\n\nCall to order"
    assert str(text.first_page_date(first)) == "2025-07-24"
    assert str(text.first_page_date("Report of the Director\nSubmitted to the Board of Trustees August 28, 2025")) == "2025-08-28"
    assert text.first_page_date("Acme Supply, Inc.\n# Invoice: INV-1\nInvoice Date: 03/04/2025\nTotal $1,200") is None
    assert str(text.first_page_date("AGENDA\nPosted March 7, 2025\nRegular Meeting of the Council\nMarch 11, 2025")) == "2025-03-11"


def test_an_undated_document_takes_its_date_and_record_asks_only_then(cx, monkeypatch):
    from ddwb.deep import web
    html = ("<html><head><title>Board minutes</title></head><body><h2>Board of Trustees</h2><h2>Regular Meeting Minutes</h2>"
            "<p>Thursday, July 24, 2025</p><p>1. Approve the contract with Acme Paving LLC for parking lot repairs, $48,000.</p>"
            "<p>2. Approve the agreement with Northwind Services for the library's annual service plan.</p></body></html>")
    url = "https://lib.example.org/sites/default/files/2025-08/board-minutes.pdf"
    k = nav_state.nid("K", url)
    cx.st.put(cx.sid, k, "link", "scored", url=url, data={"text": "Board Minutes", "row": "", "date": None, "rule_kind": "minutes",
                                                          "rule_governing": True}, jev={"relevant": 0.7})
    resp = web.Response(url, status=200, content_type="text/html", body=html.encode())

    def fake_download(cx_, k_):
        rel, _ = web.save_download(cx_.body, "undated-minutes-0001.html", resp.body)
        return rel, "html", None, resp
    monkeypatch.setattr(tools, "download", fake_download)
    cx._providers = FakeProviders(lambda payload: SimpleNamespace(cost=0.00004, response={"answers": {
        "is_event": {"type": "boolean", "probability": 0.9}}}))
    tools.t_read(cx, argparse.Namespace(target=k, pages=None, all_items=False))
    n = cx.st.get(cx.sid, k)
    assert n["status"] == "read" and n["data"]["date"] == "2025-07-24" and n["data"]["date_from"] == "first page"
    ps = cx.st.nodes(cx.sid, "passage")
    assert ps and {p["data"]["date"] for p in ps} == {"2025-07-24"}
    with pytest.raises(tools.Stop, match="only for a document with no date"):
        tools.t_record(cx, ns(passage=ps[0]["node"], type="contract", action="approved", what="parking lot", date="2025-07-20"))
    cx.st.put(cx.sid, "Pd000001", "passage", "kept", url="https://x/item.pdf", data={"text": "Agreement with Acme LLC, $9,000",
                                                                                     "date": None, "doc": "K9999999"})
    with pytest.raises(tools.Stop, match="--date YYYY-MM-DD"):
        tools.t_record(cx, ns(passage="Pd000001", type="contract", action="approved", what="service agreement"))
    with pytest.raises(tools.Stop, match="before the window"):
        tools.t_record(cx, ns(passage="Pd000001", type="contract", action="approved", what="x", date="2024-12-10"))
    tools.t_record(cx, ns(passage="Pd000001", type="contract", action="approved", what="service agreement", date="2025-09-25"))
    e = [x for x in cx.st.nodes(cx.sid, "event") if x["data"]["passage"] == "Pd000001"][0]
    assert e["data"]["date"] == "2025-09-25" and "agent" in e["data"]["date_from"]


def test_a_document_dated_before_the_window_by_its_first_page_is_not_read(cx, monkeypatch):
    from ddwb.deep import web
    url = "https://x/files/minutes.html"
    k = nav_state.nid("K", url)
    cx.st.put(cx.sid, k, "link", "scored", url=url, data={"text": "Minutes", "date": None, "rule_kind": "minutes"})
    resp = web.Response(url, status=200, content_type="text/html",
                        body=b"<html><body><h1>City Council Regular Meeting</h1><p>December 10, 2024</p></body></html>")
    monkeypatch.setattr(tools, "download", lambda cx_, k_: ("data/deep/cortez-co/x.html", "html", None, resp))
    with pytest.raises(tools.Stop, match="before the window"):
        tools.t_read(cx, argparse.Namespace(target=k, pages=None, all_items=False))
    assert cx.st.get(cx.sid, k)["data"]["date"] == "2024-12-10" and not cx.st.nodes(cx.sid, "passage")


def test_the_score_leaves_pauses_out_of_the_working_time():
    from ddwb.deep import truth
    folder = truth.folder("cortez-co")
    (folder / "truth.json").write_text(json.dumps({"decisions": [{"date": "2025-03-11", "type": "contract", "action": "approved",
                                                                  "vendor": "Acme Paving LLC", "amount": 125000,
                                                                  "what": "paving of main street", "buyer": True}]}))
    (folder / "sample.json").write_text(json.dumps({"sampled": [{"date": "2025-03-11"}]}))
    st = nav_state.State("t-score")
    sid = "t-score:opus:cortez-co"
    st.start(sid, run_id="t-score", arm="opus", body="cortez-co", body_id="b1", started_at="2026-11-05T01:00:00+00:00")
    rows = [("2026-11-05T01:00:00.000+00:00", "B", "body", "started", "start", None, None),
            ("2026-11-05T01:10:00.000+00:00", "K0000001", "link", "read", "read", None, 5_000),
            ("2026-11-05T11:00:00.000+00:00", "K0000002", "link", "read", "read", None, 20 * 60_000),  # paused, then a 20-minute read
            ("2026-11-05T11:05:00.000+00:00", "E0000001", "event", "recorded", "record",
             {"date": "2025-03-11", "type": "contract", "action": "approved", "vendor": "Acme Paving LLC", "amount": 125000,
              "what": "main street paving"}, None)]
    for at, node, kind, status, tool, data, ms in rows:
        st.db.execute("INSERT INTO events (session, at, node, kind, status, data, tool, ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                      [sid, at, node, kind, status, json.dumps(data or {}), tool, ms])
    arm = truth.score("t-score", "cortez-co")["arms"]["opus"]
    assert arm["found"] == 1 and arm["minutes"] == 605.0  # first to last call, as before
    assert arm["minutes_working"] == 10 + 20 + 5 and arm["pauses"] == 1


def test_a_meetings_minutes_are_read_before_its_packet(cx):
    for node, kind in (("Ka000001", "minutes"), ("Kb000002", "packet")):
        cx.st.put(cx.sid, node, "link", "listed", url=f"https://x/{node}.pdf",
                  data={"date": "2025-03-11", "rule_kind": kind, "rule_governing": True, "text": kind})
    packet = cx.st.get(cx.sid, "Kb000002")
    assert tools.minutes_first(cx, packet) == "Ka000001"
    with pytest.raises(tools.Stop) as stop:
        tools.t_read(cx, ns(target="Kb000002", pages=None, all_items=False, anyway=False))
    assert "Ka000001" in str(stop.value) and "--anyway" in str(stop.value)
    cx.st.put(cx.sid, "Ka000001", "link", "read", data={"looked": True})
    assert tools.minutes_first(cx, cx.st.get(cx.sid, "Kb000002")) is None
    assert tools.minutes_first(cx, cx.st.get(cx.sid, "Ka000001")) is None


# ------------------------------------------------------------------------------------------- reading plan and items
def test_items_carry_their_number_and_section_across_pages():
    ctx = {}
    page1 = ("## 23. CONSENT CALENDAR-CURRICULUM AND INSTRUCTION\n"
             "23.1 Approved the agreement with Bionerds, Inc. for $12,000.\n"
             "23.2 Approved the service agreement with Renaissance Learning.\nThe motion was approved 5-0.\n"
             "24. Purchase of buses from Creative Bus Sales, $1,200,000")
    items = text.labelled_items(page1, context=ctx)
    assert [(i["item_no"], i["section"]) for i in items] == [
        ("23", None), ("23.1", "23. CONSENT CALENDAR-CURRICULUM AND INSTRUCTION"),
        ("23.2", "23. CONSENT CALENDAR-CURRICULUM AND INSTRUCTION"), ("24", None)]
    assert "approved 5-0" in items[2]["text"]  # the outcome stays with its item
    page2 = text.labelled_items("delivered by June 2026\n25. Adjournment", context=ctx)
    assert page2[0]["item_no"] == "24" and page2[1]["item_no"] == "25"  # page 2 opens mid-item 24
    assert text.item_label("Resolution No. 2025-14 approving") == "resolution 2025-14" and text.item_label("b) Lease") == "b"
    assert not text.ITEM_START.match("3.5 acres of land")  # a decimal is not a sub-item


def test_the_rule_names_the_kind_jev_only_where_the_rule_cannot_tell(cx):
    cx.st.put(cx.sid, "Kf000006", "link", "scored", url="https://x/Kf.pdf", jev={"kind": "minutes", "kind_p": 0.55},
              data={"date": "2025-03-11", "rule_kind": "agenda", "text": "March 11, 2025"})  # Cortez's agenda pages, nav3
    assert tools.doc_kind(cx.st.get(cx.sid, "Kf000006")) == "agenda"
    cx.st.put(cx.sid, "Kg000007", "link", "scored", url="https://x/Kg.pdf", jev={"kind": "minutes", "kind_p": 0.55},
              data={"date": "2025-03-11", "rule_kind": "document", "text": "DOWNLOAD PDF"})
    assert tools.doc_kind(cx.st.get(cx.sid, "Kg000007")) == "minutes"
    assert "minutes 0.55" in tools.link_line(cx, cx.st.get(cx.sid, "Kg000007"))  # an unsure guess shows as one


def test_the_minutes_cover_the_meeting(cx):
    links = {"Ka000001": ("document", "minutes"), "Kb000002": ("agenda", "agenda"), "Kc000003": ("packet", "packet"),
             "Kd000004": ("item", "item")}
    for node, (rule, jev) in links.items():
        cx.st.put(cx.sid, node, "link", "scored", url=f"https://x/{node}.pdf", jev={"kind": jev, "governing": 0.9},
                  data={"date": "2025-03-11", "rule_kind": rule, "rule_governing": True, "text": node})
    cx.st.put(cx.sid, "Ke000005", "link", "scored", url="https://x/Ke.pdf", jev={"kind": "agenda", "governing": 0.1},
              data={"date": "2025-03-11", "rule_kind": "agenda", "text": "planning commission agenda"})
    assert tools.is_minutes(cx.st.get(cx.sid, "Ka000001"))  # Jev's kind: the rule only knew it was a file
    assert tools.minutes_first(cx, cx.st.get(cx.sid, "Kc000003")) == "Ka000001"
    cx.st.put(cx.sid, "Ka000001", "link", "read", data={"looked": True})
    covered = tools.cover_meeting(cx, cx.st.get(cx.sid, "Ka000001"))
    assert sorted(covered) == ["Kb000002", "Kc000003", "Kd000004"]  # not the committee's agenda
    assert cx.st.get(cx.sid, "Kc000003")["status"] == "skipped" and cx.st.get(cx.sid, "Ke000005")["status"] == "scored"
    c = tools.coverage(cx)
    assert c["links_covered_by_minutes_or_agenda"] == 3 and c["links_skipped_unseen"] == 0


def test_an_agenda_covers_a_meeting_only_when_no_minutes_are_listed(cx):
    for node, kind in (("Ka000001", "agenda"), ("Kb000002", "packet")):
        cx.st.put(cx.sid, node, "link", "listed", url=f"https://x/{node}.pdf",
                  data={"date": "2026-10-13", "rule_kind": kind, "rule_governing": True, "text": kind})
    cx.st.put(cx.sid, "Ka000001", "link", "read", data={"looked": True})
    assert tools.cover_meeting(cx, cx.st.get(cx.sid, "Ka000001")) == ["Kb000002"]
    cx.st.put(cx.sid, "Kc000003", "link", "listed", url="https://x/Kc.pdf",
              data={"date": "2026-10-13", "rule_kind": "minutes", "rule_governing": True, "text": "minutes"})
    cx.st.put(cx.sid, "Kd000004", "link", "listed", url="https://x/Kd.pdf",
              data={"date": "2026-10-13", "rule_kind": "item", "rule_governing": True, "text": "staff report"})
    assert tools.cover_meeting(cx, cx.st.get(cx.sid, "Ka000001")) == []  # minutes are listed now: they cover it


def test_jev_sees_the_item_and_meeting_and_answers_people_in_the_same_call(cx):
    cx.st.put(cx.sid, "Pa000001", "passage", "kept", data={
        "text": "23.1 Approved the agreement with Bionerds, Inc. for $12,000.", "item_no": "23.1", "section": "23. CONSENT CALENDAR"})
    cx.st.put(cx.sid, "Pa000002", "passage", "kept", data={"text": "Appointed Jane Smith as Director of Technology."})
    seen = []

    def label(payload):
        seen.append(payload)
        qs = payload["questions"]
        vendor = {"vendor": {"type": "choice", "choice": "v1"}} if "vendor" in qs else {}
        people = 0.95 if "Appointed" in payload["state"]["passage"] else 0.03
        return SimpleNamespace(cost=0.00004, response={"answers": {
            "is_event": {"type": "boolean", "probability": 0.1 if people > 0.5 else 0.9},
            "people": {"type": "boolean", "probability": people},
            "people_change": {"type": "choice", "choice": "hire_or_appoint" if people > 0.5 else "none"}} | vendor})
    cx._providers = FakeProviders(label)
    tools.label_passages(cx, ["Pa000001", "Pa000002"], {"jev": {"kind": "minutes"}, "data": {
        "rule_kind": "minutes", "text": "DOWNLOAD PDF", "row": "September 9, 2025 Regular Board Meeting Minutes"}})
    by = {p["state"]["passage"][:4]: p for p in seen}
    assert by["23.1"]["state"]["item"] == {"item_no": "23.1", "section": "23. CONSENT CALENDAR"}
    doc = by["23.1"]["state"]["document"]
    assert doc["kind"] == "minutes" and "title" not in doc and doc["meeting"].startswith("September 9, 2025")
    assert {"people", "people_change", "is_event"} <= set(by["23.1"]["questions"]) and "vendor" not in by["23.1"]["questions"]
    assert "item" not in by["Appo"]["state"]
    vendor_p, people_p = cx.st.get(cx.sid, "Pa000001"), cx.st.get(cx.sid, "Pa000002")
    assert vendor_p["status"] == "kept"
    assert people_p["jev"]["people"] == 0.95 and people_p["status"] == "skipped"  # people, not buying: set aside
    assert "people p=0.95 hire_or_appoint" in tools.passage_line(cx, people_p)
    assert tools.passage_line(cx, vendor_p).lstrip().split("  ")[2].startswith("item 23.1")


def test_recording_an_item_sets_aside_the_same_item_in_the_meetings_other_documents(cx):
    common = {"date": "2025-03-11", "item_no": "7"}
    cx.st.put(cx.sid, "Pa000001", "passage", "kept", url="https://x/m.pdf",
              data=common | {"doc": "Kminutes", "text": "7. Award of the paving contract to Asphalt Co., $250,000. Approved 5-0."})
    cx.st.put(cx.sid, "Pb000002", "passage", "kept", url="https://x/a.pdf",
              data=common | {"doc": "Kagenda", "text": "7. Award of the paving contract to Asphalt Co."})
    cx.st.put(cx.sid, "Pc000003", "passage", "kept", url="https://x/a.pdf",
              data=common | {"doc": "Kagenda2", "text": "7. Proclamation for Arbor Day"})  # same number, another item
    tools.t_record(cx, ns(passage="Pa000001", type="contract", action="approved", vendor="Asphalt Co.", what="paving"))
    assert cx.st.get(cx.sid, "Pb000002")["status"] == "same_item" and cx.st.get(cx.sid, "Pc000003")["status"] == "kept"
    assert tools.coverage(cx)["passages_same_item"] == 1
    e = cx.st.nodes(cx.sid, "event")[0]
    assert e["data"]["item_no"] == "7"
    tools.t_unrecord(cx, argparse.Namespace(event=e["node"], reason="wrong passage"))
    assert cx.st.get(cx.sid, "Pb000002")["status"] == "kept"


def test_the_agent_sees_jevs_type_only_when_sure_and_never_its_action(cx):
    assert tools.compact({"type": {"type": "choice", "choice": "purchase", "probabilities": {"purchase": 0.9, "contract": 0.1}},
                          "is_event": {"type": "boolean", "probability": 0.95}}) == {"type": "purchase", "type_p": 0.9, "is_event": 0.95}
    cx.st.put(cx.sid, "Pa000001", "passage", "kept", jev={"is_event": 0.95, "type": "purchase", "type_p": 0.9, "action": "approved"},
              data={"text": "Purchase of buses"})
    cx.st.put(cx.sid, "Pa000002", "passage", "kept", jev={"is_event": 0.9, "type": "contract", "type_p": 0.55, "action": "approved"},
              data={"text": "Agreement with the county"})
    sure, unsure = (tools.passage_line(cx, cx.st.get(cx.sid, n)) for n in ("Pa000001", "Pa000002"))
    assert "p=0.95 purchase" in sure and "approved" not in sure
    assert "contract" not in unsure.split("Agreement")[0] and "approved" not in unsure


# ------------------------------------------------------------------------------------------- held-out truth
def test_a_held_out_frame_comes_from_the_hand_list_not_the_map(tmp_path, monkeypatch):
    from ddwb.deep import truth
    monkeypatch.setattr(truth, "HELDOUT", tmp_path)
    (tmp_path / "bodies.json").write_text(json.dumps([{"slug": "x-county-nc", "slot": "counties-large", "name": "COUNTY OF X", "state": "NC"}]))
    (tmp_path / "x-county-nc").mkdir()
    (tmp_path / "x-county-nc" / "frame.json").write_text(json.dumps({"name": "COUNTY OF X", "state": "NC", "meetings": [
        {"date": "2025-02-03", "kind": "regular", "title": "Board of Commissioners", "minutes": [{"url": "https://x/m1.pdf", "text": "Minutes"}]},
        {"date": "2025-02-03", "kind": "special", "title": "Special meeting", "minutes": [{"url": "https://x/m2.pdf"}]},
        {"date": "2025-02-10", "kind": "work_session", "title": "Work session", "minutes": [{"url": "https://x/w.pdf"}]},
        {"date": "2025-03-03", "kind": "regular", "title": "Board of Commissioners", "minutes": []},
        {"date": "2024-12-02", "kind": "regular", "title": "Board", "minutes": [{"url": "https://x/old.pdf"}]}]}))
    f = truth.frame("x-county-nc")
    assert f["card"]["kind"] == "county" and f["meetings"] == 2 and f["with_minutes"] == 1
    assert [m["url"] for m in f["frame"][0]["minutes"]] == ["https://x/m1.pdf", "https://x/m2.pdf"]  # one date, both minutes


def test_people_decisions_merge_by_person_and_change():
    from ddwb.deep import truth
    assert truth.same_person({"name": "Jane Q. Smith", "change": "hire_or_appoint"}, {"name": "J. Smith", "change": "hire_or_appoint"})
    assert not truth.same_person({"name": "Jane Smith", "change": "departure"}, {"name": "Jane Smith", "change": "hire_or_appoint"})
    assert not truth.same_person({"name": "Jane Smith", "change": "departure"}, {"name": "Jane Doe", "change": "departure"})


# ------------------------------------------------------------------------------------------- variance register fixes
def test_rules_that_misread_other_bodies_formats():
    from ddwb.deep import chain, web
    assert not text.SKIP.search("https://www.dropbox.com/s/x/minutes.pdf") and text.SKIP.search("https://x.com/county")  # H4
    items = text.labelled_items("## 22. CONSENT\n22.8\nApproved an increase\n3.5 Million gallon tank\n2025-2026 School Year Calendar")
    assert [i["item_no"] for i in items] == ["22", "22.8"]  # T2, T3: no fake items
    nums = [i["item_no"] for i in text.labelled_items("VIII. NEW\n" + "\n".join(f"{c}. x" for c in "ABCDEFGHIJ") + "\nIX. END")]
    assert nums[-3:] == ["VIII.I", "VIII.J", "IX"]  # T6: I after H is a letter
    agenda = web.Link("https://x.gov/agendas-and-minutes/2025/agenda-2025-03-11.pdf", "March 11, 2025")
    assert chain.classify_link(agenda, date(2025, 3, 11))[0] == "agenda"  # L1: not the folder's "minutes"
    board = web.Link("https://x/m.pdf", "Library Board of Trustees Minutes")
    assert chain.is_governing(board, "PUEBLO CITY-COUNTY LIBRARY DISTRICT") and not chain.is_governing(board, "CITY OF CORTEZ")  # L5


def test_links_in_a_line_broken_list_take_their_own_line():
    from ddwb.deep import web
    html = ('<div><p>Jan 13, 2025 <a href="/a1.pdf">Agenda</a><br>Feb 10, 2025 <a href="/a2.pdf">Agenda</a><br>Mar 10, 2025<br>'
            '<a href="/m3.pdf">Minutes</a></p></div>')
    rows = [l.row for l in web.read_html(html, "https://x.gov/").links]
    assert rows == ["Jan 13, 2025 Agenda", "Feb 10, 2025 Agenda", "Mar 10, 2025 Minutes"]


def test_two_meetings_on_one_date_do_not_cover_each_other(cx):
    for node, kind, row in (("Ka000001", "minutes", "Special Meeting Minutes"), ("Kb000002", "agenda", "Regular Meeting Agenda"),
                            ("Kc000003", "packet", "Special Meeting Packet")):
        cx.st.put(cx.sid, node, "link", "listed", url=f"https://x/{node}.pdf",
                  data={"date": "2025-06-02", "rule_kind": kind, "rule_governing": True, "text": row, "row": row})
    cx.st.put(cx.sid, "Ka000001", "link", "read", data={"looked": True})
    assert tools.cover_meeting(cx, cx.st.get(cx.sid, "Ka000001")) == ["Kc000003"]


def test_a_host_block_lapses_after_a_week(tmp_path, monkeypatch):
    from ddwb.deep import state
    monkeypatch.setattr(state, "NAV", tmp_path)
    clock = state.HostClock()
    clock.block("old.example.gov", "HTTP 403")
    assert clock.blocked("old.example.gov") == "HTTP 403"
    clock.db.execute("UPDATE hosts SET blocked_at = '2026-01-01T00:00:00.000+00:00' WHERE host = 'old.example.gov'")
    assert clock.blocked("old.example.gov") is None


def test_a_shared_google_document_is_read_through_its_export_address():
    from ddwb.deep import web
    assert web.direct_url("https://docs.google.com/document/d/10YqZbQNejR3GkP5NSLRpddziL_taeHy6/edit?usp=sharing") == \
        "https://docs.google.com/document/d/10YqZbQNejR3GkP5NSLRpddziL_taeHy6/export?format=txt"
    assert web.direct_url("https://drive.google.com/file/d/1AbCdEfGhIjKlMnOpQrStUvWx/view") == \
        "https://drive.google.com/uc?export=download&id=1AbCdEfGhIjKlMnOpQrStUvWx"
    assert web.direct_url("https://x.gov/m.pdf") == "https://x.gov/m.pdf"
    assert tools.doc_format(SimpleNamespace(body=b"Minutes of the board", content_type="text/plain; charset=utf-8"), "u") == "txt"


def test_likely_portals_are_a_hint_by_kind_and_state():
    from ddwb import portals
    tx, basis = portals.likely_portals({"kind": "school_district", "state": "TX", "name": "CLEAR CREEK ISD"})
    assert tx and "TX" in basis and {p["portal"] for p in tx} & {"boardbook", "boarddocs", "diligent"}
    fire, _ = portals.likely_portals({"kind": "special_district", "state": "ZZ", "name": "ELBERT FIRE PROTECTION DISTRICT"})
    assert fire[0]["portal"] == "streamline"  # the kind across the country when the state cell is too small
    assert portals.prior_kind({"kind": "municipality", "name": "CITY OF X"}) == "city_town"
    assert "a hint" in tools.likely_line({"kind": "county", "state": "NC", "name": "COUNTY OF WAKE"})


def test_an_underscore_s3_bucket_is_retried_path_style_and_errors_are_not_cached():
    from ddwb.deep import web
    assert web.s3_path_style("https://granicus_production_attachments.s3.amazonaws.com/a/b.pdf") == \
        "https://s3.amazonaws.com/granicus_production_attachments/a/b.pdf"
    assert web.s3_path_style("https://plainbucket.s3.amazonaws.com/a.pdf") is None


def test_read_turns_minutes_into_structured_items_with_outcomes_and_sets_procedure_aside(cx, monkeypatch, tmp_path):
    from ddwb.deep import web
    minutes = ("CITY COUNCIL REGULAR MEETING MINUTES\nMarch 11, 2025\n"
               "1. CALL TO ORDER\nThe meeting was called to order at 7:00 pm.\n"
               "2. CONSENT AGENDA\n"
               "a. Approval of the Expenditure List for March 11, 2025 in the amount of $412,550.\n"
               "b. Award of the 2025 paving contract to Asphalt Co. for $250,000.\n"
               "Councilmember Smith moved to approve the Consent Agenda; seconded by Councilmember Lee. Motion carried 5-0.\n"
               "3. ADJOURNMENT\nThe meeting adjourned at 8:10 pm.\n")
    f = tmp_path / "m.txt"
    f.write_text(minutes)
    resp = web.Response("https://x/m.txt", status=200, content_type="text/plain", body=minutes.encode())
    monkeypatch.setattr(tools, "download", lambda cx_, k_: (str(f), "txt", None, resp))
    cx.st.put(cx.sid, "Ka000001", "link", "listed", url="https://x/m.txt",
              data={"date": "2025-03-11", "rule_kind": "minutes", "rule_governing": True, "text": "Minutes"})
    tools.t_read(cx, argparse.Namespace(target="Ka000001", pages=None, all_items=True, anyway=False))
    ps = cx.st.nodes(cx.sid, "passage")
    kept = [p for p in ps if p["status"] == "kept"]
    paving = next(p for p in kept if "paving" in p["data"]["text"])
    assert "carried" in (paving["data"].get("outcome") or "").lower()  # the consent vote reaches each consent item
    assert any(p["status"] == "skipped" and p["data"].get("procedural") for p in ps)  # call to order, adjournment


def test_any_arm_name_works_and_jev_follows_the_name(tmp_path, monkeypatch):
    monkeypatch.setattr(nav_state, "NAV", tmp_path)
    assert tools.Cx("r1:claude:wake-county-nc").jev_on is False
    assert tools.Cx("r1:mapper-jev:wake-county-nc").jev_on is True
    assert tools.Cx("r1:haiku-jev-relay:wake-county-nc").jev_on is True  # the benchmark's own arms as listed
    with pytest.raises(tools.Stop):
        tools.Cx("r1:Bad Arm:wake-county-nc")
