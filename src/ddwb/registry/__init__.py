"""Find the body: stage 1 of the flow (notes/INTENT.md). Resolve a query to the registry's bodies, look bodies up by
ID or by host, give a body's card, and export the registry tables as a release.

    from ddwb import registry
    registry.resolve("Summit County, OH")          # {'candidates': [...], 'ambiguous': False, ...}
    registry.resolve("Springfield IL")             # ambiguous: city, township (water system next)
    registry.by_id("nces_leaid", "0630660")        # [candidate]
    registry.by_host("https://www.pylusd.org/")    # [candidate]
    registry.body_card("bf62a1a7cef59")            # the evals.run.body_card fields

Reads the latest Parquet snapshot of each table from data/tables, or from the folder DDWB_REGISTRY_DIR names (a
release made by export_release). Command line: uv run python -m ddwb.registry "Summit County, OH".
"""

from __future__ import annotations

import threading
from pathlib import Path

from . import names, snapshot
from .release import export_release
from .resolver import Registry, format_result

__all__ = ["Registry", "body_card", "by_host", "by_id", "export_release", "format_result", "names", "registry", "resolve"]

_REGISTRIES: dict[str, Registry] = {}
_LOCK = threading.Lock()


def registry(root: str | Path | None = None) -> Registry:
    """The Registry of a folder (default: DDWB_REGISTRY_DIR, else data/tables), opened once per process."""
    key = str(Path(root or snapshot.default_root()).resolve())
    with _LOCK:
        if key not in _REGISTRIES:
            _REGISTRIES[key] = Registry(key)
        return _REGISTRIES[key]


def resolve(query: str | None = None, *, name: str | None = None, state: str | None = None, kind: str | None = None,
            county: str | None = None, limit: int = 5) -> dict:
    """Ranked candidates for a free-text query ('Summit County, OH') or its parts; keyword arguments override what is
    read from the text. Returns {query, parsed, candidates, ambiguous, ambiguity, considered, snapshot, seconds}."""
    return registry().resolve(query, name=name, state=state, kind=kind, county=county, limit=limit)


def by_id(scheme: str | None, value: str) -> list[dict]:
    return registry().by_id(scheme, value)


def by_host(host: str) -> list[dict]:
    return registry().by_host(host)


def body_card(body_id: str) -> dict:
    return registry().body_card(body_id)
