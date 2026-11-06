# public-records

Ask an agent what a US public body (a county, city, school district or special district) bought or decided, and who
changed roles, and get an answer built from the body's own meeting records: each decision with its date, outcome,
vendor and amount, a link to the document and the quoted line, plus what was covered and what could not be read, and why.

The agent works through tools, one per step: find the body in a registry of about 56,000 US public bodies, find where
its meetings are published (portal adapters for Legistar, BoardDocs, Granicus, IQM2, eScribe, CivicClerk and ten
more, else its own website), map every meeting since 2025-01-01, read the minutes item by item, record each decision
against the passage it came from, and answer. A cheap judge (Jev) can label items first so the agent reads
less; it is optional.

This repository is also a benchmark of that work (`evals/`, `ddwb.pipeline`): the same steps run by code with a small
model, scored against truth sets.

## Install (Claude Code)
```
/plugin marketplace add shrey-hibe/deep-document-web-benchmark
/plugin install public-records@public-signals
```
Needs [`uv`](https://docs.astral.sh/uv/). The first start installs the Python dependencies (a minute; if `/mcp` shows
the server as failed, reconnect it), and the first question downloads the body registry once (34 MB, checksum
checked). Everything the tools write (run stores, downloaded documents, the registry) stays in the plugin's data folder,
`~/.claude/plugins/data/`, across updates.

Then ask, for example: *"What has the City of National City, California approved since January 2025?"*

Settings, asked at install and changed with `/plugin configure public-records@public-signals`:
| Setting | Default | What it does |
|---|---|---|
| Browser | `playwright` | For portals that refuse plain HTTP: `playwright` (a Chromium of its own; the tools give the one-line install the first time it is needed), `openclaw` (the browser your OpenClaw runs), or a Chrome remote-debugging address |
| Jev | `off` | `api:vercel` labels items with Jev through the Vercel AI Gateway |
| Vercel AI Gateway key | empty | Only for Jev; kept in your system's credential store |
| Search | Parallel's keyless search server, then Claude's own web search | Comma-separated, tried in order: `mcp:<url>` (any MCP search server), `api:parallel`, `api:exa`, `agent`, `off` |
| Scrape | Firecrawl's keyless server (trial limits), then off | For pages a plain fetch cannot read: `mcp:<url>`, `api:firecrawl`, `api:treg` (Firecrawl through treg), `off` |
| Parallel, Exa, Firecrawl keys; treg token | empty | Only for the matching `api:` setting; kept in your system's credential store |

Outside Claude Code the same settings are environment variables (`DDWB_SEARCH`, `DDWB_SCRAPE`, `DDWB_JUDGE`, `DDWB_BROWSER`) or a `providers.toml` (see `providers.example.toml`). Keys are read from the environment only: a key written into a settings file is refused.

## What a run looks like
The master skill (`skills/public-records/SKILL.md`) drives these tools, in this order; each prints the next step:

| Stage | Tools |
|---|---|
| Find the body | `start` |
| Find its meetings | `portal` (detect, meetings, documents); else `fetch` the homepage, `check` a page, `accept` the list; `search` or `scrape` (paid) only when those fail |
| Map them | `map`, `links` |
| Read | `read` (minutes first), `peek`, `passages`, `passage`, `jev` |
| Decide | `record`, `unrecord`, `skip`, `unreachable` |
| Finish | `next`, `status`, `answer` |

Three sub-agents split the work (`agents/`): a mapper, a reader and a verifier, each allowed only its stage's tools.

## Other ways to use it
- **Any MCP client:** `uv run --project <this folder> public-records-mcp` (stdio). Tool names are the steps above;
  each takes `session` (`<run>:<arm>:<body>`, e.g. `r1:agent:jefferson-county-al`) and the step's own named parameters
  (`start`: `query`; `fetch`: `target`; `record`: `passage`, `type`, `action`, `what`, ...).
- **Command line:** `uv run python -m ddwb.deep.tools <session> <step> ...`, the same steps.
- **OpenClaw:** the skills are plain `SKILL.md` files, and the tools can use the browser OpenClaw runs
  (`DDWB_BROWSER=openclaw`). OpenClaw is an option, never a requirement.

