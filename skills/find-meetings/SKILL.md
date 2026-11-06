---
name: find-meetings
description: Find where a public body's governing board (council, commissioners, board of education, district board) publishes its meeting agendas, minutes and packets, on its own site or on a meeting portal (BoardDocs, Legistar, CivicClerk, Granicus, BoardBook, Simbli, eScribe, Streamline, CivicWeb, IQM2, AgendaCenter, PrimeGov, NovusAgenda, BoardOnTrack, Google Drive). Use after the body is resolved.
---

# find-meetings

1. `fetch <homepage>`: links ranked toward the board's meetings; portals linked or embedded on the page are named. A
   site that refuses plain HTTP is read through the browser tier on its own; only a challenge that asks for an answer
   stops it (then look for the same documents on the body's portal or another of its sites).
2. `portal detect <url>`: is this a known portal, and which client? A known portal is mapped through its adapter.
3. LIKELY PORTALS (from `start`) is where bodies like this one usually publish: a hint for where to look first, never
   a list to stop at; most bodies publish on their own sites. A web search for "<body> <portal> agendas" (the `search`
   tool, or your own) often finds the portal's address; `portal detect` it.
4. `check <page>` on each candidate list: whose meetings, how many dated documents, which years. `accept` every list the
   governing board uses (agendas and minutes can be separate lists; another year can be another view).
5. Committees, planning commissions and other boards are not the governing board: skip them with the reason.
