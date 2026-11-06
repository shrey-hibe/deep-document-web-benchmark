"""Names, kinds and queries for the registry resolver.

The normaliser builds on labels.websites (its `_tokens` and `name_key`) and adds what lookups need:
- EPA's inverted government names put back in order: 'CORTEZ CITY OF' -> 'CITY OF CORTEZ', 'AMSTERDAM (C)' -> 'CITY OF
  AMSTERDAM'; 'COUNTY OF SUMMIT' and 'Summit County' already share a key through name_key ('SUMMIT|COUNTY').
- More abbreviations: USD, ISD, CISD, CSD, UFSD, CUSD, CCSD (school districts), SD after the state is read off a query,
  BORO, VLG, AUTH, MUNI, RWD, WD and others (ABBREVIATIONS below), on top of the ones labels.websites expands (TWP, FPD...).
- The core of a name: its words without designators (CITY, COUNTY, DISTRICT, SCHOOL, LIBRARY, WATER...), for matching
  'Placentia-Yorba Linda Unified School District' to 'PLACENTIA-YORBA LINDA UNIFIED'.
- Families: the kind of body a name or a record is (county, municipality, township, school, library, fire, water,
  transit, housing, ...), from the record's entity_type and Census function, or from the words of a query.

VERSION changes whenever a rule here changes the precomputed name keys (registry/name_keys in a release).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..labels import websites as w

VERSION = "r1"

ABBREVIATIONS = {
    "USD": "UNIFIED SCHOOL DISTRICT", "ISD": "INDEPENDENT SCHOOL DISTRICT", "CISD": "CONSOLIDATED INDEPENDENT SCHOOL DISTRICT",
    "CSD": "CENTRAL SCHOOL DISTRICT", "UFSD": "UNION FREE SCHOOL DISTRICT", "CUSD": "COMMUNITY UNIT SCHOOL DISTRICT",
    "CCSD": "COMMUNITY CONSOLIDATED SCHOOL DISTRICT", "SCH": "SCHOOL", "SCHS": "SCHOOLS", "ELEM": "ELEMENTARY",
    "BORO": "BOROUGH", "VLG": "VILLAGE", "TWP": "TOWNSHIP", "TWSP": "TOWNSHIP", "TWN": "TOWN", "CNTY": "COUNTY", "DIST": "DISTRICT", "AUTH": "AUTHORITY", "MUNI": "MUNICIPAL",
    "MUNIC": "MUNICIPAL", "MUN": "MUNICIPAL", "WTR": "WATER", "JT": "JOINT", "JNT": "JOINT", "RWD": "RURAL WATER DISTRICT",
    "PWD": "PUBLIC WATER DISTRICT", "WD": "WATER DISTRICT", "SWCD": "SOIL AND WATER CONSERVATION DISTRICT", "LIB": "LIBRARY",
    "HSG": "HOUSING", "PKS": "PARKS", "REC": "RECREATION",
}  # not SAN (San Diego) or CO (Colorado, company): labels.websites expands CO only before a district word
QUERY_ONLY = {"SD": "SCHOOL DISTRICT"}  # in a query, after the state is read off ('Springfield SD 186, IL')
# Census short forms that depend on the next word: 'HOUSTON CO SCH DIST', 'HOUSTON IND SCH DIST', 'X CONS IND SCH DIST'
BEFORE = {"CO": ("COUNTY", {"SCHOOL", "SCHOOLS", "SCH", "SCHS", "BOARD", "COMMISSION", "COMMISSIONERS", "SD", "USD", "ISD",
                            "EDUCATION", "UNIFIED", "CONSOLIDATED", "CONS", "COMMUNITY", "CENTRAL", "HIGH", "ELEMENTARY", "ELEM"}),
          "IND": ("INDEPENDENT", {"SCHOOL", "SCH"}), "CONS": ("CONSOLIDATED", {"IND", "INDEPENDENT", "SCHOOL", "SCH", "HIGH"})}

GENERAL_KINDS = ("CITY", "TOWN", "VILLAGE", "BOROUGH", "TOWNSHIP", "COUNTY", "PARISH")
INVERTED = re.compile(r"^(?P<rest>.+?)[\s,]+(?P<kind>CITY|TOWN|VILLAGE|BOROUGH|BORO|TOWNSHIP|TWP|COUNTY|PARISH)\s+OF$")
NY_MARK = re.compile(r"^(?P<rest>.+?)\s*\((?P<kind>C|V|T)\)$")  # EPA New York: 'AMSTERDAM (C)', 'MOHAWK (V)'
NY_KIND = {"C": "CITY", "V": "VILLAGE", "T": "TOWN"}
GENERAL_PREFIX = re.compile(r"^(CITY AND COUNTY|CONSOLIDATED CITY|CHARTER TOWNSHIP|INCORPORATED VILLAGE|CITY|TOWN|VILLAGE|BOROUGH|"
                            r"TOWNSHIP|COUNTY|PARISH) OF ")

# Words that say what kind of body it is, not which one: left out of a name's core.
DESIGNATORS = {
    "CITY", "TOWN", "VILLAGE", "BOROUGH", "TOWNSHIP", "COUNTY", "PARISH", "OF", "AND", "GOVERNMENT", "MUNICIPALITY",
    "INCORPORATED", "CHARTER", "DISTRICT", "AUTHORITY", "COMMISSION", "BOARD", "SPECIAL", "PUBLIC", "DEPARTMENT", "DEPT",
    "SERVICE", "SERVICES", "SCHOOL", "SCHOOLS", "UNIFIED", "INDEPENDENT", "CENTRAL", "COMMUNITY", "UNION", "FREE", "UNIT",
    "ELEMENTARY", "EDUCATION", "JOINT", "CONSOLIDATED", "LIBRARY", "LIBRARIES", "FIRE", "PROTECTION", "RESCUE", "VOLUNTEER",
    "EMERGENCY", "WATER", "SYSTEM", "SUPPLY", "WORKS", "UTILITY", "UTILITIES", "MUNICIPAL", "CORPORATION", "CORP", "COMPANY",
    "ASSOCIATION", "ASSN", "TRANSIT", "TRANSPORTATION", "MASS", "HOUSING", "SEWER", "SEWERAGE", "SANITARY", "SANITATION",
    "WASTEWATER", "HOSPITAL", "AIRPORT", "CEMETERY", "PARKS", "RECREATION", "PENSION", "RETIREMENT", "FUND",
}

# Families: what kind of body. General-purpose governments first.
GENERAL = frozenset({"county", "municipality", "township", "city", "town", "village", "borough"})
ENTITY_FAMILY = {
    "county": "county", "municipality": "municipality", "town_or_township": "township", "school_district": "school",
    "dependent_school_system": "school", "library_system": "library", "fire_department": "fire", "water_system": "water",
    "transit_reporter": "transit", "public_pension_system": "pension", "special_district": "special", "state": "state",
}
FUNCTION_FAMILY = {  # Census special district functions
    "LOCAL FIRE PROTECTION": ("fire",), "WATER SUPPLY UTILITY": ("water",), "SEWERAGE AND WATER SUPPLY": ("water", "sewer"),
    "HOUSING AND COMMUNITY DEVELOPMENT": ("housing",), "SOIL AND WATER CONSERVATION": ("conservation",),
    "DRAINAGE": ("drainage",), "FLOOD CONTROL": ("drainage",), "SEWERAGE": ("sewer",), "CEMETERIES": ("cemetery",),
    "LIBRARIES": ("library",), "PARKS AND RECREATION": ("park",), "HIGHWAYS": ("highway",), "TOLL HIGHWAYS": ("highway",),
    "IRRIGATION": ("water",), "HEALTH": ("hospital",), "HOSPITALS": ("hospital",), "SOLID WASTE MANAGEMENT": ("waste",),
    "AIR TRANSPORTATION": ("airport",), "PUBLIC MASS TRANSIT UTILITY": ("transit",), "NATURAL RESOURCES AND WATER SUPPLY": ("water",),
    "EDUCATION": ("school",), "ELECTRIC POWER UTILITY": ("electric",), "SEA AND INLAND PORT FACILITIES": ("port",),
    "FIRE PROTECTION AND WATER SUPPLY": ("fire", "water"), "GAS SUPPLY UTILITY": ("gas",), "POLICE PROTECTION": ("police",),
    "PARKING FACILITIES": ("parking",), "INDUSTRIAL DEVELOPMENT": ("development",), "MORTGAGE CREDIT": ("housing",),
}
KEY_KIND_FAMILY = {"CITY": "city", "TOWN": "town", "VILLAGE": "village", "BOROUGH": "borough", "TOWNSHIP": "township", "COUNTY": "county"}

# A strong word names the kind wherever it appears; a weak word only when a district word follows it.
STRONG = {
    "SCHOOL": "school", "SCHOOLS": "school", "UNIFIED": "school", "EDUCATION": "school", "ELEMENTARY": "school", "ACADEMY": "school",
    "LIBRARY": "library", "LIBRARIES": "library", "TRANSIT": "transit", "HOUSING": "housing", "SEWER": "sewer", "SEWERAGE": "sewer",
    "SANITARY": "sewer", "SANITATION": "sewer", "WASTEWATER": "sewer", "HOSPITAL": "hospital", "AIRPORT": "airport",
    "CEMETERY": "cemetery", "CEMETERIES": "cemetery", "PENSION": "pension", "RETIREMENT": "pension", "RECREATION": "park",
    "PARKS": "park", "IRRIGATION": "water", "CONSERVATION": "conservation", "DRAINAGE": "drainage", "LEVEE": "drainage",
}
WEAK = {"WATER": "water", "FIRE": "fire", "PARK": "park", "PORT": "port", "HEALTH": "hospital", "TRANSPORTATION": "transit",
        "UTILITY": "water", "RESCUE": "fire", "EMERGENCY": "fire"}
FOLLOWERS = {"DISTRICT", "AUTHORITY", "SYSTEM", "DEPARTMENT", "COMMISSION", "BOARD", "CORPORATION", "COMPANY", "WORKS", "SUPPLY",
             "PROTECTION", "SERVICES", "SERVICE", "UTILITY", "UTILITIES", "ASSOCIATION", "COOPERATIVE", "AND", "RESCUE", "AGENCY"}
ENDS = {"WATER", "FIRE"}  # weak words that also name the kind at the end of a name ('Springfield Water')
SPECIAL_WORDS = {"DISTRICT", "AUTHORITY", "COMMISSION"}
GENERAL_WORDS = {"COUNTY": "county", "PARISH": "county", "CITY": "city", "TOWN": "town", "VILLAGE": "village", "BOROUGH": "borough",
                 "TOWNSHIP": "township"}
ENTITY_TYPE_WORDS = {**ENTITY_FAMILY, "special district": "special", "school district": "school", "general purpose": "general",
                     "general": "general"}  # a `kind=` argument may name an entity_type


def plain(name: str) -> str:
    """Upper case, EPA's inverted government names put back in order, abbreviations expanded (words joined by spaces)."""
    text = re.sub(r"\s+", " ", name.upper().replace("&", " AND ")).strip(" .,;")
    m = INVERTED.match(text)
    if m:
        text = f"{m['kind']} OF {m['rest']}"
    m = NY_MARK.match(text)
    if m:
        text = f"{NY_KIND[m['kind']]} OF {m['rest']}"
    words = re.sub(r"[^A-Z0-9]+", " ", text.replace("'", "")).split()
    out = []
    for i, word in enumerate(words):
        if word in BEFORE and i + 1 < len(words) and words[i + 1] in BEFORE[word][1]:
            word = BEFORE[word][0]
        out.append(ABBREVIATIONS.get(word, word))
    return " ".join(out)