### The browser, for portals that refuse plain HTTP
Most portals answer a plain, honest HTTP client (User-Agent `public-signals-research/0.1`). Some don't: BoardDocs
(CloudFront) and Simbli (Imperva) wall it, IQM2 hosts never answer it, eScribe's Cloudflare walls some regions. The
tools then move that host to a browser with a person's settings, which still stops at any CAPTCHA or challenge that
asks for an answer and never solves one. Which browser, by `DDWB_BROWSER` (the plugin's Browser setting):
- unset or `playwright`: a Chromium of its own: `uv sync --extra browser`, then `uv run playwright install chromium`.
- `openclaw`: the browser your OpenClaw runs (a real Chrome with OpenClaw's own profile, not your personal one),
  started with `openclaw browser start` when it is not running. No Chromium download.
- a CDP address (`http://127.0.0.1:9222`): any Chrome you started with `--remote-debugging-port`.

Attached to a browser someone else runs, it opens a tab of its own, leaves the other tabs and settings alone, and only
disconnects at the end. Hosts moved to the browser are remembered for a week, then plain HTTP is tried again.

## Limits
- US public bodies only, and meetings from 2025-01-01.
- Scanned PDFs (OCR) and Word documents are read on macOS only; elsewhere they are reported as unread.
- A portal behind a challenge that asks for an answer is reported as unreachable, never worked around.
- Paid steps (Jev, `search`, `scrape`) run only within the caps a run starts with.

## For developers
`uv sync --extra browser`, `uv run pytest`. The package is `src/ddwb`: `registry` (find the body), `portals` (one
adapter per platform), `deep` (the navigation tools), `structure` (documents into items), `pipeline` (the same steps
run by code, for the benchmark), `mcp_server`. The sections below describe the data layers the registry is built from.

## Layers
Data moves down these layers and only down. A judgement never sits in a table of facts.

| Dataset | Holds | Rule |
|---|---|---|
| `raw` | each agency's file exactly as published, every value as text, one table per file or tab | nothing renamed, cast, mapped or dropped, except people data (SAM points of contact and sole proprietors, the .gov list's contact e-mails), which is never kept; each row says its file, tab, row, file sha256 and edition |
| `standard` | one row per source record, under this repo's column names, and the code tables used | only mechanical steps: copy, cast, split, join codes, look up a code table copied from the agency's documentation |
| `labels` | judgements: what is labelled, the label, the method and version, the evidence | nothing here is a fact; each consumer names the label set it uses |
| `entities` | bodies, identifiers, relations | built from `standard` plus the label sets chosen, and that choice is recorded |
| `meta` | `columns` (the catalogue), `runs`, `stale` | |

**The catalogue** (`meta.columns`, defined in code next to each table) says, for every column: what it means, where it comes from, and how it is made (`copy`, `cast`, `split`, `join_codes`, `code_table`, `id_lookup`, `label`, `bookkeeping`). A build fails if a table and its catalogue entry disagree.

**Changes carry downstream:** `ddwb.build` runs every step in dependency order; `meta.runs` records what each table was built from; `meta.stale` lists any table built from an older run of its inputs.

## Pieces
| # | Piece | Tables |
|---|---|---|
| 1 | Foundation and the Census list (`ddwb/raw/census.py`) | `raw.census_gus_2026_*` (5 tabs, 97,390 governments), `meta.columns`, `meta.runs`, `meta.stale` |
| 2 | The other raw sources (`ddwb/raw/files.py`, expected headers in `ddwb/raw/headers/`) | `raw.nces_lea_directory_2025_26`, `raw.nces_school_directory_2025_26`, `raw.epa_sdwis_water_system`, `raw.epa_sdwis_geographic_area`, `raw.imls_pls_fy2024_administrative_entity`, `raw.usfa_registry`, `raw.ntd_reporters_2025`, `raw.census_finance_2022_units` |
| 3 | Standard records (`ddwb/standard/records.py`, code tables in `ddwb/standard/codes/`) | `standard.records` (209,438 records from 12 lists; scope in the module's docstring), `standard.code_lists`, `standard.code_states`, `standard.code_entity_types` |
| 4 | Links the agencies publish (`ddwb/standard/links.py`), and the Census F-33 file that links Census school systems to NCES districts | `raw.census_f33_2024` (14,077 school systems), `standard.links` (25,916 links: Census parents, F-33 Census-NCES links, a charter school's district and authorizer, NTD reporting agencies; matched to records by exact ID only) |
| 5 | Websites from outside sources, pass 1 (`ddwb/raw/sam.py`, `ddwb/standard/site_claims.py`, `ddwb/labels/websites.py`) | `raw.sam_entity_public_2026_10` (SAM.gov public extract: US governments and tax-exempt bodies, no people data), `raw.cisa_dotgov_domains`, `raw.texas_spd_entity`; `standard.site_claims` (59,955 websites with the body's name and place as published); `labels.website_matches` (which record each claim belongs to: exact UEI, or normalised name and place, accepted only one-to-one) |
| 6 | Entities (`ddwb/entities/bodies.py`) | `entities.bodies` (195,583 bodies: one per record, except where an agency says two records are one body: Census F-33 school links, NTD's repeated IDs), `entities.body_records` (which records form each body and why), `entities.identifiers` (every ID a body carries, with its issuer), `entities.relations` (links between bodies, accepted only when an agency publishes them; parents come only from these), `entities.websites` (every known website, from the agencies' lists and accepted labels) |

## Build and look
- Build everything: `uv run python -m ddwb.build`, then `uv run pytest -q`.
- Open in Chrome: `./ui.sh` (read only). Close it before a build: it locks the warehouse file.
- Providers (search, scrape, Jev): which vendor or MCP server does each, keys as `${ENV_NAME}` references only, and per-run limits: `providers.example.toml`; see what is in force with `uv run python -m ddwb.providers show`.
- Where things live (`ddwb/paths.py`): the data root `DDWB_DATA_DIR` (default `data/`, not in git) holds what this repo writes for itself; the work root `DDWB_WORK_DIR` (default the folder that holds the repo) holds downloaded documents (`data/deep/`, listed in `data-manifest.tsv`), the agencies' downloads (`data/registries/`, read only; `DDWB_DOWNLOADS` points elsewhere) and run notes (`work/deep-discovery/`). Stores keep document paths relative to the work root.
