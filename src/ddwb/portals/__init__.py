"""Meeting portals: where a public body is likely to publish its board's meetings.

`prior.json` is generated from the registry's accepted portal links (work/registry, 2026-11-05): for each kind of body,
and for each kind in each state where at least 15 bodies have a known portal, the portals they use and their shares.
It is a soft indication of where to look first, never an exclusive list: most bodies publish on their own sites.
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path

PRIOR = Path(__file__).with_name("prior.json")
SUBTYPES = (("fire", r"fire|emergency services|rescue"), ("water_sewer", r"water|sewer|sanitar|utility|irrigation|wastewater"),
            ("library", r"librar"), ("transit", r"transit|transport"), ("hospital", r"hospital"), ("health", r"health"),
            ("housing", r"housing"))


@cache
def _prior() -> dict:
    return json.loads(PRIOR.read_text())


def prior_kind(card: dict) -> str | None:
    """The prior's kind for a body card: county, city_town, township, school_district, charter_school, or
    special_district:<fire|water_sewer|library|transit|hospital|health|housing|other>."""
    kind, name = (card.get("kind") or "").lower(), (card.get("name") or "").lower()
    if "charter" in kind:
        return "charter_school"
    if kind.startswith("k12") or "school" in kind:
        return "school_district"
    if kind == "county":
        return "county"
    if kind == "township":
        return "township"
    if kind in ("municipality", "town", "city", "village", "city_town"):
        return "city_town"
    if "district" in kind or kind in ("special_district", "fire_department", "transit_agency", "library_system", "authority"):
        return "special_district:" + next((sub for sub, words in SUBTYPES if re.search(words, f"{kind} {name}")), "other")
    return None


def likely_portals(card: dict, top: int = 4) -> tuple[list[dict], str]:
    """The portals bodies like this one use, most common first, with where the shares come from. Kind and state when
    that cell is large enough, else the kind across the country. ([], "") when the prior knows nothing."""
    kind = prior_kind(card)
    if not kind:
        return [], ""
    p = _prior()
    cell = p["by_kind_and_state"].get(f"{kind}|{(card.get('state') or '').upper()}")
    if cell:
        return cell["portals"][:top], f"{kind.replace('_', ' ')} in {card['state'].upper()}, {cell['bodies_with_a_known_portal']} bodies with a known portal"
    cell = p["by_kind"].get(kind)
    if not cell:
        return [], ""
    return cell["portals"][:top], f"{kind.replace('_', ' ')} nationally, {cell['bodies_with_a_known_portal']} bodies with a known portal"
