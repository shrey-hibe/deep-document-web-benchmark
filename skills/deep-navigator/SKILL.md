---
name: deep-navigator
description: Find what a US public body (county, city, school district, special district) has bought or committed to buy, by navigating its own public meeting records with the nav tools. Use when asked what a public body bought, approved, put out to bid or funded, from its agendas, packets and minutes.
---

# Deep navigator

You answer one question about one public body, from its own meeting records:

> What has the body bought or committed to buy since 2025-01-01: contracts, renewals, purchases, bids, grants and budget
> decisions, each with vendor, amount and date?

You do it with the `nav` tools only. They are the plumbing: they fetch, read and record, and they keep the state. You make
the decisions: where the board's meetings are listed, which items and documents could hold buying decisions, how deep
to read, and what each decision was.

Run every tool as `skills/deep-navigator/nav <session> <tool> [args]`, with the session you were given
(`<run>:<arm>:<body>`). Start with `start`. A benchmark body needs nothing more; for any other body, name it in words,
`start --query "Cortez, CO"` (its name and state; add its kind or county when the name is common), and if the registry
finds several bodies, `start` lists them and you start again with `--body-id <id>`. Whenever you are unsure where you are,
or after a long pause, run `next`: the store remembers everything you did; your context does not have to.

## Rules (the tools enforce them; you follow them too)

- The website rules, word for word: the User-Agent `public-signals-research/0.1` with no personal info; pace each host and
  back off on errors; stop at any CAPTCHA or bot wall and never solve one; no paid calls beyond the run's approval and
  caps (who approved the run, `start --approved-by`, and the CAPS line `start` prints); downloads need no approval but
  are paced like a person and logged in the data manifest.
- Reach the web only through `nav`: no other fetch, curl, browser or search tool. Paid calls (search, scrape, Jev) are
  already approved within the run's caps; the tools stop when a cap is reached.
- Run every `nav` call in the foreground and wait for its output, however long it takes. Never end your turn to wait for
  something: ending your turn ends your run.
- A tool that fails tells you so. A failure is never "nothing there": try another way (another tool, `scrape` instead of
  `fetch`, another page), or mark the item `unreachable` with the reason. An answer with unreachable parts is incomplete,
  and that is fine; a silent gap is not. Incomplete is only for what you cannot reach: work that is merely large is
  never a reason to stop. Keep going until `next` says STAGE answer, however many meetings or passages are left.
- Record names as the record shows them, people included: a vendor who is a person is recorded by name.
- Read only what the tools print: they read what they need.

## The workflow

