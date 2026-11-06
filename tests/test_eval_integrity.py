"""Every logged output maps to the right input: answers to calls, calls to items, the state Jev saw to the item's card,
search results to their call, picks to that item's candidates. Reads the eval part files directly (all runs)."""

import json

import duckdb
import pytest

from ddwb import paths

EVAL = paths.TABLES / "eval"


def _view(con, table: str) -> str:
    """The table's rows from finished runs only (a run's latest status row decides; runs still going are left out)."""
    files = sorted((EVAL / table).glob("*.parquet"))
    runs = sorted((EVAL / "runs").glob("*.parquet"))
    if not files or not runs:
        pytest.skip(f"no eval.{table} parts yet")
    finished = f"""(SELECT run_id FROM read_parquet({[str(f) for f in runs]!r}, union_by_name = true)
                    QUALIFY row_number() OVER (PARTITION BY run_id ORDER BY written_at DESC) = 1 AND status = 'finished')"""
    return f"(SELECT * FROM read_parquet({[str(f) for f in files]!r}, union_by_name = true) WHERE run_id IN {finished})"


@pytest.fixture(scope="module")
def con():
    if not EVAL.exists():
        pytest.skip("no eval runs yet")
    return duckdb.connect()


def test_call_ids_are_unique_and_answers_are_unique_per_question(con):
    assert con.execute(f"SELECT count(*) - count(DISTINCT (run_id, call_id)) FROM {_view(con, 'jev_calls')}").fetchone()[0] == 0
    assert con.execute(f"SELECT count(*) - count(DISTINCT (run_id, call_id, question)) FROM {_view(con, 'jev_answers')}").fetchone()[0] == 0


def test_every_answer_belongs_to_a_call_of_the_same_item(con):
    assert con.execute(f"""SELECT count(*) FROM {_view(con, 'jev_answers')} a LEFT JOIN {_view(con, 'jev_calls')} c
                           USING (run_id, call_id) WHERE c.call_id IS NULL OR c.item_id <> a.item_id""").fetchone()[0] == 0


def test_every_call_belongs_to_a_logged_item(con):
    """Wide-tier items are in eval.items; the deep chain's fork items (Phase 2) in deep.decisions, one row per decider."""
    deep = sorted((paths.TABLES / "deep" / "decisions").glob("*.parquet"))
    forks = (f"(SELECT DISTINCT run_id, item_id FROM read_parquet({[str(f) for f in deep]!r}, union_by_name = true) WHERE arm = 'jev')"
             if deep else "(SELECT NULL::VARCHAR AS run_id, NULL::VARCHAR AS item_id WHERE false)")
    assert con.execute(f"""SELECT count(*) FROM {_view(con, 'jev_calls')} c LEFT JOIN {_view(con, 'items')} i USING (run_id, item_id)
                           LEFT JOIN {forks} f USING (run_id, item_id)
                           WHERE i.item_id IS NULL AND f.item_id IS NULL""").fetchone()[0] == 0


def test_the_state_jev_saw_is_the_items_card(con):
    rows = con.execute(f"""SELECT c.variant, c.state, i.card FROM {_view(con, 'jev_calls')} c JOIN {_view(con, 'items')} i
                           USING (run_id, item_id) WHERE c.variant IN ('W1', 'R', 'triage', 'pick')""").fetchall()
    assert rows
    for variant, state, card in rows:
        state, card = json.loads(state), json.loads(card)
        if variant == "pick":  # the body is the item's; the page is one candidate
            assert state["body"] == card["body"]
        else:
            assert state == card


def test_w1_judges_the_items_own_website(con):
    assert con.execute(f"""SELECT count(*) FROM {_view(con, 'jev_calls')} c JOIN {_view(con, 'decisions')} d USING (run_id, item_id)
                           WHERE c.variant = 'W1' AND c.candidate_url IS DISTINCT FROM d.website""").fetchone()[0] == 0


def test_search_results_belong_to_their_call_and_item(con):
    assert con.execute(f"""SELECT count(*) FROM {_view(con, 'search_results')} r LEFT JOIN {_view(con, 'search_calls')} s
                           USING (run_id, call_id) WHERE s.call_id IS NULL OR s.item_id <> r.item_id""").fetchone()[0] == 0


def test_every_judged_page_was_a_result_for_that_item(con):
    assert con.execute(f"""SELECT count(*) FROM {_view(con, 'jev_calls')} c
                           WHERE c.variant = 'pick' AND NOT EXISTS (SELECT 1 FROM {_view(con, 'search_results')} r
                           WHERE r.run_id = c.run_id AND r.item_id = c.item_id AND r.url = c.candidate_url)""").fetchone()[0] == 0


def test_every_pick_was_one_of_that_providers_results_for_the_item(con):
    assert con.execute(f"""SELECT count(*) FROM {_view(con, 'decisions')} d
                           WHERE d.website IS NOT NULL AND (ends_with(d.test, '_exa') OR ends_with(d.test, '_parallel'))
                             AND NOT EXISTS (SELECT 1 FROM {_view(con, 'search_results')} r
                           WHERE r.run_id = d.run_id AND r.item_id = d.item_id AND r.url = d.website
                             AND r.provider = split_part(d.test, '_', -1))""").fetchone()[0] == 0
