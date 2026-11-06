"""Free live check: one GET per website host in entities.websites, written to the warehouse in batches as it runs.

Run (VPN on; close the DuckDB UI first, since this rebuilds the warehouse views once at the start):
    uv run python -m ddwb.fetch.live_check [--run RUN_ID] [--import-jsonl FILE]

Tables (the 'web' dataset: what we observe on the web, kept apart from 'raw', which is what agencies publish):
- web.fetches: one row per host checked: the outcome (status, final URL, redirect chain, error, timing), the page's
  identity (title, description, site name, canonical URL, language, generator), its first 15,000 characters of
  visible text, bot-wall and parked-domain flags, the sha256 of the bytes read. The full HTML is not kept.
- web.links: one row per link on that page (up to 500), with its text.

Every 500 hosts a part file of each table is written (tables.write_part), so the views show rows as they arrive and
meta.runs is updated. A run stopped for any reason resumes with --run and the same run ID: hosts already written are
skipped. --recheck-failed-since re-checks, in the same run, the hosts that gave no answer since that time (a network
fault on our side); their earlier rows stay, and readers take each host's latest check.

Network guard: when more than half a batch gives no answer (normally about a fifth), five hosts that answered before
are tried. If they fail too, the network is down, not the sites: the run waits a minute and tries again, and stops
after ten tries, so it never records a network fault as dead websites. Rules (notes/RULES.md): our user agent, no personal information; one request per host, many hosts at once;
nothing is retried and no bot wall is worked around: it is recorded.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import re
import sys
import tempfile
import time
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import duckdb
import httpx

from .. import catalogue, tables, warehouse
from ..catalogue import Column

USER_AGENT = "public-signals-research/0.1"
EGRESS = "US VPN (New York exit)"
BATCH, MAX_BYTES, TEXT_CHARS, MAX_LINKS = 500, 2_000_000, 15_000, 500
BOT_WALL = re.compile(r"just a moment|attention required|cf-chl|captcha|are you a robot|access denied|request unsuccessful|"
                      r"verify you are human|ddos protection", re.I)
PARKED = re.compile(r"domain (is|may be) for sale|buy this domain|this domain is parked|parked free|domain parking|"
                    r"hugedomains|sedoparking|this domain has expired|godaddy\.com/domainsearch", re.I)

OBSERVED = "the HTTP response, as our client received it"
FETCHES = catalogue.register(catalogue.Table(
    dataset="web", name="fetches", grain="check of a website host (the homepage, one GET); a host re-checked after a network fault has more than one row, and readers use its latest",
    description="The free live check: what each website host returned (see ddwb/fetch/live_check.py)",
    columns=(
        Column("host", "the host checked (from entities.websites)", "copy", "entities.websites.host"),
        Column("url_requested", "the URL requested", "copy", "entities.websites.website"),
        Column("status", "HTTP status of the final response (empty when no response)", "copy", OBSERVED),
        Column("final_url", "the URL after redirects", "copy", OBSERVED),
        Column("redirects", "the URLs redirected through, in order", "copy", OBSERVED),
        Column("content_type", "the Content-Type header", "copy", OBSERVED),
        Column("bytes", "bytes read (at most 2,000,000)", "copy", OBSERVED),
        Column("sha256", "sha256 of the bytes read: the page's fingerprint, for spotting identical pages", "bookkeeping"),
        Column("error", "the error, when there was no response (DNS, refused, timeout, TLS)", "copy", OBSERVED),
        Column("elapsed_ms", "time taken", "bookkeeping"),
        Column("title", "the page's <title>", "split", OBSERVED),
        Column("description", "its meta description", "split", OBSERVED),
        Column("site_name", "its og:site_name", "split", OBSERVED),
        Column("canonical", "its canonical URL", "split", OBSERVED),
        Column("lang", "its <html lang>", "split", OBSERVED),
        Column("generator", "its meta generator (the CMS, when the page says)", "split", OBSERVED),
        Column("text", "its visible text, first 15,000 characters (scripts and styles left out)", "split", OBSERVED),
        Column("link_count", "how many links the page has (up to 500 kept in web.links)", "bookkeeping"),
        Column("bot_wall", "status 403 or 429, or a bot-check page (captcha, 'just a moment', ...)", "label", OBSERVED),
        Column("parked", "a parked or for-sale domain page", "label", OBSERVED),
        Column("fetched_at", "when (UTC)", "bookkeeping"),
        Column("user_agent", "the user agent sent", "bookkeeping"),
        Column("egress", "where the request left from", "bookkeeping"),
    ),
    inputs=("entities.websites",),
))
LINKS = catalogue.register(catalogue.Table(
    dataset="web", name="links", grain="link on a checked homepage",
    description="Every link on each checked homepage (up to 500 per page), with its text",
    columns=(
        Column("host", "the host checked", "copy", "web.fetches.host"),
        Column("page_url", "the page the link is on (the final URL)", "copy", "web.fetches.final_url"),
        Column("position", "the link's order on the page, from 1", "bookkeeping"),
        Column("href", "the link, made absolute", "split", OBSERVED),
        Column("anchor_text", "the link's text", "split", OBSERVED),
    ),
    inputs=("web.fetches",),
))


class _Page(HTMLParser):
    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base, self.text, self.links, self.meta = base, [], [], {}
        self.title, self.lang, self._skip, self._title, self._link = "", None, 0, False, None

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in ("script", "style", "noscript", "svg"):
            self._skip += 1
        elif tag == "title":
            self._title = True
        elif tag == "html" and a.get("lang"):
            self.lang = a["lang"]
        elif tag == "meta":
            key = (a.get("name") or a.get("property") or "").lower()
            if key in ("description", "og:site_name", "generator") and a.get("content"):
                self.meta.setdefault(key, a["content"].strip()[:500])
        elif tag == "link" and "canonical" in a.get("rel", "").lower() and a.get("href"):
            self.meta.setdefault("canonical", urljoin(self.base, a["href"]))
        elif tag == "a" and a.get("href") and len(self.links) < MAX_LINKS:
            self._link = [urljoin(self.base, a["href"].strip()), ""]
            self.links.append(self._link)

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg") and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._title = False
        elif tag == "a":
            self._link = None

    def handle_data(self, data):
        if self._skip:
            return
        if self._title:
            self.title += data
        if self._link is not None and len(self._link[1]) < 200:
            self._link[1] += data
        if sum(len(t) for t in self.text) < TEXT_CHARS:
            self.text.append(data)


def _url(website: str) -> str:
    website = website.strip()
    return website if re.match(r"^https?://", website, re.I) else "http://" + website


async def _check(client: httpx.AsyncClient, host: str, website: str) -> dict:
    started, out = time.monotonic(), {"host": host, "url_requested": _url(website)}
    try:
        async with client.stream("GET", out["url_requested"]) as response:
            body = b""
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) >= MAX_BYTES:
                    break
            out |= {"status": response.status_code, "final_url": str(response.url),
                    "redirects": [str(r.url) for r in response.history], "content_type": response.headers.get("content-type"),
                    "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(), "error": None}
            text = body.decode(response.encoding or "utf-8", errors="replace") if "html" in (out["content_type"] or "") else ""
    except Exception as error:  # recorded, not retried
        out |= {"status": None, "final_url": None, "redirects": [], "content_type": None, "bytes": 0, "sha256": None,
                "error": f"{type(error).__name__}: {str(error)[:200]}"}
        text = ""
    page = _Page(out.get("final_url") or out["url_requested"])
    try:
        page.feed(text)
    except Exception:
        pass
    visible = re.sub(r"\s+", " ", " ".join(page.text)).strip()[:TEXT_CHARS]
    head = (page.title + " " + visible[:3000]).lower()
    return out | {"elapsed_ms": int((time.monotonic() - started) * 1000), "title": re.sub(r"\s+", " ", page.title).strip()[:500] or None,
                  "description": page.meta.get("description"), "site_name": page.meta.get("og:site_name"),
                  "canonical": page.meta.get("canonical"), "lang": page.lang, "generator": page.meta.get("generator"),
                  "text": visible or None, "links": [[h, re.sub(r"\s+", " ", t).strip()] for h, t in page.links],
                  "bot_wall": bool(BOT_WALL.search(head)) or out["status"] in (403, 429), "parked": bool(PARKED.search(head)),
                  "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"), "user_agent": USER_AGENT, "egress": EGRESS}


def _write_batch(con: duckdb.DuckDBPyConnection, rows: list[dict], run_id: str, part: int) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    read = f"""read_json('{handle.name}', format = 'newline_delimited', columns = {{
        host: 'VARCHAR', url_requested: 'VARCHAR', status: 'INTEGER', final_url: 'VARCHAR', redirects: 'VARCHAR[]',
        content_type: 'VARCHAR', bytes: 'BIGINT', sha256: 'VARCHAR', error: 'VARCHAR', elapsed_ms: 'INTEGER', title: 'VARCHAR',
        description: 'VARCHAR', site_name: 'VARCHAR', canonical: 'VARCHAR', lang: 'VARCHAR', generator: 'VARCHAR', text: 'VARCHAR',
        links: 'VARCHAR[][]', bot_wall: 'BOOLEAN', parked: 'BOOLEAN', fetched_at: 'TIMESTAMPTZ', user_agent: 'VARCHAR',
        egress: 'VARCHAR'}})"""
    tables.write_part(con, FETCHES, f"SELECT * EXCLUDE (links), len(links) AS link_count FROM {read}", run_id, part)
    tables.write_part(con, LINKS, f"""SELECT host, final_url AS page_url, generate_subscripts(links, 1) AS position,
                                             unnest(links)[1] AS href, unnest(links)[2] AS anchor_text FROM {read}""", run_id, part)
    Path(handle.name).unlink()


def _done(run_id: str) -> set[str]:
    files = tables.parts(FETCHES, run_id)
    if not files:
        return set()
    return {r[0] for r in duckdb.connect().execute(f"SELECT host FROM read_parquet({[str(f) for f in files]!r})").fetchall()}


def _failed_since(run_id: str, since: str) -> set[str]:
    """Hosts whose latest check in this run gave no answer and was made at or after `since` (ISO time)."""
    files = tables.parts(FETCHES, run_id)
    if not files:
        return set()
    return {r[0] for r in duckdb.connect().execute(f"""
        SELECT host FROM (SELECT * FROM read_parquet({[str(f) for f in files]!r})
                          QUALIFY row_number() OVER (PARTITION BY host ORDER BY fetched_at DESC) = 1)
        WHERE status IS NULL AND fetched_at >= TIMESTAMPTZ '{since}'""").fetchall()}


def _canaries(run_id: str, n: int = 5) -> list[tuple[str, str]]:
    files = tables.parts(FETCHES, run_id)
    return duckdb.connect().execute(f"""SELECT host, url_requested FROM read_parquet({[str(f) for f in files]!r})
                                        WHERE status = 200 AND elapsed_ms < 5000 ORDER BY sha1(host) LIMIT {n}""").fetchall()


async def _network_ok(client: httpx.AsyncClient, canaries: list[tuple[str, str]]) -> bool:
    results = await asyncio.gather(*(_check(client, h, w) for h, w in canaries))
    return sum(1 for r in results if r["status"] is not None) >= max(1, len(canaries) // 2 + 1)


def _hosts() -> list[tuple[str, str]]:
    return duckdb.connect().execute(f"""SELECT host, arg_min(website, rank) FROM {tables.latest('entities', 'websites')}
                                        WHERE host IS NOT NULL AND host LIKE '%.%' GROUP BY host ORDER BY host""").fetchall()


async def _run(run_id: str, import_jsonl: str | None, concurrency: int, recheck_since: str | None = None) -> None:
    started = datetime.now(UTC)
    con = duckdb.connect()
    part = len(tables.parts(FETCHES, run_id))
    if import_jsonl and part == 0:  # results saved by the first version of this script, which wrote one file
        rows = []
        with gzip.open(import_jsonl, "rt", encoding="utf-8") as handle:
            try:
                for line in handle:
                    try:
                        rows.append(json.loads(line) | {"egress": EGRESS})
                    except json.JSONDecodeError:
                        pass  # the last line of a file cut off mid-write
            except EOFError:
                pass  # the file was cut off when that run was stopped
        for i in range(0, len(rows), BATCH):
            part += 1
            _write_batch(con, rows[i:i + BATCH], run_id, part)
    try:
        warehouse.build()  # the web.* views, so rows show while the run goes on
    except duckdb.IOException as error:  # the DuckDB UI holds the lock; the views exist from an earlier build
        print(f"warehouse not rebuilt ({str(error)[:80]}...): carrying on", file=sys.stderr, flush=True)
    done = _done(run_id)
    recheck = _failed_since(run_id, recheck_since) if recheck_since else set()
    todo = [(h, w) for h, w in _hosts() if h not in done or h in recheck]
    canaries = _canaries(run_id)
    print(f"run {run_id}: {len(done):,} done, {len(recheck):,} to re-check, {len(todo):,} to check", file=sys.stderr, flush=True)
    queue: asyncio.Queue = asyncio.Queue()
    for item in todo:
        queue.put_nowait(item)
    batch: list[dict] = []
    upstream = {"entities.websites": tables.latest_run(con, "entities", "websites")}

    def flush() -> float:
        """Write the batch; return its share of hosts that gave no answer."""
        nonlocal part, batch
        failed = sum(1 for r in batch if r["status"] is None) / len(batch) if batch else 0.0
        if batch:
            part += 1
            _write_batch(con, batch, run_id, part)
            batch = []
            tables.record_parts(con, FETCHES, run_id, upstream, started)
            tables.record_parts(con, LINKS, run_id, {"web.fetches": run_id}, started)
            print(f"part {part} written ({failed:.0%} no answer)", file=sys.stderr, flush=True)
        return failed

    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=0)
    async with httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, follow_redirects=True, max_redirects=5,
                                 timeout=httpx.Timeout(30.0, connect=10.0), limits=limits) as client:
        healthy = asyncio.Event()
        healthy.set()

        async def guard() -> None:
            for attempt in range(10):
                if canaries and await _network_ok(client, canaries):
                    print("network back: carrying on", file=sys.stderr, flush=True)
                    healthy.set()
                    return
                print(f"network down (try {attempt + 1}/10): waiting a minute", file=sys.stderr, flush=True)
                await asyncio.sleep(60)
            print("network still down after 10 tries: stopping; resume with --run and --recheck-failed-since", file=sys.stderr, flush=True)
            while not queue.empty():
                queue.get_nowait()
            healthy.set()

        async def worker():
            while not queue.empty():
                await healthy.wait()
                if queue.empty():
                    break
                host, website = queue.get_nowait()
                result = await _check(client, host, website)
                batch.append(result)  # after the await: `batch` may have been written and replaced meanwhile
                if len(batch) >= BATCH and flush() > 0.5 and healthy.is_set():
                    healthy.clear()  # more than half failed: check the network before going on
                    asyncio.create_task(guard())
        await asyncio.gather(*(worker() for _ in range(concurrency)))
    flush()
    tables.record_parts(con, FETCHES, run_id, upstream, started)
    tables.record_parts(con, LINKS, run_id, {"web.fetches": run_id}, started)
    print(f"finished run {run_id}: {len(_done(run_id)):,} hosts", file=sys.stderr, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", help="the run ID to write or resume (default: a new one)")
    parser.add_argument("--import-jsonl", help="results saved by the first version of this script, to load as the first parts")
    parser.add_argument("--concurrency", type=int, default=64, help="hosts checked at once (one request per host); default 64")
    parser.add_argument("--recheck-failed-since", help="ISO time: re-check hosts whose latest check gave no answer since then")
    args = parser.parse_args()
    run = args.run or tables.new_run_id()
    asyncio.run(_run(run, args.import_jsonl, args.concurrency, args.recheck_failed_since))
