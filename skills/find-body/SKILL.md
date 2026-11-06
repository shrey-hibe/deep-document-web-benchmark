---
name: find-body
description: Resolve which US public body a question is about (a county, city, town, school district or special district) to one registry entry with its website, before looking for its meetings. Use when a name is ambiguous (Springfield, Washington County), when only a district or service is named, or when the website is unknown.
---

# find-body

1. `start --query "<name>, <state>"` on a new session (`<run>:<arm>:<body-label>`). It resolves against the registry of
   US public bodies and prints the card (name, kind, state, city, county), the website on record and LIKELY PORTALS.
2. Ambiguous: `start` stops and lists the candidates; pick by kind and place, then `start --body-id <id>`.
3. No website on record, or a dead one (registry sites can be stale; `start` lists every one it knows): `search
   "<name> board meeting agendas"`, or, when no search provider is set up, your own web search; take the body's own
   site, not a directory, a vendor or a same-name body elsewhere (`check` shows whose page it is).
4. Hand the session to find-meetings (or the public-records-mapper agent).
