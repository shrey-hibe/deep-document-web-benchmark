"""labels.website_matches: which record each outside website claim (standard.site_claims) belongs to.

Methods (each row says which, and the version):
- uei_exact: the claim's SAM UEI equals the UEI NTD publishes for the record.
- fscskey_exact: the claim's library ID (Oregon Library Directory, Wisconsin DPI, USAC E-Rate) equals the record's IMLS FSCS ID.
- fdid_exact: the claim's FDID (CAL FIRE) equals the record's USFA FDID, in the same state.
- place_fips_exact: the claim's Census place code (Delaware, Wikidata) equals the record's place FIPS code.
- county_fips_exact: the claim's county FIPS code (Wikidata) equals a county government's.
- nces_exact: the claim's NCES district ID (Wikidata) equals the record's NCES LEAID.
- pwsid_exact: the claim's PWSID (EPA mail domains) equals the record's; the mail-domain rule applies.
- name_place: the same state, a compatible kind of body, the same county where both sides give one, and the same
  name once normalised (below).

A match is accepted when it is one-to-one: the claim fits exactly one record of a kind (a Colorado or Texas district's
claim may be accepted for both the Census district and the EPA water system of that name), and the source offers that
record exactly one website (by host). Otherwise it is kept as 'ambiguous'. Claims for part of a body (a SAM division,
a .gov suborganization, a library branch) are not matched by name: they are departments' sites. An Illinois mail
domain counts only when it is not a webmail or internet-provider domain, at most two Illinois units use it, and it
ends in .gov or .us or contains a word of the unit's name.

name_place normalisation: upper case; '&' to AND; punctuation to spaces; agency abbreviations expanded (MUD, WCID,
FWSD, SUD, PUD, PSD, FPD, VFD, FD, ESD, WSC, DIST, TWP, CNTY; CO before a district word); SAINT, MOUNT and FORT
shortened; THE, INC, NO and NUMBER dropped; a trailing state name or postal code dropped (unless nothing else is
left). For cities, towns, townships and counties the kind word ('CITY OF', 'TOWNSHIP', 'COUNTY OF', ...) and words
like 'BOARD OF COMMISSIONERS' or 'TOWN HALL' are taken off, and the kind word must agree when both sides have one.

v7 (2026-10-31): adds EPA water systems' mail domains (by exact PWSID, under the same mail-domain rule as Illinois),
the MN and VA library lists (by name) and the Texas State Library directory (by FSCS ID).
v6 (2026-10-31): adds Wikidata, by exact ID only: places by FIPS place code, counties by FIPS county code
(county_fips_exact), school districts by NCES ID (nces_exact), libraries by FSCS ID.
v5 (2026-10-31): adds the state library lists of MA, NJ, VT, UT, WA and PA (by name).
v4 (2026-10-31): adds USAC E-Rate libraries (by FSCS ID, else by name) and IRS 990-N organisations (by name, the kind
read from the name); a claimed website must have the form of a web address.
v3 (2026-10-31): adds CAL FIRE (by FDID) and Wisconsin libraries (by FSCS ID).
v2 (2026-10-31): adds the county condition, the exact library and place methods, Colorado, New York, Oregon,
Delaware and Illinois claims, and the Illinois mail-domain rule.
"""

from __future__ import annotations

import re

import duckdb

from .. import catalogue, tables
from ..catalogue import Column
from ..standard import records, site_claims as sc

VERSION = "v7"