def split_key(key: str | None) -> tuple[str, str] | None:
    if not key:
        return None
    text, _, kind = key.partition("|")
    return text, kind


def families_of_words(tokens: list[str], *, general: bool = True) -> set[str]:
    """The kinds a run of words names: specific kinds first; general words count only when no other kind is named."""
    found: set[str] = set()
    for i, word in enumerate(tokens):
        nxt = tokens[i + 1] if i + 1 < len(tokens) else None
        if word in STRONG:
            found.add(STRONG[word])
        elif word in WEAK and (nxt in FOLLOWERS or (nxt is None and word in ENDS and i > 0) or (word == "PORT" and nxt == "OF")):
            found.add(WEAK[word])
    if not found and SPECIAL_WORDS & set(tokens):
        found.add("special")
    if not found and general:
        found |= {GENERAL_WORDS[t] for t in tokens if t in GENERAL_WORDS}
    return found


@dataclass(frozen=True)
class NameRow:
    """What the resolver needs from one record's name (one row of registry/name_keys)."""
    key: str | None  # 'TEXT|KIND' (labels.websites.name_key of the plain name)
    tokens: tuple[str, ...]
    core: tuple[str, ...]
    families: tuple[str, ...]


def record_row(name: str | None, state: str | None, entity_type: str | None, function: str | None) -> NameRow:
    words = plain(name or "")
    family = ENTITY_FAMILY.get(entity_type or "", "school" if (entity_type or "").startswith("k12_") else None)
    general = family in GENERAL or bool(GENERAL_PREFIX.match(words))
    key = w.name_key(words, state, general) if words else None
    tokens = w._tokens(words)
    core = [t for t in tokens if t not in DESIGNATORS]
    families = {family} if family else set()
    families |= set(FUNCTION_FAMILY.get((function or "").upper(), ()))
    if family in GENERAL:
        kind = (split_key(key) or ("", ""))[1]
        if kind in KEY_KIND_FAMILY:
            families.add(KEY_KIND_FAMILY[kind])
    else:
        families |= families_of_words(tokens, general=False)
    return NameRow(key, tuple(tokens), tuple(dict.fromkeys(core)), tuple(sorted(families)))


