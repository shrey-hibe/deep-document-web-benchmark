"""The driver: code decides the order and the work; the model only judges items.

Given a session whose map is built (meetings and document links in the nav store), for each governing-board meeting
date, newest first:
1. choose the documents by the reading plan: the meeting's minutes; else its agenda; else its packet. A work or study
   session's documents are read only when the date has no other meeting's (they take no votes). A file read already
   (the same bytes under another link) is not read twice;
2. read each with the navigator's own readers (`tools.download`, `tools.doc_pages`) and turn it into items
   (`items.document_items`); procedural items are set aside, counted. Each passage is a passage node, as `read` makes;
   the read link covers the meeting's other documents (`tools.cover_meeting`);
3. Jev labels the passages (`tools.label_passages`: BUYING_EVENT + PEOPLE); an item whose passages are all at p <= 0.2
   is set aside, counted;
4. the small model decides the rest with the fixed form (`form`), in batches, one document per model context; an
   answer that fails a check is asked once more, then left undecided, counted;
5. the check: items the model called "no event" though Jev rates them p >= 0.8 or they state an amount, and consent
   items left undecided, are asked again with the reason; what changed is counted;
6. escalation: items the model marks unsure, or with Jev's p between 0.3 and 0.7, go to the bigger model with the same
   form; its answer replaces the small model's; escalations and their cost are counted;
7. decisions are recorded with `tools.t_record` (same event nodes, same-item set-aside), so `truth.score` scores them;
8. the margin audit (arms with `audit`, decision #126): after every date, a stronger model reads the items Jev set
   aside just under this body's cut, band by band (policy.toml [audit]); a decision found there is recorded, the body's
   cut (profile set_aside_p) moves below the band, and the next band is read; a clean band ends it. The thresholds the
   steps use (the cut, check_p, the procedural limit) are this body's profile values (ddwb.deep.profile).
No leg limit, no early stop: every planned meeting is done; the links left are closed with the plan's reason, and the
coverage is computed by `tools.coverage`, never claimed.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import date

from .. import paths, policy
from ..deep import profile
from ..deep import state as nav_state
from ..deep import text, tools, web
from ..deep.state import State, nid
from . import form
from .backends import Asked
from .items import CONSENT, document_items

AUDIT = policy.audit()  # the margin audit: its model, band width and item cap (policy.toml)
ESCALATE = tuple(policy.decide()["escalate_band"])  # Jev unsure: the bigger model decides (policy.toml)
WORK_SESSION = re.compile(r"work ?session|worksession|study session|workshop|retreat", re.I)
NOT_ONLY_SESSION = re.compile(r"special|regular", re.I)
PLAN = ("minutes", "agenda", "packet")


@dataclass(frozen=True)
class Arm:
    name: str
    small: str | None  # the deciding model's tier (haiku, sonnet), or None for a decider without a model
    big: str | None  # the escalation model's tier, or None
    jev: bool
    decider: str  # model | jev | recorded
    check: bool
    audit: bool = False  # the margin audit, and this body's profile in force (else the defaults: a fixed arm)


ARMS = {
    "haiku-jev": Arm("haiku-jev", "haiku", "sonnet", True, "model", True),
    "haiku": Arm("haiku", "haiku", "sonnet", False, "model", True),
    "sonnet-jev": Arm("sonnet-jev", "sonnet", None, True, "model", True),
    "haiku-jev-solo": Arm("haiku-jev-solo", "haiku", None, True, "model", True),  # no escalation: what Sonnet adds, measured
    "haiku-jev-audit": Arm("haiku-jev-audit", "haiku", None, True, "model", True, audit=True),  # haiku-jev-solo, tuned per body
    "jev-only": Arm("jev-only", None, None, True, "jev", False),
    "recorded": Arm("recorded", None, None, True, "recorded", False),
}


class PipeCx(tools.Cx):
    """The navigator's session context for a pipeline session (any arm label), with the readers and Jev it is given:
    the live ones by default, cache-only ones for a replay."""

    def __init__(self, sid: str, *, jev_on: bool, http=None, jev=None, offline: bool = False, profiled: bool = False) -> None:
        parts = sid.split(":")
        if len(parts) != 3 or not tools.SLUG.fullmatch(parts[2]):
            raise tools.Stop("session must be <run>:<arm>:<body>")
        self.sid, (self.run, self.arm, self.body) = sid, parts
        self.jev_on, self.offline, self.jev = jev_on, offline, jev
        self.profiled, self.carry = profiled, False  # a benchmark session starts from the defaults, never a carried profile
        self.st = State(self.run)
        self.sess = self.st.session(sid)
        self._http, self._providers = http, None

    @property
    def providers(self):
        if self.offline:
            raise tools.Stop("a replay makes no paid calls")
        return tools.Cx.providers.fget(self)

    def caps(self) -> dict[str, float]:
        return tools.DEFAULT_CAPS | {"model": 0.0} | (self.st.settings().get("caps") or {})

    def jev_many(self, items: list[tuple], qs: dict, budget: str) -> dict[str, tuple]:
        if self.jev is not None:
            return self.jev.many(self, items, qs, budget)
        if self.offline:
            raise tools.Stop("a replay makes no paid calls")
        return super().jev_many(items, qs, budget)


def quiet(fn, *a):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a)


def is_session(n: dict) -> bool:
    d = n.get("data") or {}
    blob = f"{d.get('text') or ''} {d.get('row') or ''}"
    return bool(WORK_SESSION.search(blob)) and not NOT_ONLY_SESSION.search(blob)


def plan_date(links: list[dict]) -> tuple[str | None, list[dict], list[tuple[dict, str]]]:
    """The documents to read for one meeting date: (the kind chosen, the links, links set aside with their reason)."""
    usable = [n for n in links if (n["status"] in tools.UNDECIDED and n["status"] != "failed") or n["status"] == "read"]
    plain = [n for n in usable if not is_session(n) and tools.doc_kind(n) in PLAN]
    pool = plain or usable
    for kind in PLAN:
        docs = [n for n in pool if tools.doc_kind(n) == kind]
        if docs:
            aside = [(n, "a work or study session takes no votes; this date has another meeting's documents (reading plan)")
                     for n in usable if n not in pool and tools.doc_kind(n) in PLAN and n["status"] != "read"]
            return kind, docs, aside
    return None, [], []


class Pipeline:
    def __init__(self, cx: PipeCx, arm: Arm, small, big=None, out=print, auditor=None) -> None:
        self.cx, self.arm, self.small, self.big, self.out, self.auditor = cx, arm, small, big, out, auditor
        self.set_aside: list[tuple[dict, dict]] = []  # (document link, item) Jev set aside: what the margin audit reads
        self.stats: Counter = Counter()
        self.money: Counter = Counter()  # cash and estimates, by what spent it
        self.undecided: Counter = Counter()
        self.shas: dict[str, str] = {}
        self.stopped: str | None = None
        self.calls = 0

    # ------------------------------------------------------------------------------- the order of work
    def run(self) -> dict:
        cx = self.cx
        links = cx.st.nodes(cx.sid, "link")
        dates = sorted(tools.governing_meetings(links), reverse=True)
        self.stats["governing_dates"] = len(dates)
        profile.cadence(cx, dates)
        for when in dates:
            if self.stopped:
                break
            self.meeting_date(when)
        if self.arm.audit and not self.stopped:
            self.audit()
        self.close_links()
        return self.answer()

    def meeting_date(self, when: str) -> None:
        """One date: its minutes, else its agenda, else its packet. Documents that cannot be read drop out of the plan,
        so a date whose minutes fail is read from its agenda."""
        cx = self.cx
        for _ in PLAN:
            day = [n for n in cx.st.nodes(cx.sid, "link") if (n.get("data") or {}).get("date") == when and tools.governing(n)]
            kind, docs, aside = plan_date(day)
            for n, why in aside:
                cx.st.put(cx.sid, n["node"], "link", "skipped", tool="pipeline", reason=why,
                          data={"plan": "work_session", "skipped_unseen": True, "skipped_on_jev": False})
            if not docs:
                pages = [n for n in day if tools.doc_kind(n) == "meeting_page" and n["status"] in tools.UNDECIDED
                         and n["status"] != "failed" and not (n.get("data") or {}).get("expanded")]
                if pages and self.open_meeting_pages(pages):
                    continue  # the pages' minutes, agenda or packet joined the plan: choose again
                self.stats["dates_without_a_meeting_document"] += 1
                return
            if all(n["status"] == "read" for n in docs):
                return
            for k in docs:
                k = cx.st.get(cx.sid, k["node"])
                if k["status"] not in tools.UNDECIDED:  # covered or read meanwhile
                    continue
                try:
                    self.document(k)
                except tools.Stop as stop:  # a cap, or the run stopped: finish with what is done, said so
                    self.stopped = str(stop)
                    return
            if any(cx.st.get(cx.sid, k["node"])["status"] == "read" for k in docs):
                self.stats[f"dates_read_from_{kind}"] += 1
                return
            self.stats[f"dates_{kind}_unreadable"] += 1

    def open_meeting_pages(self, pages: list[dict]) -> bool:
        """A date listed only as a meeting's own page (a site with one page per meeting): its document links join the
        map as links of that meeting (minutes, agenda, packet, items). A page that links none is read as the record
        itself. True when a minutes, agenda or packet link joined."""
        cx, joined = self.cx, False
        for k in pages:
            path, fmt, err, resp = self.download(k)
            if err:
                cx.st.put(cx.sid, k["node"], "link", "failed", tool="pipeline", data={"error": err})
                continue
            cx.st.put(cx.sid, k["node"], "link", k["status"], tool="pipeline", data={"expanded": True})
            if fmt == "html":
                page = web.read_html(resp.text, resp.final_url or k["url"])
                children = tools.expand(cx, k, page, "pipeline")
                self.stats["meeting_pages_opened"] += 1
                if any(tools.doc_kind(cx.st.get(cx.sid, c)) in PLAN for c in children):
                    joined = True
                    continue
            try:
                self.document(cx.st.get(cx.sid, k["node"]))  # the page holds the record itself
                self.stats["dates_read_from_meeting_page"] += 1
            except tools.Stop as stop:
                self.stopped = str(stop)
                return False
        return joined

    def download(self, k: dict):
        """The navigator's `download` in a live run. A replay reads the HTTP cache and keeps its copy beside the nav
        stores (data/pipeline/replay-docs/), so it adds nothing to the work root's documents or the data manifest."""
        cx = self.cx
        if not cx.offline:
            return tools.download(cx, k)
        resp = cx.http.get(web.direct_url(k["url"]))
        if not resp.ok:
            return None, "", resp.error or f"HTTP {resp.status}", resp
        fmt = tools.doc_format(resp, k["url"])
        if fmt == "binary":
            return None, fmt, f"a file the tools cannot read ({resp.content_type or 'unknown type'})", resp
        folder = nav_state.NAV.parent / "pipeline" / "replay-docs" / cx.body
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{resp.sha256[:16]}.{fmt}"
        if not path.exists():
            path.write_bytes(resp.body)
        return str(path), fmt, None, resp

    def document(self, k: dict) -> None:
        cx = self.cx
        started = time.time()
        path, fmt, err, resp = self.download(k)
        if err:
            cx.st.put(cx.sid, k["node"], "link", "failed", tool="pipeline", data={"error": err})
            self.stats["documents_failed"] += 1
            return
        if resp.sha256 in self.shas:
            cx.st.put(cx.sid, k["node"], "link", "skipped", tool="pipeline", data={"plan": "same_file", "looked": True},
                      reason=f"the same file as {self.shas[resp.sha256]}, which was read")
            self.stats["documents_same_file"] += 1
            return
        self.shas[resp.sha256] = k["node"]
        try:
            pages, total, no_text, ocred, _page = tools.doc_pages(k, path, fmt, resp, None)
        except tools.Stop as why:
            cx.st.put(cx.sid, k["node"], "link", "failed", tool="pipeline", data={"error": str(why)})
            self.stats["documents_failed"] += 1
            return
        items = document_items(pages, kind=tools.doc_kind(k), pdf_path=paths.stored(path) if fmt == "pdf" else None,
                               procedural_max=profile.value(cx, "procedural_max"))
        when = k["data"].get("date")
        keep: list[str] = []
        with cx.st.batch():
            for idx, it in enumerate(items, start=1):
                parts = it.get("passages") or [{"page": (it.get("pages") or [1])[0], "i": f"item{idx}", "text": it["text"],
                                                 "item_no": it.get("number"), "section": it.get("section"), "refs": it.get("refs")}]
                it["pnodes"] = []
                for ps in parts:
                    pn = nid("P", f"{k['node']}:{ps['page']}:{ps['i']}")
                    it["pnodes"].append(pn)
                    if cx.st.get(cx.sid, pn):
                        continue
                    data = {"doc": k["node"], "page": ps["page"], "item": ps["i"], "date": when, "text": ps["text"],
                            "chars": len(ps["text"]), "amounts": text.amounts(ps["text"])[:5]} \
                        | {f: ps[f] for f in ("item_no", "section", "part", "refs") if ps.get(f)} \
                        | {"item_key": it["pnodes"][0], "item_title": it["title"], "item_number": it.get("number")}
                    if it.get("procedural"):
                        cx.st.put(cx.sid, pn, "passage", "skipped", parent=k["node"], url=k["url"], tool="pipeline",
                                  reason=f"procedural ({it['procedural']}): set aside by the item rules",
                                  data=data | {"procedural": it["procedural"], "pipeline": {"verdict": "procedural"}})
                    else:
                        cx.st.put(cx.sid, pn, "passage", "kept", parent=k["node"], url=k["url"], tool="pipeline", data=data)
                        keep.append(pn)
        self.stats["documents_read"] += 1
        self.stats["items"] += len(items)
        self.stats["items_procedural"] += sum(1 for it in items if it.get("procedural"))
        cx.st.put(cx.sid, k["node"], "link", "read", tool="pipeline", ms=int((time.time() - started) * 1000),
                  data={"path": path, "content_type": fmt, "pages_total": total, "looked": True, "pages_read": "all",
                        "pages_without_text": no_text, "pages_read_by_ocr": ocred, "items_total": len(items),
                        "items_procedural": sum(1 for it in items if it.get("procedural")), "sha256": resp.sha256})
        quiet(tools.cover_meeting, cx, cx.st.get(cx.sid, k["node"]))
        if self.arm.jev and keep:
            c, _ = tools.label_passages(cx, keep, k)
            self.money["jev_usd"] += c
        cards = []
        for it in items:
            if it.get("procedural"):
                continue
            ps = [cx.st.get(cx.sid, pn) for pn in it["pnodes"]]
            ps = [p for p in ps if p]
            labelled = [(p.get("jev") or {}) for p in ps if isinstance((p.get("jev") or {}).get("is_event"), (int, float))]
            best = max(labelled, key=lambda j: j["is_event"], default=None)
            it["jev_p"] = best["is_event"] if best else None
            it["jev_type"], it["jev_action"] = (best or {}).get("type"), (best or {}).get("action")
            if self.arm.jev and labelled and len(labelled) == len(ps) and all(p["status"] == "skipped" for p in ps):
                self.stats["items_set_aside_by_jev"] += 1
                self.mark(it, {"verdict": "set_aside_jev", "jev_p": it["jev_p"]})
                self.set_aside.append((k, it))
                continue
            cards.append(it)
        self.stats["items_decided_on"] += len(cards)
        if cards:
            self.decide(k, cards)

    # ------------------------------------------------------------------------------- deciding
    def meeting(self, k: dict) -> dict:
        card = self.cx.card()
        d = k["data"]
        title = d.get("text") if not tools.GENERIC_TITLE.match(d.get("text") or "") else d.get("row")
        return {"name": (card.get("name") or self.cx.body).title(), "kind": card.get("kind"), "state": card.get("state"),
                "doc_kind": tools.doc_kind(k), "date": d.get("date"), "title": title or "", "url": k["url"]}

    def decide(self, k: dict, cards: list[dict]) -> None:
        meeting = self.meeting(k)
        numbered = list(enumerate(cards, start=1))
        by_n = dict(numbered)
        answers, failed = self.ask_all(self.small, meeting, numbered, None, "decide")
        how: dict[int, dict] = {n: {"by": self.small.by} for n in by_n}
        if self.arm.check and not self.small.deterministic:
            check_p = profile.value(self.cx, "check_p")
            notes = {}
            for n, it in numbered:
                said_no = n in answers and not any(a["event"] for a in answers[n])
                why = []
                if said_no and it.get("jev_p") is not None and it["jev_p"] >= check_p:
                    why.append(f"Jev rates it a likely buying decision (p={it['jev_p']:.2f})")
                if said_no and it.get("amounts"):
                    why.append(f"it states an amount (${max(it['amounts']):,.2f})")
                if n in failed and CONSENT.search(f"{it.get('section') or ''} {it.get('title') or ''}"):
                    why.append("it is a consent item, approved with the consent agenda's vote, and has no decision yet")
                if why:
                    notes[n] = "; ".join(why) + ". Check again."
            if notes:
                again, _ = self.ask_all(self.small, meeting, [(n, by_n[n]) for n in notes], notes, "check")
                self.stats["check_asked"] += len(notes)
                for n, objs in again.items():
                    before = answers.get(n)
                    if before is None or any(a["event"] for a in objs) != any(a["event"] for a in before):
                        self.stats["check_changed"] += 1
                    answers[n] = objs
                    failed.pop(n, None)
                    how[n]["checked"] = notes[n]
        if self.big is not None:
            esc = [n for n, it in numbered if any(a.get("unsure") for a in answers.get(n) or [])
                   or (ESCALATE and it.get("jev_p") is not None and ESCALATE[0] <= it["jev_p"] <= ESCALATE[1])]
            if esc:
                before_cost = self.money["model_usd"]
                again, _ = self.ask_all(self.big, meeting, [(n, by_n[n]) for n in esc], None, "escalate")
                self.stats["escalated"] += len(esc)
                self.money["escalation_usd"] += self.money["model_usd"] - before_cost
                for n, objs in again.items():
                    if n not in answers or any(a["event"] for a in objs) != any(a["event"] for a in answers[n]):
                        self.stats["escalation_changed"] += 1
                    answers[n] = objs
                    failed.pop(n, None)
                    how[n] |= {"by": self.big.by, "escalated": True}
        for n, it in numbered:
            if n in answers:
                self.record(it, answers[n], how[n])
            else:
                reason = failed.get(n) or "no answer"
                self.undecided[reason.split(":")[0][:60]] += 1
                self.mark(it, {"verdict": "undecided", "why": reason, **how[n]}, undecided=reason)

    def ask_all(self, decider, meeting: dict, cards: list[tuple[int, dict]], notes, step: str):
        """Every card answered by `decider`, in batches; a card whose answer fails a check is asked once more (a model
        only). Returns ({card: [clean answers]}, {card: why it has none})."""
        answers: dict[int, list[dict]] = {}
        failed: dict[int, str] = {}
        for batch in form.batches(cards):
            items = dict(batch)
            got = self.call(decider, meeting, batch, notes, step)
            if got.objects is None:
                good, bad = {}, {n: [got.error or "no answer"] for n in items}
                if got.miss:
                    for n in items:
                        failed[n] = f"no cached answer: {got.error}"
                    continue
            else:
                good, bad = form.sort_answers(got.objects, items)
            answers |= good
            if bad and not decider.deterministic:
                self.stats["retried_items"] += len(bad)
                retry_notes = {n: ("your previous answer failed a check: " + "; ".join(bad[n])[:300]) for n in bad}
                again = self.call(decider, meeting, [(n, items[n]) for n in bad], {**(notes or {}), **retry_notes}, step + ":retry")
                if again.objects is not None:
                    good2, bad = form.sort_answers(again.objects, {n: items[n] for n in bad})
                    answers |= good2
                elif again.miss:
                    for n in bad:
                        failed[n] = f"no cached answer: {again.error}"
                    bad = {}
                else:
                    bad = {n: [again.error or "no answer"] for n in bad}
                left_out = [n for n, why in bad.items() if why == ["no answer for this item"]]
                for i in range(0, len(left_out), form.SPLIT_ITEMS):  # a small model drops items from a long batch
                    part = left_out[i: i + form.SPLIT_ITEMS]
                    self.stats["split_items"] += len(part)
                    third = self.call(decider, meeting, [(n, items[n]) for n in part], notes, step + ":split")
                    if third.objects is None:
                        continue
                    good3, bad3 = form.sort_answers(third.objects, {n: items[n] for n in part})
                    answers |= good3
                    for n in part:
                        bad.pop(n, None)
                    bad |= bad3
            for n, why in bad.items():
                failed[n] = "form check failed twice: " + "; ".join(why)[:200] if not decider.deterministic else \
                    ("no answer from " + decider.by if why == ["no answer for this item"] else "; ".join(why)[:200])
        return answers, failed

    def call(self, decider, meeting: dict, batch, notes, step: str) -> Asked:
        cx = self.cx
        if getattr(decider, "live", False):
            cx.check("model")  # raises Stop at the model cap or when the run's paid calls are stopped
        got = decider.ask(meeting, batch, notes)
        if not got.model_call:
            return got
        self.calls += 1
        self.stats[f"model_calls_{step.split(':')[0]}"] += 1
        self.stats["model_calls_cached" if got.cached else "model_calls_missed" if got.miss else "model_calls_sent"] += 1
        self.stats["prompt_chars"] += got.prompt_chars
        self.money["model_usd"] += got.cost
        self.money["model_api_equivalent_usd"] += got.api_usd or 0.0
        if got.miss and got.estimate:
            self.money["missed_calls_estimate_usd"] += got.estimate
        node = nid("C", f"{cx.sid}:{meeting['url']}:{step}:{self.calls}")
        cx.st.put(cx.sid, node, "call", "missed" if got.miss else "failed" if got.objects is None else "answered", tool="pipeline",
                  url=meeting["url"], cost=got.cost, budget="model" if got.cost else None,
                  data={"by": got.by, "step": step, "items": [n for n, _ in batch], "cached": got.cached, "error": got.error,
                        "prompt_chars": got.prompt_chars, "api_equivalent_usd": got.api_usd})
        return got

    # ------------------------------------------------------------------------------- the margin audit
    def audit(self) -> None:
        """Read what Jev set aside just under this body's cut with the stronger model, a band at a time; a decision found
        moves the cut below the band and the next band is read, a clean band ends it (decision #126). The auditor's
        answers are recorded as any decision is; each band read is logged in the profile, with what it found."""
        cx = self.cx
        if self.auditor is None or not self.set_aside:
            return
        band, cap, floor = AUDIT["band"], AUDIT["max_items"], profile.knobs()["set_aside_p"]["low"]
        read = 0
        while read < cap:
            cut = profile.value(cx, "set_aside_p")
            low = round(max(floor, cut - band), 4)
            todo = sorted([(k, it) for k, it in self.set_aside if low < (it.get("jev_p") or 0) <= cut and not it.get("audited")],
                          key=lambda x: -(x[1].get("jev_p") or 0))[:cap - read]
            if not todo:
                break
            found, by_doc = [], {}
            for k, it in todo:
                it["audited"] = True
                by_doc.setdefault(k["node"], (k, []))[1].append(it)
            for k, items in by_doc.values():
                meeting = self.meeting(k)
                numbered = list(enumerate(items, start=1))
                try:
                    answers, failed = self.ask_all(self.auditor, meeting, numbered, None, "audit")
                except tools.Stop as stop:  # the model cap: the audit ends, said so
                    self.stopped = str(stop)
                    return
                for n, it in numbered:
                    if n in answers:
                        if any(a["event"] for a in answers[n]):
                            found.append(it)
                        self.record(it, answers[n], {"by": self.auditor.by, "audited": True})
            read += len(todo)
            self.stats["audit_items_read"] += len(todo)
            self.stats["audit_decisions_found"] += len(found)
            evidence = {"band": [low, cut], "read": len(todo), "decisions": [it["pnodes"][0] for it in found]}
            if not found:
                profile.note(cx, "set_aside_p", source="audit", evidence=evidence, reason=f"band read by {self.auditor.by}: no decision")
                break
            moved = profile.move(cx, "set_aside_p", min(it["jev_p"] for it in found) - 0.01, source="audit", evidence=evidence,
                                 reason=f"{len(found)} decision(s) the cut had set aside, read by {self.auditor.by}")
            if moved is None or moved >= cut:  # at the floor, or a fixed session: no lower band to read
                break

    # ------------------------------------------------------------------------------- recording
    def mark(self, it: dict, verdict: dict, undecided: str | None = None, status: str | None = None, reason: str | None = None) -> None:
        cx = self.cx
        with cx.st.batch():
            for pn in it["pnodes"]:
                p = cx.st.get(cx.sid, pn)
                if not p:
                    continue
                new = status if status and p["status"] == "kept" else p["status"]
                cx.st.put(cx.sid, pn, "passage", new, tool="pipeline", reason=reason if new != p["status"] else None,
                          data={"pipeline": verdict} | ({"undecided": undecided} if undecided else {}))

    def record(self, it: dict, answers: list[dict], how: dict) -> None:
        cx = self.cx
        events = [a for a in answers if a["event"]]
        if not events:
            self.stats["items_no_event"] += 1
            self.mark(it, {"verdict": "not_event", **how}, status="skipped", reason=f"not a buying decision ({how['by']})")
            return
        recorded = []
        for a in events:
            ps = [cx.st.get(cx.sid, pn) for pn in it["pnodes"]]
            ps = [p for p in ps if p]
            p = next((p for p in ps if form.norm(a["quote"]) in form.norm(p["data"]["text"])), ps[0])
            args = argparse.Namespace(passage=p["node"], type=a["type"], action=a["action"], vendor=a["vendor"],
                                      amount=None if a["amount"] is None else repr(float(a["amount"])), term=None, what=a["what"], date=None)
            try:
                quiet(tools.t_record, cx, args)
            except tools.Stop as why:
                self.undecided["record refused"] += 1
                self.mark(it, {"verdict": "undecided", "why": f"record refused: {why}", **how}, undecided=str(why))
                continue
            e = (cx.st.get(cx.sid, p["node"])["data"].get("events") or [None])[-1]
            cx.st.put(cx.sid, e, "event", "recorded", tool="pipeline",
                      data={"by": how["by"], "model_quote": a["quote"], "unsure": a["unsure"], "escalated": bool(how.get("escalated")),
                            "checked": how.get("checked"), "item_number": it.get("number")})
            recorded.append(e)
            self.stats["events_recorded"] += 1
        if recorded:
            self.stats["items_with_events"] += 1
            self.mark(it, {"verdict": "event", "events": recorded, **how}, status="skipped",
                      reason=f"part of an item recorded as {', '.join(recorded)}")

    # ------------------------------------------------------------------------------- the rest of the map, and the answer
    def close_links(self) -> None:
        """Every link the plan did not read is decided with the plan's reason; a document that could not be read is
        unreachable with its error. Nothing is left silently undecided."""
        cx = self.cx
        links = cx.st.nodes(cx.sid, "link")
        today = date.today().isoformat()
        read_dates = {n["data"].get("date") for n in links if n["status"] == "read"}
        with cx.st.batch():
            for n in links:
                if n["status"] not in tools.UNDECIDED:
                    continue
                d = n.get("data") or {}
                when = d.get("date")
                if n["status"] == "failed":
                    cx.st.put(cx.sid, n["node"], "link", "unreachable", tool="pipeline", reason=d.get("error") or "could not be read")
                    continue
                if not when:
                    code, why = "undated", "undated: the reading plan reads the dated documents of the board's meetings"
                elif when < tools.SINCE.isoformat() or when > today:
                    code, why = "outside_window", "outside the window (2025-01-01 to today)"
                elif not tools.governing(n):
                    code, why = "not_governing", "not a meeting of the governing board"
                elif self.stopped:
                    continue  # left undecided: the run stopped before its meeting
                elif when in read_dates:
                    code, why = "other_document", "another document of a meeting whose minutes (or agenda) the plan read"
                else:
                    code, why = "no_meeting_document", "no minutes, agenda or packet listed for this date: reports and items alone are not read"
                cx.st.put(cx.sid, n["node"], "link", "skipped", tool="pipeline", reason=why,
                          data={"plan": code, "skipped_unseen": not d.get("looked"), "skipped_on_jev": False})

    def answer(self) -> dict:
        cx = self.cx
        c = tools.coverage(cx)
        events = sorted(cx.st.nodes(cx.sid, "event", "recorded"), key=lambda n: n["data"].get("date") or "")
        incomplete = []
        if c["governing_meetings_unopened"]:
            incomplete.append(f"{len(c['governing_meetings_unopened'])} of {c['governing_meetings']} governing meeting dates "
                              f"have no document read ({', '.join(c['governing_meetings_unopened'][:6])})")
        if c["governing_meetings_minutes_unread"]:
            incomplete.append(f"{len(c['governing_meetings_minutes_unread'])} dates read without their listed minutes")
        if c["passages_undecided"]:
            incomplete.append(f"{c['passages_undecided']} passages undecided")
        if c["links_undecided"]:
            incomplete.append(f"{c['links_undecided']} links undecided")
        if c["unreachable"]:
            incomplete.append(f"{len(c['unreachable'])} documents unreachable")
        if c["map_gaps"]:
            incomplete.append("the map has gaps: " + "; ".join(c["map_gaps"][:3]))
        if self.stopped:
            incomplete.append(f"stopped: {self.stopped}")
        answer = {"query": tools.QUERY.format(name=(cx.sess["card"] or {}).get("name", cx.body).title(), since=tools.SINCE.isoformat()),
                  "as_of": date.today().isoformat(), "complete": not incomplete, "incomplete_because": incomplete,
                  "events": [{"id": e["node"], **{f: e["data"].get(f) for f in ("date", "type", "action", "vendor", "amount", "what",
                                                                                  "source_url", "page", "quote")}} for e in events],
                  "coverage": c, "pipeline": {"arm": self.arm.name, "stats": dict(self.stats), "undecided": dict(self.undecided),
                                              "money": {k: round(v, 5) for k, v in self.money.items()}}}
        cx.st.put(cx.sid, "A", "answer", "answered", tool="pipeline", data=answer)
        cx.st.set_session(cx.sid, status="answered")
        return answer


def new_session(run: str, arm: str, body: str) -> str:
    return f"{run}:pipe-{arm}:{body}"


def nav_dir():
    return nav_state.NAV