SPEC = catalogue.register(catalogue.Table(
    dataset="labels", name="website_matches", grain="claimed website judged to belong to a record (one claim, one record, one method)",
    description="Which record each outside website claim belongs to, by exact ID or by normalised name and place; "
                "accepted only when one-to-one (see ddwb/labels/websites.py)",
    columns=(
        Column("record_id", "the record the claim is judged to belong to", "label", "standard.records.record_id"),
        Column("claim_id", "the website claim", "label", "standard.site_claims.claim_id"),
        Column("website", "the claimed website, as published", "copy", "standard.site_claims.website"),
        Column("host", "the website's host, lower case, without 'www.'", "label", "standard.site_claims.website"),
        Column("method", "uei_exact, fscskey_exact, fdid_exact, place_fips_exact, county_fips_exact, nces_exact, pwsid_exact or name_place",
               "bookkeeping"),
        Column("method_version", "the version of the rules", "bookkeeping"),
        Column("status", "accepted (one-to-one) or ambiguous", "label"),
        Column("record_class", "the kind of body the record is, for matching: place, county, district, water, fire, library, "
               "transit, school", "label", "standard.records.entity_type"),
        Column("claim_class", "the kind of body the claim says it is, from the source's codes", "label",
               "standard.site_claims.kind_codes, naics_codes"),
        Column("match_key", "what was compared: the ID, or state | normalised name | kind word", "label"),
        Column("records_with_key", "how many records of the same kind share the claim's key", "label"),
        Column("hosts_for_record", "how many different hosts this source offers the record", "label"),
        Column("website_basis", "published_website, or email_domain (Illinois)", "copy", "standard.site_claims.website_basis"),
        Column("record_has_own_website", "whether the record's own list already publishes a website", "label", "standard.records.website"),
        Column("same_host_as_own", "whether the claimed host equals the host of the record's own website", "label"),
    ),
    inputs=("standard.records", "standard.site_claims", "standard.code_states"),
))

ABBREVIATIONS = {
    "MUD": "MUNICIPAL UTILITY DISTRICT", "WCID": "WATER CONTROL AND IMPROVEMENT DISTRICT", "FWSD": "FRESH WATER SUPPLY DISTRICT",
    "SUD": "SPECIAL UTILITY DISTRICT", "PUD": "PUBLIC UTILITY DISTRICT", "PSD": "PUBLIC SERVICE DISTRICT",
    "FPD": "FIRE PROTECTION DISTRICT", "VFD": "VOLUNTEER FIRE DEPARTMENT", "FD": "FIRE DEPARTMENT",
    "ESD": "EMERGENCY SERVICES DISTRICT", "WSC": "WATER SUPPLY CORPORATION", "DIST": "DISTRICT", "TWP": "TOWNSHIP",
    "CNTY": "COUNTY", "SAINT": "ST", "MOUNT": "MT", "FORT": "FT",
}
DROP = {"THE", "INC", "NO", "NUMBER", "NBR"}
DISTRICT_WORDS = {"MUD", "WCID", "FWSD", "SUD", "PUD", "PSD", "FPD", "ESD", "MUNICIPAL", "WATER", "FRESH", "UTILITY", "SPECIAL",
                  "PUBLIC", "FIRE", "EMERGENCY", "DRAINAGE", "IMPROVEMENT", "HOSPITAL", "LEVEE", "RECLAMATION", "DIST", "DISTRICT",
                  "RURAL", "REGIONAL", "SANITARY", "SEWER", "LIBRARY", "PARK", "CONSOLIDATED"}
PREFIXES = (("CITY AND COUNTY OF", "CITY"), ("CONSOLIDATED CITY OF", "CITY"), ("CHARTER TOWNSHIP OF", "TOWNSHIP"),
            ("UNIFIED GOVERNMENT OF", None), ("INCORPORATED VILLAGE OF", "VILLAGE"), ("CITY OF", "CITY"), ("TOWN OF", "TOWN"),
            ("VILLAGE OF", "VILLAGE"), ("BOROUGH OF", "BOROUGH"), ("TOWNSHIP OF", "TOWNSHIP"), ("COUNTY OF", "COUNTY"),
            ("PARISH OF", "COUNTY"), ("PLANTATION OF", "PLANTATION"), ("MUNICIPALITY OF", "MUNICIPALITY"), ("CORPORATION OF", None))