def county_key(county: str | None) -> str | None:
    """'Summit County' / 'SUMMIT' / 'St. Louis Parish' -> 'SUMMIT' / 'ST LOUIS'."""
    if not county:
        return None
    words = w._tokens(county)
    while words and words[-1] in {"COUNTY", "PARISH", "BOROUGH", "CENSUS", "AREA", "MUNICIPALITY"} and len(words) > 1:
        words = words[:-1]
    if words[:2] == ["COUNTY", "OF"]:
        words = words[2:]
    return " ".join(words) or None


# ------------------------------------------------------------------------------------------- queries
@dataclass(frozen=True)
class Query:
    text: str | None
    name: str
    state: str | None
    county: str | None  # county_key form
    families: frozenset[str]
    keys: frozenset[tuple[str, str]]  # (text, kind) from name_key, general and not
    tokens: tuple[str, ...]
    core: tuple[str, ...]
    notes: tuple[str, ...] = field(default=())

    def as_dict(self) -> dict:
        return {"name": self.name, "state": self.state, "county": self.county, "kinds": sorted(self.families),
                "keys": sorted(f"{t}|{k}" for t, k in self.keys), "core": list(self.core), "notes": list(self.notes)}


def state_code(text: str | None, states: dict[str, str]) -> str | None:
    """'OH', 'oh', 'Ohio', 'OHIO' -> 'OH' (states: postal -> upper-case name)."""
    if not text:
        return None
    t = re.sub(r"[^A-Z ]+", " ", text.upper()).strip()
    t = re.sub(r"\s+", " ", t)
    if t in states:
        return t
    for postal, name in states.items():
        if name == t:
            return postal
    return None


