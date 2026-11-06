---
name: public-records
description: Answer questions about what a US public body (county, city, town, school district, water, fire, transit or library district) bought, contracted, put out to bid, funded or decided, or who it hired or appointed, from the body's own meeting records (agendas, minutes, packets). Use when someone asks what a council, board of education, commission or district board approved or spent on, or wants buying signals or people changes from public meetings.
---

# public-records

You answer from the body's own records, with what you covered and what you could not reach. The `public-records` MCP
server gives you one tool per step; three sub-agents do the stages (defined from `src/ddwb/policy.toml`).

## The flow
1. **Name the body.** `start` with `--query "<name>, <state>"` (or `--body-id`); it resolves the body in the registry, prints
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
5. **Reply** with the answer's summary: the decisions (date, type, outcome, vendor, amount, source link and quote), what
   was covered, and what is incomplete and why.

## Rules
- Paid calls (Jev, search, scrape) stay within the run's caps; anything beyond needs the user's yes with the cost.
- Websites: an honest research User-Agent for plain fetches, a person's pace per host, back off on errors; stop at any
  CAPTCHA or challenge that asks for an answer and never solve one. A site that refuses plain HTTP is read through the
  browser tier automatically (a person's browser, same pace), which still stops at challenges.
- No search key set up: use your own web search to find a body's site or portal, then hand the URL to `fetch` or
  `portal detect`; the tools do the reading.
- A failure is never "nothing there": say what failed and mark it unreachable with the reason.
- People named in public records are in scope: record names and roles as the record shows them.
