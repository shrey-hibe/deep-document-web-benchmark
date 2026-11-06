"""Find the body: rank the registry's bodies against a query, look bodies up by ID or by host, and give a body's card.

Scoring (each candidate carries the reasons for its score):
- name (0 to 1): the same name key (labels.websites.name_key of the plain name: 'SUMMIT|COUNTY') 1.0; the same core
  words (the name without designators) 0.9; all the query's core words plus others 0.6 to 0.85; the record's core
  words all in the query 0.55 to 0.8; some words shared 0 to 0.5. Shared words are weighted by rarity (IDF).
- kind: when the query names a kind, +0.3 for that kind and -0.3 for another (+0.1 for a different general kind:
  'city' for a village); 'district' or 'authority' alone +0.15 for any special-purpose body. When it names none,
  general-purpose governments get +0.2 and water systems -0.05 (EPA systems often carry the city's name).
- county, when given: +0.15 the same county, -0.4 another, -0.05 unknown (water, transit and NCES-only bodies).
- ties: +0.03 for a body on the Census government lists (+0.01 NCES), and up to +0.04 for the population of a
  general-purpose government.
The state, when given, is a filter. Candidates within MARGIN of the best are reported as ambiguous instead of picked.
"""

from __future__ import annotations

import math
import threading
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import NamedTuple

from ..labels import websites as w
from . import names, people, snapshot

MARGIN = 0.1  # candidates this close to the best are ambiguous
MAX_CARDS = 10  # cards shown when more than `limit` candidates are close (the ambiguity still lists every close ID)
FLOOR = 0.3  # candidates below this score are dropped
GENERAL_BODY = frozenset({"county", "municipality", "township"})
NAME_KEY_COLUMNS = ("record_id", "body_id", "state", "name", "key", "tokens", "core", "families", "is_anchor")


class Body(NamedTuple):
    body_id: str
    name: str
    entity_type: str
    entity_types: str | None
    state: str | None
    county: str | None
    city: str | None
    population: int | None
    function: str | None
    lists: str
    parent_body_id: str | None
    families: frozenset[str]


def name_rows(con) -> list[tuple]:
    """One row per record (NAME_KEY_COLUMNS), from standard.records and entities.body_records: the name keys."""
    out = []
    for record_id, body_id, state, name, entity_type, function, is_anchor in con.execute(
            "SELECT r.record_id, m.body_id, r.state, r.name, r.entity_type, r.function, m.is_anchor "
            "FROM body_records m JOIN records r USING (record_id) ORDER BY m.body_id, NOT m.is_anchor, r.record_id").fetchall():
        row = names.record_row(name, state, entity_type, function)
        out.append((record_id, body_id, state, name, row.key, " ".join(row.tokens), " ".join(row.core), ",".join(row.families),
                    is_anchor))
    return out


class _Index:
    """Records' name keys and core words, in memory: key text -> records, word -> records, and each word's weight."""

    def __init__(self, rows: list[tuple], bodies: dict[str, Body]) -> None:
        self.body, self.name, self.key, self.words = [], [], [], []
        self.by_key: dict[str, list[int]] = defaultdict(list)
        self.by_word: dict[str, list[int]] = defaultdict(list)
        df: Counter = Counter()
        for i, (record_id, body_id, state, name, key, tokens, core, families, is_anchor) in enumerate(rows):
            words = set((core or tokens or "").split())
            self.body.append(body_id)
            self.name.append(name)
            self.key.append(key)
            self.words.append(words)
            if key:
                self.by_key[key.partition("|")[0]].append(i)
            for word in words:
                self.by_word[word].append(i)
            df.update(words)
        self.n = max(len(rows), 1)
        self.idf = {t: math.log(self.n / c) + 1.0 for t, c in df.items()}
        self.bodies = bodies

    def weight(self, words) -> float:
        return sum(self.idf.get(t, math.log(self.n) + 1.0) for t in words)


