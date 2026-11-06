"""The Haiku pipeline: the navigator's reading policy in code, the model judging items only.

    driver.py     the order of work: per governing meeting, newest first, read by the plan, label, decide, check,
                  escalate, record; coverage by code
    items.py      a document's items (the shape ddwb.structure will give; a thin adapter over the navigator's split for now)
    form.py       the fixed decision form and its checks
    backends.py   model backends (gateway, Claude Code headless, replay), the response cache, the deciders
    replay.py     the $0 replay: a stored map copied, HTTP, Jev and model answers from caches only
    evaluate.py   the score (`truth.score`) and the miss breakdown

Commands: `python -m ddwb.pipeline replay|run|score` (see __main__.py).
"""