| Stage | You decide | The tools do |
|---|---|---|
| Reach | Is this the body's own site? | `fetch` the homepage from `start` (free). It shows the page's links, each with an ID: with Jev on, every link ranked by Jev's p that it leads to the board's meetings (those at p ≤ 0.2 folded into a count); otherwise those that look like meetings, boards or documents. `--all-links` shows every link |
| Locate | Which pages could be the governing board's meetings list; which to check | `check <page>`: counts dated meeting documents on it (and Jev's verdict, if on) |
| Accept | Is it the governing board's list (council, board of commissioners, trustees, board of education)? | `accept <page> --reason`, or `skip <page> --reason`. Accept every list the board uses (agendas and minutes may be separate) |
| Map | — | `map <list>`: every meeting and document link dated from 2025-01-01, on every view of the list (years, months, portal pages). A list on a known portal, or a page that links or embeds one, is read through the portal's own service (below); Diligent and Destiny through theirs. `map --plain` reads a list as a page only |
| Triage | Which links could hold buying decisions; which to open; how deep | `links` to page through them; `peek` for a first look; `read` to read; `skip` with a reason for the rest. Opening a meeting's or an item's page (`peek` or `read`) adds its own documents (items, attachments) to the map |
| Read | Which pages of a long packet to read | `read <link> [--pages 4-19]`: splits the text into items. A meeting's minutes come first: `read` on its agenda, packet or an item refuses while the map lists minutes for that meeting that nobody has read (the minutes record the decisions; a packet is the paper behind them); `--anyway` reads it, for an amount the minutes leave out. Once a meeting's minutes are read, its agenda, packet, item and video links are covered (skipped by the tools, `covered_by` the minutes); a meeting with no minutes listed is covered the same way by its agenda. A covered link still opens with `--anyway`. Each item read carries its number and section ("23.1" under "23. CONSENT CALENDAR"). With Jev on, Jev labels every item and those at p ≤ 0.2 are set aside for you (counted and listable); otherwise items with a dollar amount, a buying word or a decision word are kept (`--all-items` keeps all) and the rest counted. PDFs, web pages and Word or RTF files are read; image-only pages are read by OCR; a format the tools cannot read is reported as such |
| Extract | Is a passage a buying decision; its type, outcome, vendor, amount | `passages`, `passage <P>` (with the events already recorded from it), then `record <P> ...` for each decision, `skip` for the rest. `unrecord <E> --reason` takes back an event recorded by mistake. Recording an item sets aside the passages of the same item (same number, same opening words) in the meeting's other documents: one decision, one event; `unrecord` reopens them. An event takes its document's date (the map's, else one found in its title, file name or first page); for a document with no date, `record` asks for the meeting's date as the document states it (`--date YYYY-MM-DD`, or `--date unknown`) |
| Answer | — | `answer`: your recorded events, with coverage computed by the tools (what was mapped, read, skipped, unreachable) |

A document can be opened wherever you saw it: on a mapped list, on a page you fetched, or in search results (`peek` and
`read` take its ID). One found off the mapped lists is counted as such; a document dated before 2025-01-01 is not opened.

You are done when the map has the board's meetings across the whole window (no stretch of more than 150 days without one),
every governing-board meeting on your map has at least one of its documents opened (peeked or read) and, where the map
lists its minutes, its minutes read (the minutes say what the board decided, an agenda only what it would consider),
every link is read, peeked and decided, skipped with a reason, or unreachable, and every kept passage is recorded or
skipped. `next` tells you what is left, starting with the board's meetings nobody has opened. The answer is marked
incomplete while the map has such a gap (a year view or a list is missing: find and map it), any of the board's meetings
is unopened or has its listed minutes unread, or anything is skipped unreviewed (below); the coverage counts links
skipped without being opened apart from those skipped after a look or on Jev's low score. You may skip in bulk: several
IDs at once, or `skip --rest --meeting 2025-03-11 --reason "..."`. A bulk skip decides only what you can have read: a
passage that may record buying (by the plain rule, or Jev's p above 0.2) counts as decided in a skip of several only if a
tool has shown it to you (`read`, `passages`, `passage`) and the skip names at most 20 passages (a page); otherwise, and
for the unopened minutes of a governing meeting whose minutes nobody has read, it is kept as `skipped_unreviewed`, and
the answer stays incomplete until you read and decide each, or skip it on its own with its reason. A reason says why, specifically ("proclamations and minutes approval only", "planning commission, not the
governing board"), so the skip can be audited.

### Portals

The tools know 16 meeting portals (Legistar, BoardDocs, Simbli, CivicClerk, CivicPlus AgendaCenter, CivicWeb, eSCRIBE,
Granicus, IQM2, NovusAgenda, PrimeGov, BoardBook, BoardOnTrack, AgendaOnline, Streamline, Google Drive folders). A
portal's page is often an empty shell built by scripts; the tools read the portal's own service instead.
- `fetch` and `check` say when a page is on a portal, or links or embeds one (`PORTAL ...`), and which of its links
  goes there. `check` on a portal's page counts its meetings through the portal (per year, the governing board's).
- `map` on such a list lists every meeting from 2025-01-01 to 60 days ahead with its documents (agenda, packet,
  minutes, video, files), the governing board's first, as the same links and meetings a plain list gives. For the
  governing board's meetings held so far it also keeps the portal's items on the meeting: their own numbers, titles,
  outcomes and files. A page that only links a portal is read as a page too.
- `portal detect <url|page>`: which portal, and its client id. `portal meetings <url|page|portal:client> [--board
  [words]]`: the meetings with their IDs (`--board` alone: the governing board's only). `portal documents <meeting ID
  or date>`: one meeting's documents and items, with each item's files added to the map (open them by ID). Use these to
  look before you map, to reach an item's own files (a staff report, a contract), or for a meeting whose documents the
  map did not list. `read` on a portal meeting's document points to it.
- Some portals refuse plain requests (BoardDocs, Simbli): the tools read them, and fetch their documents, through a
  browser with a person's settings and pace, and stop at any challenge. A `BOT WALL` or `PORTAL ERROR` is a failure,
  never "no meetings": mark what depends on it unreachable with the reason, or find the meetings another way.
- `PORTAL NOTES` are the portal's own remarks (minutes listed for 12 of 20 meetings, a calendar that ends in 2024):
  read them; they are not gaps. What the tools could not read is in the map's NOTES and keeps the answer incomplete.
- `start` prints LIKELY PORTALS: where bodies like this one most often publish. It is a hint for where to look first,
  never a list to stop at; most bodies publish on their own sites.

### Choosing where to look

- `fetch` (our own GET, free): always first, for any page.
- `scrape` (Firecrawl, about $0.005 a page): only when `fetch` cannot read the page or the page needs rendering. It
  fails on files over 50 MB and responses over 8 MB.
- `search "..."` (Parallel, about $0.001): an open search with an objective. In our tests it found the right site for
  73% of bodies, about five times cheaper per right find than Exa.
- `search "..." --engine exa` (about $0.007): found the right site for 83%. With `--domain <site>` it searches within one
  site.
- Searches return single documents (one agenda PDF) more often than the page that lists them all. A document is a clue
  to where the list is, not the list.
- Meeting lists are often under "Agendas & Minutes", "Board", "Council", "Government", "Meetings", or on a portal (the 16
  above, Diligent, Destiny).

### What counts

- Types: `contract` (new contract or agreement), `renewal` (renewal, extension, amendment), `bid` (authorising, issuing or
  awarding a bid, RFP or RFQ), `grant` (a grant or outside funding), `purchase` (a purchase, payment, invoice or claims
  list), `budget` (a budget, appropriation or transfer). Staffing decisions are not buying.
- Actions: `approved`, `to_consider` (on an agenda, not yet decided), `rejected_or_tabled`, `reported_only`, `unclear`.
- One event per decision per meeting. When the same decision is in a meeting's agenda and its minutes, record it once,
  from the minutes (they say what happened). A list of payments or claims approved together is one event (`purchase`,
  what = "claims list", amount = the total if stated).
- `--what` says what was bought or decided in a few words ("road resurfacing, Main St", "library e-book platform").

## When Jev is on

`start` says whether Jev is on in your session. If it is, Jev has already answered inside the tools:
- `fetch` shows `p=` for each link of the page: Jev's probability that it leads to the governing board's meetings, most
  likely first; links at p ≤ 0.2 are folded into a count (`--all-links` lists them).
- `check` shows Jev's probability that the page is the governing board's meetings list, and whose list it is.
- `map` and `links` show `p=` for each link: Jev's probability that it could record a buying decision. What a link
  opens comes from the map's rule; where the rule cannot tell, Jev's guess is shown, with its probability when unsure
  (`minutes 0.55`).
- `read` labels every item of the document, and `passages` shows, for each, its item number, Jev's probability that it
  is a buying event (with its type when Jev is sure of it, p ≥ 0.8), and `people` when Jev labels it a staffing or
  people decision (hire, departure, move, pay). People decisions are kept as their own label; they are not buying
  events. The outcome (approved, to consider, ...) and the type when Jev is unsure are yours to judge from the text. Items at p ≤ 0.2 are set aside by the tools: `passages --doc <K>
  --status skipped` lists them, and `record` works on one if you disagree.

Jev is sharpest on single agenda items, whose titles say what they are. On a whole meeting's agenda or minutes its
`p=` sits around 0.5–0.8, because any meeting may hold a purchase: there, decide by whose meeting it is and what it
opens, and `peek` the governing board's meeting pages to reach their items.

Trust Jev where it is confident, and look further where it is not:
- p ≥ 0.8 or p ≤ 0.2: act on it (open, or skip with a reason such as "Jev p ≤ 0.2: not a buying decision").
- In between: look further yourself (`peek`, `read`, `passage`) before deciding.
- `p=?` means Jev did not answer (a failure, shown with its error). It is unknown, never "no": look yourself.
- Jev gives probabilities, not words: vendor, amount, term and what was bought come from your own reading.

## Your final message

End with: the `answer` tool's summary (complete or incomplete, and why), how many events, and anything that blocked you
(a page that needed a browser, a list you could not find, a cap that was reached).
