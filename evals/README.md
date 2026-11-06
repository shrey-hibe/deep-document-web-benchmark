# Wide-tier evaluation: Jev and search on frozen samples

Samples are drawn once by `ddwb.evals.samples` (deterministic, seed `wide-tier-2026-10-31`) and saved here as ID lists, so they
never move when the data changes. Every sample leaves out the frames held back for the final tests: all California bodies
(set B) and cities and towns in TX, IL, NY and OR (set C).

| Set | File | What | Flow | Paid calls |
|---|---|---|---|---|
| **W** website found | `samples/W.csv` (after the live check) | 30 bodies per website source whose chosen website answered and is not junk | Jev reads the body's facts and the page. **W1:** title, description, site name, text excerpt, link texts. **W2:** W1 plus the site's sitemap (a free fetch for these hosts) | Jev |
| **S** had a website, all dead | `samples/S.csv` (after the live check) | 50 bodies whose every known website failed the live check | search, free fetch of the top results, Jev picks the body's site | search + Jev |
| **N** never had a website | `samples/N.csv` | 100 bodies with no website from any source: water (local government) 25, fire 25, special districts 20, towns 15, libraries 15 | triage first (below; its prediction is recorded), then search for every body in this first test, free fetch of the top results, Jev picks the body's site | search + Jev |
| **R** relation rules | `samples/R.csv` | 50 candidate links per rule in `labels.relation_candidates` (6 rules) | Jev reads both records and, where known, both websites' pages: same organisation? does B run A? | Jev |

**Triage for N, step by step** (2026-10-31):
1. **Base rates (free):** the share of similar bodies with a known website, by kind, Census function and size. Examples: libraries 83%,
   fire districts 29%, drainage and cemetery districts 5%; water systems under 1,000 people 14%, 10,000-100,000 58%. These measure
   'website in our sources', so they rank groups rather than give absolute odds.
2. **Parent check (free):** where a run-by candidate's government has a working site, look for the body's name in that site's homepage
   links (`web.links`) and sitemap. A hit gives the body's page with no search.
3. **Jev on a fact card,** three narrow yes/no questions: run by a city or county? volunteer or nonprofit? big enough to keep its own
   site? The card holds function, owner or legal basis, volunteer or career, people served or stations, name clues, the parent
   candidate and whether its site mentions the body, and the base rate.
4. **Learn from outcomes:** in this first test every N body is searched whatever triage says, and triage's prediction is recorded next
   to what search finds. The triage rule used at scale (search, skip, or parent first) comes from that.

Search plus Jev picking the result is the same pattern as the board-portal route (W5 in `BENCHMARK.md`): search proposes, Jev keeps
only the body's own.

**Ground truth:** a separate, blind checker (a subagent that reads the actual pages and never sees our method, source or Jev's
answer) labels every item. The build does not grade its own matching (decision #85).

**What we measure:**
- precision per website source and per relation rule, at Jev's probability cut-offs (coverage vs accuracy, calibration);
- W1 vs W2, to see whether the sitemap is worth fetching at scale;
- for S and N: how often search finds the body's site, how often Jev's pick is right, and the cost per body found;
- for N: whether triage predicts which bodies search can find, which is what saves search spend at scale.

**Questions Jev answers** (typed; probabilities out; options asked twice in reversed order to catch first-option bias):
- W, S, N: *Is this page the official website of {body}, a {kind} in {city}, {state}?* (yes/no), and *Whose site is this?* (choice:
  this body; the government that runs it; a different public body; a company, directory or law firm; unclear).
- N triage: *Where would {body}'s public notices be published?* (choice: its own website; its parent government's website; none, it is
  a private or volunteer body; unclear).
- R: *Are A and B the same organisation?* and *Does B run A?* (yes/no each).

All paid calls go through the provider layer (`ddwb.providers`), pinned to what each test measures: Jev on the Vercel AI
Gateway, search on the Parallel and Exa APIs, with keys from the environment (`PARALLEL_API_KEY`, `EXA_API_KEY`,
`AI_GATEWAY_API_KEY`). Each run has a dollar budget (`--budget`); who approved it, if given (`--approved-by`), is
recorded with the run.