def _trailing_state(part: str, states: dict[str, str]) -> tuple[str, str | None]:
    """'Springfield IL' -> ('Springfield', 'IL'); 'City of Cortez Colorado' -> ('City of Cortez', 'CO').

    A trailing two-letter code counts unless it is 'Co'/'co' after a name (county); something must be left over.
    """
    raw = part.strip()
    words = raw.replace(".", " ").split()
    names = sorted(((n.split(), p) for p, n in states.items()), key=lambda x: -len(x[0]))
    upper = [x.upper() for x in words]
    for name_words, postal in names:
        n = len(name_words)
        if len(upper) > n and upper[-n:] == name_words:
            return " ".join(words[:-n]), postal
    if len(words) > 1:
        last = words[-1]
        if last.upper() in states and not (last.upper() == "CO" and last != "CO"):
            return " ".join(words[:-1]), last.upper()
    return raw, None


COUNTY_PART = re.compile(r"^(?:(?P<a>.+?)\s+(?:county|parish|co)|county of\s+(?P<b>.+))$", re.I)
IN_COUNTY = re.compile(r"^(?P<name>.+?)\s+in\s+(?P<county>.+?)\s+(?:county|parish)$", re.I)


def parse(text: str | None = None, *, name: str | None = None, state: str | None = None, kind: str | None = None,
          county: str | None = None, states: dict[str, str] | None = None) -> Query:
    """Read a free-text query (and keyword overrides) into a name, a state, a county and the kinds it names.

    'Summit County, OH' -> name 'Summit County', state OH, kinds {county}
    'Springfield, Sangamon County, IL' -> name 'Springfield', county SANGAMON, state IL
    'Pueblo City-County Library District' -> kinds {library} (a specific kind outranks the general words in a name)
    """
    states = states if states is not None else dict(w.STATE_NAMES)
    notes: list[str] = []
    parsed_name, parsed_state, parsed_county = None, None, None
    if text:
        parts = [p.strip() for p in re.split(r"[,;]", text) if p.strip()]
        if len(parts) > 1 and state_code(parts[-1], states):
            parsed_state = state_code(parts.pop(), states)
        if parts and not parsed_state:
            rest, parsed_state = _trailing_state(parts[-1], states)
            if parsed_state:
                parts[-1] = rest
        extra = []
        for p in parts[1:]:
            m = COUNTY_PART.match(p)
            if m and not parsed_county:
                parsed_county = m["a"] or m["b"]
            else:
                extra.append(p)
        first = parts[0] if parts else ""
        m = IN_COUNTY.match(first)
        if m and not parsed_county:
            first, parsed_county = m["name"], m["county"]
        words = first.split()  # 'Summit Co, OH' or 'Summit Co.': the county
        if len(words) > 1 and words[-1].rstrip(".").upper() == "CO" and (parsed_state or words[-1] != "CO"):
            first = " ".join([*words[:-1], "County"])
        parsed_name = " ".join([first, *extra]).strip()
    final_name = (name or parsed_name or "").strip()
    final_state = state_code(state, states) if state else parsed_state
    if state and not final_state:
        notes.append(f"state {state!r} not recognised")
    final_county = county_key(county or parsed_county)

    words = plain(final_name)
    words = " ".join(QUERY_ONLY.get(t, t) for t in words.split())
    tokens = w._tokens(words)
    if kind:
        kind_tokens = w._tokens(plain(kind))
        families = {ENTITY_TYPE_WORDS[kind.strip().lower()]} if kind.strip().lower() in ENTITY_TYPE_WORDS else set()
        if kind.strip().lower().startswith("k12"):
            families.add("school")
        families |= families_of_words(kind_tokens)
        if "general" in families:
            families = (families - {"general"}) | {"county", "municipality", "township"}
        if not families:
            notes.append(f"kind {kind!r} not recognised")
    else:
        families = families_of_words(tokens)
    keys = {split_key(w.name_key(words, final_state, g)) for g in (True, False)} - {None}
    core = tuple(dict.fromkeys(t for t in tokens if t not in DESIGNATORS))
    return Query(text, final_name, final_state, final_county, frozenset(families), frozenset(keys), tuple(tokens), core, tuple(notes))
