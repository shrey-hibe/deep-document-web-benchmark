# Findings

The wide tier asks one question of every US local public body: where does it publish? Free data answers it for many
bodies. For the rest, a search engine proposes pages and Jev, a small judge model that answers typed questions with
probabilities, picks the body's page. Every number below was graded blind, against labels the system never saw.

## In one table

Pages found on the 85 bodies where some engine or the checker found a page (graded with adjudication and audit; 95%
intervals):

| Setup | Pages found | Cost per page found |
|---|---|---|
| Exa + Jev | 0.82 (0.73-0.89) | about $0.015 |
| Parallel + Jev | 0.74 (0.64-0.82) | about $0.003 |
| Parallel first, Exa only when Parallel picks nothing | as many as Exa (70 vs 70) | about $0.0095, 62% of Exa's |

**Pair cheap search with a small judge and you keep most of premium search's finds at a fifth of the cost.** Exa finds a
little more under every grader, but at this size the lead is suggestive, not settled. The cost gap is large and certain.

## The judge
- **Ask whose site a page is, not whether it is official.** Choosing among "this body", "the government that runs it"
  and other options gave precision 0.97 and recall 0.97. Asked "is this the official site?", Jev says yes to the parent
  government's site too: precision 0.85, recall 1.00.
- **Never decide from a page that did not load.** On 13 pages that never loaded (bot checks, empty pages, certificate
  errors) Jev still said "own site" 12 times, from the name and address alone. Such pages are now "unverified", never found.
- **Its uncertainty shows where it should:** on an empty page it picked "empty or error" with low confidence. Each call
  reports its exact cost; a call takes 0.4-1.7 s end to end, of which Jev itself is about 60-80 ms.

## Free sources
- **How often a source's website is truly the body's own** (blind labels, readable pages): the .gov list 8 of 8, Wikidata
  8 of 8, SAM 7 of 7, state lists 6 of 8, the agencies' own lists 3 of 6, mail domains 1 of 8.
- **Mail domains usually lead to the government that runs the body**, such as a city's site for its water system: the
  right place to look, not the body's own site.

## Search
- **Both engines have the body's own site in their top 5 equally often** (0.93). Exa ranks it first far more often (0.90
  against 0.63). Once the judge picks from the five, Exa's lead shrinks to about 8 points.
- **Only about half the result pages load** (212 of 454 in one sample): bot walls and timeouts. Those stay unverified.
- **Most wrong picks are a nearby government's page that never names the body**: a township on the next city's site, a
  district under the county. The rest are namesakes in another state and directories.
- **A free check that the page names the body looked good, then failed on fresh bodies.** It removed 5 right picks and
  2 wrong ones. Registry names are not how bodies name themselves, so the check was dropped.

## Grading: does the answer depend on who grades it?
Truth came in four steps; each could have overturned the result, and none did. Scores count the page where the body
publishes, its own or its governing body's.

| Grading | Bodies with a page | Exa + Jev | Parallel + Jev | Only Exa / only Parallel | Paired p |
|---|---|---|---|---|---|
| A web checker (a separate model with web search) | 51 | 0.65 | 0.61 | 4 / 2 | 0.69 |
| + blind adjudication of the 63 disagreements | 84 | 0.83 | 0.73 | 14 / 5 | 0.06 |
| + an audit of a random sample of agreements | 85 | 0.82 | 0.74 | 12 / 5 | 0.14 |
| A second judge on a different model, instead | 91 | 0.81 | 0.71 | 14 / 5 | 0.06 |

- **Every judge quote was found word for word in the page text it was given** (93, 99 and 194 quotes across the steps),
  so each label can be reproduced from its inputs.
- **The two judge models agree on page verdicts at κ 0.86.** 9 of their 10 disagreements involve a low-confidence label,
  most often a county or state page that never names the body.
- **The checker's "nothing found" hid real pages**: where it was blocked, 18 of 22 bodies had one.

## Fresh bodies
50 new bodies, drawn after the rules above were set. The main result held: Exa + Jev found 21 of 27, Parallel + Jev 19
of 27, the same 8-point gap. Parallel first, then Exa, found 22. Precision was lower than on the first samples (0.70 and
0.68 against 0.78), with 9 wrong picks each in 50.

## Relations between bodies
- **A transit reporter's SAM link joins a body to itself, not to its parent**: an agency named after a city is the city.
- **Name and ZIP, a published page and the library rule agree with the judge 94-98% of the time**; name and state alone
  86%, "named after its government" 76%. Precision per rule waits for blind labels.

## Engineering lessons
- **A network fault looks like dead websites.** A guard now checks known-good hosts before a batch records a failure.
- **Hosting challenges vary** ("One moment, please..." as well as "Just a moment"), so the wall check has to know both.
- **Every logged answer maps back to its call, item and input** (`tests/test_eval_integrity.py` checks each finished run).

## The deep tier
The deep tier reads a body's meeting records to find what it bought and decided. It is tested on held-out bodies, never
the ones the code was written on; results will follow here.

## Limits
- **Small samples.** 150 bodies were searched, one query each, top 5 results, at list prices on 31 Oct 2026.
- **Pooled recall.** "Found" is measured against pages some engine or the checker found, not every page that exists.
- **Model graders.** Every grader is a model; there is no human-labelled sample yet.
- **Own site or the government's page.** Graders split on a water system named after its city, so scores count either.