class Registry:
    """The registry tables of one folder (data/tables, or a release folder), and lookups over them."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else snapshot.default_root()
        self._con, self.runs = snapshot.open_views(self.root)
        self._lock = threading.Lock()
        self._index: _Index | None = None
        self.load_seconds: float | None = None
        self.states = dict(self._rows("SELECT postal, upper(state_name) FROM code_states"))
        w.STATE_NAMES.update(self.states)  # name_key drops a trailing state name

    # ---------------------------------------------------------------------------------------- plumbing
    def _rows(self, sql: str, params: list | tuple = ()) -> list[tuple]:
        cur = self._con.cursor()  # a cursor per call: callers may run lookups from several threads
        try:
            return cur.execute(sql, list(params)).fetchall()
        finally:
            cur.close()

    def index(self) -> _Index:
        with self._lock:
            if self._index is None:
                started = time.time()
                if "name_keys" in self.runs:
                    rows = self._rows(f"SELECT {', '.join(NAME_KEY_COLUMNS)} FROM name_keys")
                else:
                    cur = self._con.cursor()
                    rows = name_rows(cur)
                    cur.close()
                self._index = _Index(rows, self._bodies(rows))
                self.load_seconds = time.time() - started
        return self._index

    def _bodies(self, rows: list[tuple]) -> dict[str, Body]:
        families: dict[str, set] = defaultdict(set)
        for row in rows:
            families[row[1]].update(f for f in (row[7] or "").split(",") if f)
        out = {}
        for body_id, name, entity_type, entity_types, state, county, city, population, function, lists, parent in self._rows("""
                WITH m AS (SELECT m.body_id, m.is_anchor, r.county_name, r.city, r.population, r.function
                           FROM body_records m JOIN records r USING (record_id)),
                fips AS (SELECT county_fips5, mode(upper(county_name)) AS county FROM records
                         WHERE agency = 'Census' AND county_name IS NOT NULL AND county_fips5 IS NOT NULL GROUP BY 1),
                agg AS (SELECT body_id,
                               arg_min(upper(county_name), NOT is_anchor) FILTER (WHERE county_name IS NOT NULL) AS county,
                               arg_min(city, NOT is_anchor) FILTER (WHERE city IS NOT NULL) AS city,
                               max(population) AS population,
                               arg_min(function, NOT is_anchor) FILTER (WHERE function IS NOT NULL) AS function
                        FROM m GROUP BY 1)
                SELECT b.body_id, b.name, b.entity_type, b.entity_types, b.state, coalesce(a.county, f.county), a.city, a.population,
                       a.function, b.lists, b.parent_body_id
                FROM bodies b LEFT JOIN agg a USING (body_id) LEFT JOIN fips f ON f.county_fips5 = b.county_fips5"""):
            out[body_id] = Body(body_id, name, entity_type, entity_types, state, county, city, population, function, lists or "",
                                parent, frozenset(families.get(body_id, ())))
        return out

    # ---------------------------------------------------------------------------------------- resolve
    def parse(self, query: str | None = None, **kw) -> names.Query:
        return names.parse(query, states=self.states, **kw)

    def resolve(self, query: str | None = None, *, name: str | None = None, state: str | None = None, kind: str | None = None,
                county: str | None = None, limit: int = 5) -> dict:
        """Ranked candidates for a query; see the module docstring for the score and ambiguity."""
        ix = self.index()  # loaded once; `seconds` below is the query alone (load_seconds has the load)
        started = time.time()
        q = self.parse(query, name=name, state=state, kind=kind, county=county)
        best: dict[str, tuple[float, list[str], int]] = {}
        for i in self._candidates(ix, q):
            body = ix.bodies.get(ix.body[i])
            if body is None or (q.state and body.state != q.state):
                continue
            score, reasons = self._score(ix, q, i, body)
            if score >= FLOOR and (body.body_id not in best or score > best[body.body_id][0]):
                best[body.body_id] = (score, reasons, i)
        ranked = sorted(best.items(), key=lambda kv: (-kv[1][0], ix.bodies[kv[0]].name or "", kv[0]))
        top = ranked[0][1][0] if ranked else None
        close = [b for b, (s, _, _) in ranked if top is not None and s >= top - MARGIN]
        shown = [b for b, _ in ranked[:max(limit, min(len(close), MAX_CARDS), 1)]]  # close ones rank first
        candidates = self._cards(ix, [(b, *best[b]) for b in shown])
        ambiguity = self._ambiguity(ix, q, [c for c in candidates if c["body_id"] in close], close) if len(close) > 1 else None
        return {"query": query, "parsed": q.as_dict(), "candidates": candidates, "ambiguous": ambiguity is not None,
                "ambiguity": ambiguity, "considered": len(best), "snapshot": self.runs.get("bodies"),
                "seconds": round(time.time() - started, 4)}

    def _candidates(self, ix: _Index, q: names.Query) -> set[int]:
        out: set[int] = set()
        for text, _ in q.keys:
            out.update(ix.by_key.get(text, ()))
        for word in (q.core or q.tokens):
            out.update(ix.by_word.get(word, ()))
        return out  # the state filter comes after, on each body

    def _score(self, ix: _Index, q: names.Query, i: int, body: Body) -> tuple[float, list[str]]:
        reasons = []
        name, why = _name_score(ix, q, i)
        reasons.append(why)
        kind, why = _kind_score(q.families, body.families)
        if why:
            reasons.append(why)
        score = name + kind
        if q.county:
            have = names.county_key(body.county)
            if have is None:
                score -= 0.05
                reasons.append("county not known for this body")
            elif have == q.county:
                score += 0.15
                reasons.append(f"in {have.title()} County")
            else:
                score -= 0.4
                reasons.append(f"in {have.title()} County, not {q.county.title()}")
        if "census_gus" in body.lists:
            score += 0.03
            reasons.append("on the Census government list")
        elif "nces_lea" in body.lists:
            score += 0.01
        if body.population and body.families & GENERAL_BODY:
            score += min(0.04, 0.005 * math.log10(max(body.population, 1)))
        if q.state:
            reasons.append(f"in {q.state}")
        return round(score, 4), reasons

    def _ambiguity(self, ix: _Index, q: names.Query, close: list[dict], close_ids: list[str]) -> dict:
        """close: the cards of the close candidates shown; close_ids: every close candidate."""
        hosts = defaultdict(set)
        for c in close:
            for site in c["websites"]:
                hosts[site["host"]].add(c["body_id"])
        same_body = {(r["body_id"], c["body_id"]) for c in close for r in c["related"] if r["relation"] == "same_body"}
        shared = sorted(h for h, b in hosts.items() if len(b) > 1)
        states = {ix.bodies[b].state for b in close_ids}
        if len(states) > 1 and not q.state:
            why = f"bodies in {len(states)} states match: name the state"
        elif same_body or shared:
            why = ("probably one body listed by several agencies" + (f" (shared website {', '.join(shared[:3])})" if shared else "")
                   + (" (a same_body candidate link joins them)" if same_body else "") + ": pick by kind, or keep both")
        elif len({ix.bodies[b].entity_type for b in close_ids}) > 1:
            why = "bodies of different kinds share the name: name the kind (city, township, water system, ...)"
        else:
            why = "several bodies of the same kind match: name the county or the state"
        return {"close": close_ids, "margin": MARGIN, "why": why, "count": len(close_ids)}

    # ---------------------------------------------------------------------------------------- cards
    def _cards(self, ix: _Index, picked: list[tuple]) -> list[dict]:
        """picked: (body_id, score, reasons, record index); returns the candidate dicts, in order."""
        ids = [p[0] for p in picked]
        if not ids:
            return []
        marks = ", ".join("?" for _ in ids)
        sites, idents, related = defaultdict(list), defaultdict(dict), defaultdict(list)
        for body_id, website, host, quality, source, rank in self._rows(
                f"SELECT body_id, website, host, quality, source, rank FROM websites WHERE body_id IN ({marks}) "
                f"AND NOT {people.sql_contact('website')} ORDER BY body_id, rank", ids):  # no e-mail addresses typed as websites
            sites[body_id].append({"url": website, "host": host, "quality": quality, "source": source, "chosen": rank == 1})
        for body_id, scheme, value, shared in self._rows(
                f"SELECT body_id, scheme, value, shared FROM identifiers WHERE body_id IN ({marks}) ORDER BY body_id, scheme, value", ids):
            idents[body_id].setdefault(scheme, []).append(value + (" (shared)" if shared else ""))
        if "relations" in self.runs and len(ids) > 1:
            for a, relation, b, status in self._rows(
                    f"SELECT DISTINCT from_body_id, relation, to_body_id, status FROM relations "
                    f"WHERE from_body_id IN ({marks}) AND to_body_id IN ({marks})", ids + ids):
                related[a].append({"body_id": b, "relation": relation, "status": status, "direction": "to"})
                related[b].append({"body_id": a, "relation": relation, "status": status, "direction": "from"})
        out = []
        for body_id, score, reasons, i in picked:
            b = ix.bodies[body_id]
            out.append({"body_id": body_id, "name": b.name, "kind": b.entity_type,
                        "kinds": [k.strip() for k in (b.entity_types or b.entity_type or "").split(",") if k.strip()],
                        "function": b.function, "state": b.state, "county": b.county.title() if b.county else None,
                        "city": b.city.title() if b.city else None, "population": b.population,
                        "websites": sites.get(body_id, [])[:6], "identifiers": idents.get(body_id, {}),
                        "parent_body_id": b.parent_body_id, "related": related.get(body_id, []),
                        "matched_name": ix.name[i] if i is not None else None, "score": score, "reasons": reasons})
        return out

    def candidate(self, body_id: str, score: float = 1.0, reasons: list[str] | None = None) -> dict | None:
        ix = self.index()
        if body_id not in ix.bodies:
            return None
        return self._cards(ix, [(body_id, score, reasons or [], None)])[0]

    def body_card(self, body_id: str) -> dict:
        """The fields evals.run.body_card gives, from the registry tables."""
        row = self._rows("""SELECT b.name, b.entity_type, b.state, r.city, r.county_name, r.function, r.owner_type, r.legal_basis,
                                   r.organization_type, r.department_type, r.population, r.unit_type
                            FROM bodies b JOIN records r ON r.record_id = b.anchor_record_id WHERE b.body_id = ?""", [body_id])
        if not row:
            return {}
        keys = ("name", "kind", "state", "city", "county", "function", "owner_type", "legal_basis", "organization_type",
                "department_type", "population_served", "unit_type")
        return {k: v for k, v in zip(keys, row[0]) if v is not None}

    # ---------------------------------------------------------------------------------------- by ID, by host
    def by_id(self, scheme: str | None, value: str) -> list[dict]:
        """Bodies carrying an ID (entities.identifiers). scheme None searches every scheme. USFA FDIDs and IMLS state
        library IDs carry the state in front ('CA-30070'), as in the table."""
        sql = "SELECT body_id, scheme, value, shared FROM identifiers WHERE value = ?" + (" AND scheme = ?" if scheme else "")
        rows = self._rows(sql + " ORDER BY body_id", [value.strip(), scheme] if scheme else [value.strip()])
        out = []
        for body_id, s, v, shared in rows:
            reasons = [f"identifier {s} = {v}"] + (["another body carries the same ID"] if shared else [])
            card = self.candidate(body_id, 1.0, reasons)
            if card:
                out.append(card)
        return out

    def by_host(self, host: str) -> list[dict]:
        """Bodies with a known website on this host (entities.websites.host); else on a parent or child host."""
        h = w.host(host)
        if not h:
            return []
        rows = self._rows("""SELECT body_id, host, website, quality, source, rank,
                                    CASE WHEN host = ? THEN 0 WHEN ? LIKE '%.' || host THEN 1 ELSE 2 END AS how
                             FROM websites WHERE host = ? OR ? LIKE '%.' || host OR host LIKE '%.' || ?
                             ORDER BY how, rank > 1, quality <> 'ok', body_id""", [h, h, h, h, h])
        if not rows:
            return []
        exact = [r for r in rows if r[6] == 0] or rows
        per_body = {}
        for body_id, hst, website, quality, source, rank, how in exact:
            per_body.setdefault(body_id, (hst, website, quality, source, rank, how))
        n = len(per_body)
        out = []
        for body_id, (hst, website, quality, source, rank, how) in per_body.items():
            reasons = [{0: "host equal", 1: f"parent host {hst}", 2: f"sub-host {hst}"}[how] + f" ({source}, quality {quality})",
                       "the body's chosen website" if rank == 1 else f"a known website (rank {rank})"]
            if n > 1:
                reasons.append(f"{n} bodies share this host")
            card = self.candidate(body_id, 1.0 if how == 0 and rank == 1 else 0.9 if how == 0 else 0.7, reasons)
            if card:
                out.append(card)
        bodies = self.index().bodies  # ties: a body on the Census government lists first, then general-purpose ones
        out.sort(key=lambda c: (-c["score"], "census_gus" not in bodies[c["body_id"]].lists,
                                not bodies[c["body_id"]].families & GENERAL_BODY, c["name"] or ""))
        return out


# ------------------------------------------------------------------------------------------- scores
def _key_match(q: names.Query, key: tuple[str, str] | None) -> str | None:
    if not key:
        return None
    for text, kind in q.keys:
        if text == key[0] and (not kind or not key[1] or kind == key[1]):
            return f"{key[0]}|{key[1]}"
    return None


def _name_score(ix: _Index, q: names.Query, i: int) -> tuple[float, str]:
    matched = _key_match(q, names.split_key(ix.key[i]))
    if matched:
        return 1.0, f"same name key {matched} ({ix.name[i]})"
    qc = frozenset(q.core or q.tokens)
    rc = ix.words[i]
    if qc == rc and qc:
        return 0.9, f"same name words {' '.join(sorted(qc))} ({ix.name[i]})"
    shared = qc & rc
    if not shared:
        return 0.0, "no shared name words"
    sim = ix.weight(shared) / ix.weight(qc | rc)
    if qc <= rc:
        return 0.6 + 0.25 * sim, f"all the query's name words, plus {' '.join(sorted(rc - qc))} ({ix.name[i]})"
    if rc <= qc:
        return 0.55 + 0.25 * sim, f"the record's name words are all in the query, which adds {' '.join(sorted(qc - rc))} ({ix.name[i]})"
    return 0.5 * sim, f"shares {' '.join(sorted(shared))} ({ix.name[i]})"


def _kind_score(qf: frozenset, bf: frozenset) -> tuple[float, str | None]:
    kinds = ", ".join(sorted(bf)) or "of unknown kind"
    if not qf:
        if bf & GENERAL_BODY:
            return 0.2, "no kind named: a general-purpose government is preferred"
        if bf == {"water"}:
            return -0.05, "no kind named: water systems often carry their government's name"
        return 0.0, None
    specific = qf - names.GENERAL - {"special"}
    if specific:
        hit = specific & bf
        if hit:
            return 0.3, f"the kind the query names: {', '.join(sorted(hit))}"
        return -0.3, f"the query names {', '.join(sorted(specific))}; this is {kinds}"
    if "special" in qf:
        if bf - names.GENERAL - {"state"}:
            return 0.15, "a special-purpose body, as 'district'/'authority' in the query says"
        return -0.3, f"the query names a district or authority; this is {kinds}"
    hit = qf & bf
    if hit:
        return 0.3, f"the kind the query names: {', '.join(sorted(hit))}"
    places = {"city", "town", "village", "borough"}
    if "municipality" in bf and qf & places and not bf & places:
        return 0.3, "a municipality, as the query says"
    if bf & {"municipality", "township"} and qf & (places | {"township"}):
        return 0.1, f"the query says {', '.join(sorted(qf))}; this is {kinds}"
    return -0.3, f"the query names {', '.join(sorted(qf))}; this is {kinds}"


# ------------------------------------------------------------------------------------------- text
def format_result(result: dict) -> str:
    """The ranked candidates as plain text (the CLI's output)."""
    p = result.get("parsed") or {}
    lines = []
    if p:
        lines.append(f"query: {result.get('query')!r} -> name {p['name']!r}, state {p['state']}, county {p['county']}, "
                     f"kinds {', '.join(p['kinds']) or '-'}, keys {', '.join(p['keys'])}")
        for note in p.get("notes", []):
            lines.append(f"note: {note}")
    if result.get("ambiguous"):
        a = result["ambiguity"]
        lines.append(f"AMBIGUOUS: {a['count']} bodies within {a['margin']} of the best ({a['why']})")
    if not result.get("candidates"):
        lines.append("no candidates")
    for n, c in enumerate(result.get("candidates", []), start=1):
        site = next((s for s in c["websites"] if s["chosen"]), (c["websites"] or [None])[0])
        ids = ", ".join(f"{k}={'/'.join(v)}" for k, v in c["identifiers"].items())
        place = ", ".join(x for x in (c["city"], f"{c['county']} County" if c["county"] else None, c["state"]) if x)
        lines.append(f"{n}. {c['body_id']}  {c['name']}  [{c['kind']}{' / ' + c['function'] if c['function'] else ''}]  "
                     f"{place}  score {c['score']}")
        more = f"  (+{len(c['websites']) - 1} more)" if len(c["websites"]) > 1 else ""
        lines.append(f"   website: {site['url'] + ' (' + site['quality'] + ')' if site else '-'}{more}   ids: {ids or '-'}")
        lines.append(f"   why: {'; '.join(c['reasons'])}")
        for r in c["related"]:
            lines.append(f"   related: {r['relation']} ({r['status']}) {r['direction']} {r['body_id']}")
    if "seconds" in result:
        lines.append(f"({result['considered']} bodies scored in {result['seconds'] * 1000:.0f} ms; snapshot {result['snapshot']})")
    return "\n".join(lines)
