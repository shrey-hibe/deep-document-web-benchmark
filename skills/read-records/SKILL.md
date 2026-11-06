---
name: read-records
description: Read a public body's mapped meeting records and record every buying decision (purchase, contract, renewal, bid, grant, budget) and people decision with its outcome, vendor, amount and quote, meeting by meeting, minutes first. Use after the map is complete.
---

# read-records

1. `next` names the meeting to read: its minutes first (they record what was decided), else its agenda; once the minutes
   are read, the meeting's agenda, packet and items are covered (`read <K> --anyway` opens one for an amount).
2. `read <link>`: the document becomes items (number, section, outcome, amounts, people); Jev labels each and sets aside
   the clear non-decisions (`passages --status skipped` lists them).
3. For each item left: `record <P> --type ... --action ... --vendor ... --amount ... --what ...` with the minutes' own
   words, or `skip <P> --reason "..."`. One event per decision; a consent agenda's vote applies to each of its items.
4. Keep going until `next` says STAGE answer; never stop because the work is large.
5. For a large body, the code-driven pipeline does this stage with a cheap model and a check pass:
   `uv run python -m ddwb.pipeline run --map-from <session> ...` (a live run needs the user's yes with its cost).
