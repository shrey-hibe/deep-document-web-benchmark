"""The search pick rule: readable pages only, this body or its government, and a government's page must name the body."""

from ddwb.evals.run import names_body, pick

TEXT = "x" * 300


def _v(whose, p):
    return {"whose_site": {"choice": whose}, "official_site": {"probability": p}}


def test_names_body_ignores_generic_words():
    assert names_body("TOWNSHIP OF COTTONWOOD", "Cottonwood township board minutes")
    assert not names_body("TOWNSHIP OF COTTONWOOD", "City of Powers Lake, North Dakota")
    assert names_body("Fire Department", "anything")  # only generic words: nothing to check


def test_government_page_must_name_the_body():
    card = {"name": "TOWNSHIP OF COTTONWOOD"}
    pages = {"a": {"status": 200, "title": "City of Powers Lake", "text": TEXT},
             "b": {"status": 200, "title": "Burke County townships: Cottonwood", "text": TEXT}}
    verdicts = {"a": _v("government_running_it", 0.9), "b": _v("government_running_it", 0.4)}
    assert pick(card, ["a", "b"], pages, verdicts, name_check=True)[0] == "b"
    assert pick(card, ["a", "b"], pages, verdicts)[0] == "a"  # off by default since the fresh sample


def test_unreadable_pages_and_other_bodies_are_never_picked():
    card = {"name": "Holland Fire Department"}
    pages = {"wall": {"status": 403, "bot_wall": True, "text": TEXT}, "other": {"status": 200, "text": TEXT}}
    verdicts = {"wall": _v("this_body", 0.99), "other": _v("other_public_body", 0.9)}
    assert pick(card, ["wall", "other"], pages, verdicts)[0] is None