GOVERNMENT_WORDS = ("BOARD OF COUNTY COMMISSIONERS", "BOARD OF COMMISSIONERS", "BOARD OF SUPERVISORS", "COMMISSIONERS COURT",
                    "COUNTY COMMISSION", "COMMISSIONERS", "FISCAL COURT", "GOVERNMENT", "GOVT", "TOWN HALL", "CITY HALL",
                    "VILLAGE HALL", "MUNICIPAL BUILDING", "BOARD OF SELECTMEN", "SELECTBOARD", "CITY COUNCIL", "ADMINISTRATION")
SUFFIXES = (("CHARTER TOWNSHIP", "TOWNSHIP"), ("TOWNSHIP", "TOWNSHIP"), ("CITY", "CITY"), ("TOWN", "TOWN"), ("VILLAGE", "VILLAGE"),
            ("BOROUGH", "BOROUGH"), ("COUNTY", "COUNTY"), ("PARISH", "COUNTY"))
GENERAL = {"place", "county"}

RECORD_CLASS = {"municipality": "place", "town_or_township": "place", "county": "county", "special_district": "district",
                "water_system": "water", "fire_department": "fire", "library_system": "library", "transit_reporter": "transit",
                "school_district": "school", "dependent_school_system": "school"}
# claim class -> the record classes it may match
COMPATIBLE = {"place": {"place"}, "county": {"county"}, "school": {"school"}, "transit": {"transit", "district"},
              "fire": {"fire", "district"}, "water": {"water", "district"}, "library": {"library", "district"},
              "water_district": {"water", "district"}, "district": {"district"}, "local": {"district"},
              "any_district": {"district", "water", "fire", "library"}}
TEXAS_WATER = re.compile(r"WATER|UTILITY|MUNICIPAL UTILITY|FRESH WATER|IRRIGATION", re.I)
ILLINOIS_PLACE = {"VILLAGE", "CITY", "TOWN", "TOWNSHIP", "INCORPORATED TOWN"}
WEBMAIL = re.compile(r"(^|\.)(gmail|googlemail|yahoo|ymail|rocketmail|hotmail|outlook|live|msn|aol|icloud|me|mac|comcast|att|"
                     r"sbcglobal|frontier|frontiernet|charter|verizon|earthlink|juno|netzero|mchsi|mediacombb|windstream|centurytel|"
                     r"centurylink|cox|bellsouth|ameritech|prodigy|wowway|tds|consolidated|hughes|q|protonmail|proton|mail|"
                     r"gmx|zoho|fastmail)\.(com|net|org)$")
GENERIC_WORDS = {"DISTRICT", "FIRE", "PROTECTION", "PUBLIC", "LIBRARY", "VILLAGE", "TOWNSHIP", "COUNTY", "CITY", "TOWN", "PARK",
                 "SANITARY", "WATER", "ROAD", "BRIDGE", "SCHOOL", "COMMUNITY", "UNIT", "AREA", "MULTI", "TAX", "ASSESSMENT",
                 "SPECIAL", "SERVICE", "MUNICIPAL", "UTILITY", "DRAINAGE", "CEMETERY", "FOREST", "PRESERVE", "HOSPITAL", "AIRPORT",
                 "AUTHORITY", "MOSQUITO", "ABATEMENT", "SOIL", "CONSERVATION", "RIVER", "CONSOLIDATED", "EAST", "WEST", "NORTH",
                 "SOUTH", "NEW", "LAKE"}

STATE_NAMES: dict[str, str] = {}  # postal -> state name, upper case; filled from standard.code_states by build()


def _tokens(name: str) -> list[str]:
    text = re.sub(r"[^A-Z0-9]+", " ", name.upper().replace("&", " AND ").replace("'", ""))
    words = text.split()
    out = []
    for i, word in enumerate(words):
        if word == "CO" and i + 1 < len(words) and words[i + 1] in DISTRICT_WORDS:
            out.append("COUNTY")
        elif word not in DROP:
            out.extend(ABBREVIATIONS.get(word, word).split())
    return out


