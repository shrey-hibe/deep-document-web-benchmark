---
name: map-records
description: Map every meeting and document a public body's governing board has published in the window (agendas, minutes, packets, items), from accepted lists or portal adapters, and check the map has no gaps. Use after the meetings lists are found and before reading.
---

# map-records

1. `map <list>` on each accepted list: every view (years, months, pages) is read; on a known portal the adapter lists
   meetings and documents directly, with its own per-year counts and notes.
2. `next` reports gaps (stretches with no governing meeting), lists checked but not accepted, and meetings with no
   document. A gap means a view or a list is missing: find it, or say why the board did not meet.
3. `links` shows what was mapped: kind (minutes, agenda, packet, item), date, board.
4. Hand the session to read-records.
