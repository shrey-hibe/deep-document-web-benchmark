"""The structure layer's primitives: a Document of Items, each with its outcome, people, organisations and amounts, and
the meeting's attendance. Plain dataclasses; `to_dict` gives JSON-ready dicts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Outcome:
    """The motion and vote lines that decide an item, as written, with what could be read from them."""
    text: str
    mover: str | None = None
    seconder: str | None = None
    tally: str | None = None  # "5-0", as written or counted from the Yes/No (Aye/Nay) lines
    ayes: int | None = None
    nays: int | None = None
    result: str | None = None  # carried, adopted, approved, failed, tabled, withdrawn; None when not stated
    consent: bool = False  # the one motion of a consent section, copied to every item it decides
    from_item: str | None = None  # the item number the motion was written under, when copied


@dataclass
class Person:
    """Someone named in the attendance of a meeting (part: present, absent, staff) or in an item (part: moved,
    seconded, presented, appointed, hired, resigned, spoke, other). Name and role as written."""
    name: str
    role: str | None = None
    part: str = "other"


@dataclass
class Item:
    number: str | None  # the outline path: "VIII.B.1", "2.b", "22.10", "2025-108"; None for an unnumbered part
    section: str | None  # the title of the item or heading it sits under
    title: str  # first line, at most 250 characters
    text: str  # the item's full text, across page breaks
    pages: tuple[int, int]  # first and last
    kind: str = "item"  # item | section (has items under it) | attachment (backup filed under an item) | front
    procedural: str | None = None  # call to order, roll call, pledge, minutes, agenda, public comment, executive session,
    # adjournment, "nothing recorded" ("5. PUBLIC HEARINGS: None"): kept, tagged, not sent to the buying question
    outcome: Outcome | None = None
    amounts: list[float] = field(default_factory=list)
    orgs: list[str] = field(default_factory=list)
    people: list[Person] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)  # resolutions and ordinances its title names ("ordinance 1360")

    @property
    def item_no(self) -> str | None:  # the name text.same_item reads
        return self.number

    def own_text(self) -> str:
        """The text without its title line and outcome, to judge whether a section says anything of its own."""
        body = self.text.split("\n", 1)[1] if "\n" in self.text else ""
        if self.outcome and not self.outcome.consent and self.outcome.text in body:
            body = body.replace(self.outcome.text, "")
        return body.strip()


@dataclass
class Document:
    items: list[Item]
    attendance: list[Person]
    dropped: dict[str, int]  # lines dropped, by reason: header/footer, page number, boilerplate
    quality: dict
    source: str  # "bookmarks" or "text"

    def buying_items(self, procedural_max: int | None = None) -> list[Item]:
        """The items worth the buying question: items and sections that say something of their own (a bare section
        heading's items carry its content), not procedural. Attachments (backup filed under an item, read on demand)
        are not sent; the part before the first item (front) only when it records a motion or, read by text rules, holds
        more than a title block (a caucus or a hearing written before the call to order)."""
        def sent(it: Item) -> bool:
            if it.kind == "attachment":
                return False
            if it.procedural:
                # a procedural tag holds only for a short item with no money and no buying words: in minutes that print
                # no item numbers, "called to order" can open the only item and hold the whole meeting (held-out check,
                # 2026-11-06: 52 true decisions lost that way in two bodies)
                return substantive(it, procedural_max)
            if it.kind == "front":
                return it.outcome is not None or self.source == "text" and len(it.text) >= 300
            return it.kind == "item" or len(it.own_text()) >= 200
        return [it for it in self.items if sent(it)]

    def attachments_of(self, number: str) -> list[Item]:
        return [it for it in self.items if it.kind == "attachment" and it.number == number]

    def to_dict(self) -> dict:
        return asdict(self)


PROCEDURAL_MAX = 600  # characters: a "procedural" item longer than this holds more than procedure


def substantive(it: Item, procedural_max: int | None = None) -> bool:
    """A procedural-tagged item that holds a decision anyway: long (over the body's procedural_max, else PROCEDURAL_MAX),
    or carrying an amount and a buying or decision word."""
    from ..deep import text as deep_text
    body = it.text or ""
    return len(body) > (procedural_max or PROCEDURAL_MAX) or bool(deep_text.MONEY.search(body) and deep_text.could_be_buying(body))