def name_key(name: str | None, state: str | None, general: bool) -> str | None:
    """'normalised name|kind word' (kind word only for cities, towns, townships and counties)."""
    if not name:
        return None
    text = " ".join(_tokens(name))
    for tail in (state or "", STATE_NAMES.get(state or "", "")):
        if tail and text.endswith(" " + tail) and len(text) > len(tail) + 1:
            trimmed = text[: -len(tail) - 1]
            if trimmed not in {p for p, _ in PREFIXES}:
                text = trimmed
                break
    kind = None
    if general:
        changed = True
        while changed:
            changed = False
            for words in GOVERNMENT_WORDS:
                if text.endswith(" " + words):
                    text, changed = text[: -len(words) - 1], True
        for prefix, word in PREFIXES:
            if text.startswith(prefix + " "):
                text, kind = text[len(prefix) + 1:], word
                break
        else:
            for suffix, word in SUFFIXES:
                if text.endswith(" " + suffix):
                    text, kind = text[: -len(suffix) - 1], word
                    break
    return f"{text}|{kind or ''}" if text else None


def claim_class(list_name: str, kind_codes: str | None, naics: str | None, name_hint: str | None = None) -> str | None:
    kind = (kind_codes or "").strip()
    if list_name == sc.SAM:
        types, codes = set(kind.split("~")), naics or ""
        if "C7" in types:
            return "county"
        if types & {"C6", "C8", "FO"}:
            return "place"
        if "H6" in types:
            return "school"
        if "TW" in types:
            return "transit"
        for pattern, klass in ((r"922160", "fire"), (r"221310", "water"), (r"5191[12]0", "library"), (r"(^|~)485", "transit")):
            if re.search(pattern, codes):
                return klass
        return "local" if "12" in types else None
    if list_name == sc.DOTGOV:
        return {"City": "place", "County": "county", "Special district": "district", "School district": "school"}.get(kind)
    if list_name == sc.TEXAS:
        return "water_district" if TEXAS_WATER.search(kind) else "district"
    if list_name == sc.COLORADO:
        return "any_district"
    if list_name == sc.NEW_YORK:
        return "district"
    if list_name == sc.NY_LOCALITIES:
        return {"County": "county", "City": "place", "Town": "place", "Village": "place"}.get(kind)
    if list_name == sc.ILLINOIS:
        upper = kind.upper()
        if upper in ILLINOIS_PLACE:
            return "place"
        if upper == "COUNTY":
            return "county"
        if "SCHOOL" in upper:
            return "school"
        if "DISTRICT" in upper and "ASSESSMENT" not in upper:
            return "any_district"
    if list_name == sc.USAC or list_name in sc.STATE_LIBRARIES or list_name == sc.TEXAS_LIBRARIES:
        return "library"
    if list_name == sc.IRS:
        upper = name_hint or ""
        for pattern, klass in ((r"LIBRARY", "library"), (r"FIRE|RESCUE|HOSE|HOOK AND LADDER|AMBULANCE|EMERGENCY", "fire"),
                               (r"WATER", "water"), (r"TRANSIT|TRANSPORTATION", "transit")):
            if re.search(pattern, upper):
                return klass
    return None  # Oregon, Delaware, CAL FIRE, Wisconsin and Wikidata are matched by exact ID only


def claim_name(list_name: str, name: str | None, kind_codes: str | None) -> str | None:
    """Illinois gives the kind apart from the name ('Westchester', 'Village'): put them together when the name lacks it."""
    if list_name == sc.ILLINOIS and name and kind_codes and kind_codes.upper() not in name.upper():
        return f"{name} {kind_codes}"
    return name


def host(website: str | None) -> str | None:
    if not website:
        return None
    text = re.sub(r"^[a-z]+://", "", website.strip().lower())
    text = re.split(r"[/?#\s]", text, maxsplit=1)[0].rstrip(".")
    return text.removeprefix("www.") or None


WEB_ADDRESS = re.compile(r"^(https?://)?([a-z0-9-]+\.)+[a-z]{2,}(:\d+)?(/\S*)?$", re.I)


