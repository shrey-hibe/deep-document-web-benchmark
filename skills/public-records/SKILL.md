---
name: public-records
description: Answer questions about what a US public body (county, city, town, school district, water, fire, transit or library district) bought, contracted, put out to bid, funded or decided, or who it hired or appointed, from the body's own meeting records (agendas, minutes, packets). Use when someone asks what a council, board of education, commission or district board approved or spent on, or wants buying signals or people changes from public meetings.
---

# public-records

You answer from the body's own records, with what you covered and what you could not reach. The `public-records` MCP
server gives you one tool per step; three sub-agents do the stages (defined from `src/ddwb/policy.toml`).

## The flow
1. **Name the body.** `start` with `--query "<name>, <state>"` (or `--body-id`; a body the registry lacks: `--name`, `--state`, `--site`); it resolves the body in the registry, prints
   its card, its website and LIKELY PORTALS for bodies like it (a hint for where to look first, never a list to stop at).
   The session id is `<run>:<arm>:<body-label>`, e.g. `20261106-ask:claude:wake-county-nc`.
2. **Find and map its meetings:** hand the session to **public-records-mapper**. It fetches the site, checks candidate
   lists, accepts the governing board's list(s) and maps them. A list on a known portal (BoardDocs, Legistar, CivicClerk,
   Granicus, BoardBook, Simbli, ...) is mapped through the portal's adapter.
3. **Read and decide:** hand it to **public-records-reader**. Each governing meeting's minutes first (else its agenda),
   items labelled by Jev, every item decided with a reason, until `next` says STAGE answer. For a large body you can run
   the code-driven pipeline instead: `uv run python -m ddwb.pipeline run ...` (needs the user's yes for its cost).
4. **Check and answer:** hand it to **public-records-verifier**: coverage, items Jev rated likely but nobody recorded,
   then `answer`.
5. **Reply** from `answer`'s output: each decision with its date, what, vendor, amount and term, its `source_url` and a
   short `quote`, exactly as `answer` gives them. A claim with no link and quote behind it is not made; say what you
   could not confirm instead. Then what was covered (meetings, dates) and what is incomplete and why.

## Running the stages
- **Wait for each sub-agent's result before the next step, and never reply while one is still working.** Ending your
  turn stops a running sub-agent (in a headless run the user then gets nothing).
- **Use the tools for anything about a body's decisions or people:** `start`, map, read. Your own web search and fetch
  only find a site, or fill a gap the tools name; they are never the answer's source in place of the minutes.
- **Keep going until `next` says STAGE answer, or a tool prints STOPPED (a cap reached).** Never stop for budget unless a
  tool said so; report how much of each cap was used.
- **A question about a topic** (meters, student transport, an ERP): read the meetings most likely to hold it first (the
  links Jev rates likely, and those whose titles name the topic), then the rest newest first, until coverage or the caps.
- **A question about an area** (the cities in a county): say which bodies you covered and which you did not, and why.
- **A name that fits several bodies:** ask the user which one; do not search their mail or calendar to guess.

## Rules
- Paid calls (Jev, search, scrape) stay within the run's caps; anything beyond needs the user's yes with the cost.
- Websites: an honest research User-Agent for plain fetches, a person's pace per host, back off on errors; stop at any
  CAPTCHA or challenge that asks for an answer and never solve one. A site that refuses plain HTTP is read through the
  browser tier automatically (a person's browser, same pace), which still stops at challenges.
- No search key set up: use your own web search to find a body's site or portal, then hand the URL to `fetch` or
  `portal detect`; the tools do the reading.
- A failure is never "nothing there": say what failed and mark it unreachable with the reason.
- People named in public records are in scope: record names and roles as the record shows them.
