"""Which adapters exist, and which portal a URL or page belongs to.

Each adapter module registers itself here with `register(...)` when imported; `ADAPTERS` lists the modules to import.
`detect` asks every adapter, so a body's own site that embeds or links a portal is recognised too, not only the
registry's portal links.
"""

from __future__ import annotations

import importlib

from .base import Fetch, Portal

ADAPTERS: tuple[str, ...] = (  # module names under ddwb.portals, e.g. "legistar"; each calls register() on import
    "agendaonline",
    "boardbook",
    "boarddocs",
    "boardontrack",
    "civicclerk",
    "civicplus_agendacenter",
    "civicweb",
    "escribe",
    "gdrive",
    "granicus",
    "iqm2",
    "legistar",
    "novusagenda",
    "primegov",
    "simbli",
    "streamline",
)
_FACTORIES: dict[str, type] = {}


def register(cls: type) -> type:
    """Class decorator: an adapter class with `name` and `transport`, constructed with a Fetch (http) or nothing."""
    _FACTORIES[cls.name] = cls
    return cls


def _load() -> None:
    for module in ADAPTERS:
        importlib.import_module(f"{__package__}.{module}")


def names() -> list[str]:
    _load()
    return sorted(_FACTORIES)


def own_browser(name: str) -> bool:
    """The adapter reads through a browser of its own (Simbli), or moves to one itself when plain HTTP is walled
    (BoardDocs: `fetch_for`). The others are handed ddwb.portals.browser.Fallback by nav."""
    _load()
    cls = _FACTORIES[name]
    return cls.transport == "browser" or callable(getattr(cls, "fetch_for", None))


def adapter(name: str, http: Fetch | None = None) -> Portal:
    _load()
    cls = _FACTORIES.get(name)
    if cls is None:
        raise KeyError(f"no adapter for {name!r} (known: {', '.join(sorted(_FACTORIES)) or 'none yet'})")
    return cls(http) if cls.transport == "http" else cls()


def detect(url: str, html: str | None = None, http: Fetch | None = None) -> list[tuple[str, str]]:
    """Every (portal, client id) that recognises the URL or the page: usually one, sometimes none."""
    _load()
    found = []
    for name, cls in sorted(_FACTORIES.items()):
        client = (cls(http) if cls.transport == "http" else cls()).detect(url, html)
        if client:
            found.append((name, client))
    return found