def website_ok(website: str | None) -> bool:
    """Whether the claimed website has the form of a web address (990-N's field is free text: often an address or a name)."""
    return bool(website and WEB_ADDRESS.match(website.strip()))


def domain_ok(domain: str | None, name: str | None) -> bool:
    """An Illinois mail domain is a website lead only if it is no webmail or provider domain and looks like the unit's."""
    if not domain or WEBMAIL.search(domain):
        return False
    if domain.endswith((".gov", ".us")):
        return True
    words = {w for w in _tokens(name or "") if len(w) >= 4 and w not in GENERIC_WORDS}
    return any(w.lower() in domain for w in words)


def build(run_id: str) -> dict[str, int]:
    con = duckdb.connect()
    STATE_NAMES.update(con.execute(f"SELECT postal, upper(state_name) FROM {tables.latest('standard', 'code_states')}").fetchall())
    for name, function, args, result in (("name_key", name_key, ["VARCHAR", "VARCHAR", "BOOLEAN"], "VARCHAR"),
                                         ("claim_class", claim_class, ["VARCHAR", "VARCHAR", "VARCHAR", "VARCHAR"], "VARCHAR"),
                                         ("website_ok", website_ok, ["VARCHAR"], "BOOLEAN"),
                                         ("claim_name", claim_name, ["VARCHAR", "VARCHAR", "VARCHAR"], "VARCHAR"),
                                         ("host", host, ["VARCHAR"], "VARCHAR"),
                                         ("domain_ok", domain_ok, ["VARCHAR", "VARCHAR"], "BOOLEAN")):
        con.create_function(name, function, args, result, null_handling="special")
    record_class = " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in RECORD_CLASS.items())
    compatible = " UNION ALL ".join(f"SELECT '{c}' AS claim_class, '{r}' AS record_class" for c, rs in COMPATIBLE.items() for r in rs)
    general = ", ".join(f"'{c}'" for c in GENERAL)
    select = f"""
        WITH recs AS (
            SELECT record_id, list, agency_id, name, state, county_name, county_fips5, place_fips, ueid,
                   website AS own_website, host(website) AS own_host, CASE entity_type {record_class} END AS record_class
            FROM {tables.latest('standard', 'records')}),
        rkeys AS (SELECT *, name_key(name, state, record_class IN ({general})) AS nkey FROM recs WHERE record_class IS NOT NULL),
        claims AS (
            SELECT claim_id, list, website, host(website) AS host, website_basis, state, county, county_fips5, place_fips, uei,
                   source_id, nces_leaid, fscskey, sub_name, claim_class(list, kind_codes, naics_codes, upper(name)) AS claim_class,
                   claim_name(list, name, kind_codes) AS name
            FROM {tables.latest('standard', 'site_claims')} WHERE website_ok(website)),
        usable AS (
            SELECT * EXCLUDE (units_on_host) FROM (SELECT *, count(*) OVER (PARTITION BY list, host) AS units_on_host FROM claims)
            WHERE website_basis = 'published_website' OR (domain_ok(host, name) AND units_on_host <= 2)),
        ckeys AS (SELECT *, name_key(name, state, claim_class IN ({general})) AS nkey FROM usable
                  WHERE claim_class IS NOT NULL AND sub_name IS NULL),
        compatible AS ({compatible}),
        by_name AS (
            SELECT r.record_id, c.claim_id, c.website, c.host, 'name_place' AS method, r.record_class, c.claim_class,
                   c.state || ' | ' || c.nkey AS match_key, c.website_basis, r.own_website, r.own_host, c.list
            FROM ckeys c
            JOIN compatible k ON k.claim_class = c.claim_class
            JOIN rkeys r ON r.record_class = k.record_class AND r.state = c.state
             AND split_part(r.nkey, '|', 1) = split_part(c.nkey, '|', 1)
             AND (split_part(r.nkey, '|', 2) = '' OR split_part(c.nkey, '|', 2) = ''
                  OR split_part(r.nkey, '|', 2) = split_part(c.nkey, '|', 2))
             AND (c.county IS NULL OR r.county_name IS NULL OR upper(trim(c.county)) = upper(trim(r.county_name)))
             AND (c.county_fips5 IS NULL OR r.county_fips5 IS NULL OR c.county_fips5 = r.county_fips5)),
        by_id AS (
            SELECT r.record_id, c.claim_id, c.website, c.host, 'uei_exact' AS method, r.record_class, c.claim_class,
                   c.uei AS match_key, c.website_basis, r.own_website, r.own_host, c.list
            FROM claims c JOIN recs r ON r.ueid = c.uei
            UNION ALL
            SELECT r.record_id, c.claim_id, c.website, c.host, 'fscskey_exact', r.record_class, c.claim_class,
                   c.source_id, c.website_basis, r.own_website, r.own_host, c.list
            FROM claims c JOIN recs r ON r.list = '{records.IMLS}'
             AND r.agency_id = coalesce(c.fscskey, CASE WHEN c.list IN ('{sc.OREGON}', '{sc.WISCONSIN}', '{sc.USAC}') THEN c.source_id END)
            UNION ALL
            SELECT r.record_id, c.claim_id, c.website, c.host, 'fdid_exact', r.record_class, c.claim_class,
                   c.state || '-' || c.source_id, c.website_basis, r.own_website, r.own_host, c.list
            FROM claims c JOIN recs r ON c.list = '{sc.CALIFORNIA_FIRE}' AND r.list = '{records.USFA}' AND r.state = c.state
             AND r.agency_id = c.source_id
            UNION ALL
            SELECT r.record_id, c.claim_id, c.website, c.host, 'place_fips_exact', r.record_class, c.claim_class,
                   c.place_fips, c.website_basis, r.own_website, r.own_host, c.list
            FROM claims c JOIN recs r ON r.place_fips = c.place_fips
            UNION ALL
            SELECT r.record_id, c.claim_id, c.website, c.host, 'county_fips_exact', r.record_class, c.claim_class,
                   c.county_fips5, c.website_basis, r.own_website, r.own_host, c.list
            FROM claims c JOIN recs r ON c.list = 'wd_counties' AND r.record_class = 'county' AND r.county_fips5 = c.county_fips5
            UNION ALL
            SELECT r.record_id, c.claim_id, c.website, c.host, 'nces_exact', r.record_class, c.claim_class,
                   c.nces_leaid, c.website_basis, r.own_website, r.own_host, c.list
            FROM claims c JOIN recs r ON r.list = '{records.LEA}' AND r.agency_id = c.nces_leaid
            UNION ALL
            SELECT r.record_id, c.claim_id, c.website, c.host, 'pwsid_exact', r.record_class, c.claim_class,
                   c.source_id, c.website_basis, r.own_website, r.own_host, c.list
            FROM usable c JOIN recs r ON c.list = '{sc.EPA_DOMAINS}' AND r.list = '{records.EPA}' AND r.agency_id = c.source_id),
        matched AS (SELECT * FROM by_name UNION ALL SELECT * FROM by_id),
        counted AS (
            SELECT *, count(DISTINCT record_id) OVER (PARTITION BY claim_id, method, record_class) AS records_with_key,
                      count(DISTINCT host) OVER (PARTITION BY record_id, method, list) AS hosts_for_record
            FROM matched)
        SELECT record_id, claim_id, website, host, method, '{VERSION}' AS method_version,
               CASE WHEN records_with_key = 1 AND hosts_for_record = 1 THEN 'accepted' ELSE 'ambiguous' END AS status,
               record_class, claim_class, match_key, records_with_key, hosts_for_record, website_basis,
               own_website IS NOT NULL AS record_has_own_website, host = own_host AS same_host_as_own
        FROM counted
        ORDER BY record_id, method, claim_id"""
    inputs = {f"standard.{t}": tables.latest_run(con, "standard", t) for t in ("records", "site_claims", "code_states")}
    return {SPEC.key: tables.write(con, SPEC, select, run_id, inputs)}
