"""The structure layer (src/ddwb/structure): small excerpts of the four development bodies' minutes. A rule prompted by
one body is tested on another body's text too."""

import json
import re
from pathlib import Path

import pytest

from ddwb.deep import text
from ddwb.structure import Bookmark, structure
from ddwb.structure import bookmarks as bm
from ddwb.structure.clean import clean_pages
from ddwb.structure.outcome import read_outcome
from ddwb.structure.people import attendance, procedural_tag

FIX = Path(__file__).parent / "fixtures" / "structure"


def pages(name: str) -> list[tuple[int, str]]:
    out, cur = [], None
    for line in (FIX / name).read_text().splitlines():
        if m := re.fullmatch(r"\[page (\d+)\]", line.strip()):
            cur = [int(m.group(1)), []]
            out.append(cur)
        elif cur is not None:
            cur[1].append(line)
    return [(p, "\n".join(ls)) for p, ls in out]


def by_no(doc, number, kind=None):
    hits = [it for it in doc.items if it.number == number and (kind is None or it.kind == kind)]
    assert hits, f"no item {number}: {[it.number for it in doc.items]}"
    return hits[0]


# ---------------------------------------------------------------- rule 1: clean pages
def test_running_footer_page_numbers_and_stamps_are_dropped_and_counted():
    lines, dropped, chars = clean_pages(pages("summit-minutes.md"))
    body = "\n".join(ln for _, ln in lines)
    assert "Docusign" not in body and "Page 2 of 4" not in body and "Council Minutes April 28" not in body
    assert dropped["boilerplate"] == 4 and dropped["header/footer"] >= 3
    # a footer glued to the last line of a page is cut off that line, the line itself stays
    assert any(ln.strip() == "Committee First Readings" for _, ln in lines)


def test_running_header_with_page_number_is_dropped_on_another_body():
    lines, dropped, _ = clean_pages(pages("cortez-minutes.md"))
    body = "\n".join(ln for _, ln in lines)
    assert "JUNE 10, 2025 PAGE 2" not in body and "PAGE 3" not in body
    assert "CITY COUNCIL REGULAR MEETING TUESDAY, JUNE 10, 2025" in body  # page 1's title is not a running line
    assert dropped["header/footer"] == 2


def test_vote_lines_are_never_dropped_as_running_lines():
    words = ["budget", "roof", "library", "grant", "lease"]
    pg = [(i, f"Header of the board\nThe {words[i - 1]} was discussed at length\nMotion carried 5-0") for i in range(1, 6)]
    lines, dropped, _ = clean_pages(pg)
    assert sum(1 for _, ln in lines if ln.startswith("Motion carried")) == 5
    assert dropped["header/footer"] == 5


# ---------------------------------------------------------------- rules 2, 4: items by text
def test_item_across_a_page_break_stays_one_item_with_its_title():
    doc = structure(pages("cortez-minutes.md"), kind="minutes")
    pres = by_no(doc, "4.a")
    assert pres.pages == (2, 3) and "dismantled in 1942" in pres.text and "Cultural Resources Survey" in pres.title


def test_outline_numbers_match_truth_ids():
    doc = structure(pages("cortez-minutes.md"), kind="minutes")
    assert text.same_item({"item_no": "2b"}, {"item_no": by_no(doc, "2.b").number})
    doc = structure(pages("pueblo-minutes.md"), kind="minutes")
    assert {"IV.A", "IV.B", "III.A", "III.B"} <= {it.number for it in doc.items}


def test_bare_letters_open_items_only_in_an_a_b_run():
    doc = structure(pages("pueblo-minutes.md"), kind="minutes")
    assert by_no(doc, "IV.A").title.startswith("A Approve Settlement Agreement")
    # on another body: "A Caucus was held ..." alone, and "A Resolution ..." inside a line, open nothing
    doc = structure(pages("summit-minutes.md"), kind="minutes")
    assert not any(it.number in ("A", "B") or (it.number or "").endswith(".A") for it in doc.items)


def test_unlabelled_headings_do_not_open_items_and_caps_ones_name_the_section():
    doc = structure(pages("summit-minutes.md"), kind="minutes")
    numbers = [it.number for it in doc.items if it.kind != "front"]
    assert numbers == [None, "2025-123", "2025-127", "2026-159"]  # the call to order, then the resolutions
    assert by_no(doc, "2025-123").section == "FIRST READINGS"
    assert "Committee First Readings" in by_no(doc, "2025-127").text


def test_a_statute_number_on_a_bold_line_is_not_an_item():
    doc = structure(pages("summit-minutes.md"), kind="minutes")
    res = by_no(doc, "2026-159")
    assert "9.64 and 149.433" in res.text and res.outcome and res.outcome.result == "adopted"
    # while a dotted item under its open section still opens (another body)
    doc = structure(pages("pylusd-minutes.md"), kind="minutes")
    assert {"12.1", "12.2", "12.3", "12.4", "22.1", "22.2", "23.1"} <= {it.number for it in doc.items}


def test_inline_bold_labelled_headings_split():
    doc = structure(pages("pueblo-minutes.md"), kind="minutes")
    assert by_no(doc, "VI").procedural == "public comment"
    assert by_no(doc, "VII").procedural == "nothing recorded"


def test_context_carries_the_outline_across_calls():
    pg = pages("cortez-minutes.md")
    ctx = {}
    structure(pg[:2], kind="minutes", context=ctx)
    second = structure(pg[2:], kind="minutes", context=ctx)
    assert second.items[0].number == "4.a"  # page 3 opens mid-item: the item goes on
    assert by_no(second, "6.a").section == "6. NEW BUSINESS"


# ---------------------------------------------------------------- rule 5: outcomes
def test_outcome_forms_of_four_bodies():
    o = read_outcome("22.1 x *Moved by:* Carrie Buck *Seconded by:* Todd Frazier **Aye** Leandra Blades, Todd Frazier, "
                     "Marilyn Anderson, Carrie Buck, and Tricia Quintero **Carried 5-0** Preferential Student Board Member Vote: Aye")
    assert (o.mover, o.seconder, o.tally, o.result) == ("Carrie Buck", "Todd Frazier", "5-0", "carried")
    o = read_outcome("*Ms. Ones made a Motion, seconded by Mr. DeHerrera to ratify the March 2025 bills. The Motion was approved.*")
    assert (o.mover, o.seconder, o.result) == ("Ms. Ones", "Mr. DeHerrera", "approved")
    o = read_outcome("Main Street, Cortez. Councilmember Dobry moved that the Consent Agenda be approved as presented. "
                     "Councilmember DeHaven seconded the motion, and the vote was as follows: DeHaven Dobry Lewis "
                     "Yes Yes No")
    assert (o.mover, o.seconder, o.tally, o.result) == ("Councilmember Dobry", "Councilmember DeHaven", "2-1", "carried")
    assert o.text.startswith("Councilmember Dobry moved")
    o = read_outcome("Ms. Walters moved to suspend Council Rule 8.01 (A). Voice Vote on the motion. Yes: Darrow, Dickinson "
                     "Motion passed-Voice Vote: 11-0 Ms. Walters moved to adopt. Motion passed-Voice Vote: 11-0 "
                     "Resolution 2025-123 was adopted.")
    assert (o.mover, o.tally, o.result) == ("Ms. Walters", "11-0", "adopted")
    assert read_outcome("A Resolution confirming the award of a contract with Geauga Highway Co.") is None
    assert read_outcome("Approved the Independent Contractor Agreement with Girls Inc.") is None
    assert read_outcome("M-002-2026 A Motion of Council confirming the reappointment of Traci Buckner.") is None


def test_consent_motion_after_the_last_item_decides_every_item():
    doc = structure(pages("cortez-minutes.md"), kind="minutes")
    for n in ("2.a", "2.b", "2.c"):
        o = by_no(doc, n).outcome
        assert o and o.consent and o.from_item == "2.d" and o.tally == "7-0"
    assert by_no(doc, "2.d").outcome.consent
    assert by_no(doc, "6.a").outcome.tally == "6-1"


def test_consent_motion_under_the_consent_section_decides_the_consent_sections_after_it():
    doc = structure(pages("pylusd-minutes.md"), kind="minutes")
    for n in ("22.1", "22.2", "23.1"):
        o = by_no(doc, n).outcome
        assert o and o.consent and o.from_item == "21" and o.mover == "Leandra Blades"
    assert by_no(doc, "27").outcome is None  # the adjournment after the consent sections is not decided by it


def test_block_vote_over_a_range_of_items():
    doc = structure(pages("pylusd-minutes.md"), kind="minutes")
    assert by_no(doc, "12.2").outcome.from_item == "12.4" and by_no(doc, "12.3").outcome
    assert by_no(doc, "12.1").outcome.from_item is None


def test_adjournment_motion_is_not_the_last_items_outcome():
    doc = structure(pages("summit-minutes.md"), kind="minutes")
    assert by_no(doc, "2025-127").outcome is None  # a first reading: no outcome by nature
    doc = structure(pages("cortez-minutes.md"), kind="minutes")
    assert doc.items[-1].procedural == "adjournment"


# ---------------------------------------------------------------- rule 6: procedural, attendance
def test_procedural_items_are_tagged_and_kept_but_reports_out_of_closed_session_are_not():
    doc = structure(pages("pylusd-minutes.md"), kind="minutes")
    assert by_no(doc, "3.5").procedural == "executive session"
    report = by_no(doc, "5")
    assert report.procedural is None and report in doc.buying_items() and report.outcome.result == "carried"
    assert by_no(doc, "7").procedural == "roll call" and by_no(doc, "7") not in doc.buying_items()
    assert procedural_tag("III. CONSENT AGENDA") is None
    assert procedural_tag("1. The meeting was called to order in the City Council Chambers") == "call to order"


@pytest.mark.parametrize("fixture,expect", [
    ("pylusd-minutes.md", [("Marilyn Anderson", "President", "present"), ("Dr. Allan Mucerino", "Secretary", "present")]),
    ("cortez-minutes.md", [("Rachel Medina", "Mayor", "present"), ("Dennis Spruell", "Mayor Pro-tem", "present"),
                           ("Sara Coffey", "Payroll/Sales Tax Administrator", "staff"),
                           ("Creighton Wright", "Director of Parks and Recreation", "staff")]),
    ("pueblo-minutes.md", [("Trisha Macias", "Vice President", "present"), ("Fredrick Quintana", "President", "absent"),
                           ("Sherri Baca", "Executive Director", "staff")]),
    ("summit-minutes.md", [("Mrs. Darrow", None, "present"), ("Mr. Ford", None, "present")]),
])
def test_attendance_of_four_bodies(fixture, expect):
    doc = structure(pages(fixture), kind="minutes")
    got = {(p.name, p.role, p.part) for p in doc.attendance}
    assert set(expect) <= got
    assert not any(p.name in ("President", "Community", "Absent") for p in doc.attendance)


def test_attendance_stops_at_the_next_sentence():
    got = attendance("Present: Mrs. Darrow, Mr. Ford. There were nine members of the public present in the audience.")
    assert [p.name for p in got] == ["Mrs. Darrow", "Mr. Ford"]


# ---------------------------------------------------------------- rule 7: people, orgs, amounts
def test_item_people_orgs_amounts():
    doc = structure(pages("cortez-minutes.md"), kind="minutes")
    bid = by_no(doc, "6.a")
    parts = {(p.name, p.part) for p in bid.people}
    assert {("Casey Simpson", "presented"), ("Councilmember Keefauver", "moved")} <= {(p.name if p.part != "moved" else
                                                                                       f"{p.role} {p.name}".strip(), p.part)
                                                                                      for p in bid.people} | parts
    assert 48250.0 in bid.amounts
    spoke = by_no(doc, "3.a")
    assert ("Susan Kemnetz", "spoke") in {(p.name, p.part) for p in spoke.people}
    doc = structure(pages("summit-minutes.md"), kind="minutes")
    res = by_no(doc, "2025-123")
    assert "Eagle Abatement & Demolition" in res.orgs and 425000.0 in res.amounts
    assert ("Mark Potter", "presented") in {(p.name, p.part) for p in res.people}


# ---------------------------------------------------------------- rule 3: bookmarks
def test_bookmarks_fix_a_two_column_text_layer():
    marks = [Bookmark(*m) for m in json.loads((FIX / "pylusd-columns.bookmarks.json").read_text())]
    doc = structure(pages("pylusd-columns.md"), kind="minutes", marks=marks)
    assert doc.source == "bookmarks"
    assert by_no(doc, "22.4").title.startswith("Approved the architectural services agreement") and "Melrose" in by_no(doc, "22.4").text
    assert "Morse" in by_no(doc, "22.5").text and "Melrose" not in by_no(doc, "22.5").text
    assert "$21,749,665.94" in by_no(doc, "22.2").text  # across the page break and the number column
    assert by_no(doc, "23.2").title.startswith("Approved the Independent Contractor Agreement with Girls Inc.")
    assert by_no(doc, "22.3", kind="attachment").pages == (12, 12)
    assert all(by_no(doc, n).outcome.consent for n in ("22.1", "22.5", "23.1", "23.2"))
    assert by_no(doc, "22").kind == "section" and by_no(doc, "22") not in doc.buying_items()
    # the same pages without their bookmarks flag themselves
    plain = structure(pages("pylusd-columns.md"), kind="minutes")
    assert plain.source == "text" and plain.quality["bare_labels"] >= 4
    assert "item numbers separated from their text (two-column layout)" in plain.quality["flags"]


def test_numbered_bookmarks_with_attachments_on_another_body():
    marks = [Bookmark(0, "Agenda", 1), Bookmark(0, "2.a. minutes for January 14, 2025", 2),
             Bookmark(0, "ATT_Minutes for January 14, 2025", 3), Bookmark(0, "2.b. Expenditure List", 4),
             Bookmark(0, "ATT_Expenditure List", 5), Bookmark(1, "012425 Expenditure List", 5),
             Bookmark(0, "7.a. Cortez Police Department Parking Lot Fencing Project", 6)]
    pg = [(1, "AGENDA\n2. CONSENT AGENDA\na. Approval of minutes\nb. Approval of the Expenditure List"),
          (2, "Memo: minutes for January 14, 2025 for approval."), (3, "Minutes of January 14"),
          (4, "Expenditure List memo"), (5, "|Vendor|Amount|\n|ACME Inc.|$1,200.00|"),
          (6, "7.a. Cortez Police Department Parking Lot Fencing Project\nStaff recommends the bid of $24,500.00.")]
    doc = structure(pg, kind="packet", marks=marks)
    assert doc.source == "bookmarks"
    assert [(it.number, it.kind) for it in doc.items] == [(None, "front"), ("2.a", "item"), ("2.a", "attachment"),
                                                          ("2.b", "item"), ("2.b", "attachment"), ("7.a", "item")]
    assert doc.attachments_of("2.b")[0].amounts == [1200.0]
    assert [it.number for it in doc.buying_items()] == ["2.a", "2.b", "7.a"]


def test_bookmarks_that_do_not_describe_items_fall_back_to_text():
    doc = structure(pages("summit-minutes.md"), kind="minutes", marks=[Bookmark(0, "Blank Page", 1)])
    assert doc.source == "text"
    assert bm.items_from([], []) is None


def test_reading_bookmarks_without_a_pdf_is_none(tmp_path):
    bad = tmp_path / "x.pdf"
    bad.write_bytes(b"not a pdf")
    assert bm.read(bad) is None


# ---------------------------------------------------------------- rule 8: quality
def test_quality_counters():
    doc = structure(pages("cortez-minutes.md"), kind="minutes")
    q = doc.quality
    assert q["numbered"] >= 0.9 and q["with_outcome"] > 0 and 0 < q["dropped_share"] < 0.1 and q["flags"] == []
    assert doc.dropped == {"header/footer": 2}
    lump = structure([(1, "word " * 3000)], kind="minutes")
    assert "no items found" in lump.quality["flags"]


def test_record_numbers_with_a_letter_prefix_open_items():
    pg = [(1, "## <u>MOTIONS FOR COUNCIL</u>\n\nM-038-2026 **A Motion of Council confirming the appointment of David "
              "Messner to the Akron Metropolitan Area Transportation Study (AMATS) Policy Committee.**\n\nMovant-Ms. "
              "Dickinson Mr. Schmidt moved to recommend passage. Motion passed-Voice Vote: 10-0 Motion M-038-2026 was "
              "adopted.\n\n|M-039-2026|David Messner addressed Council on this motion.|")]
    doc = structure(pg, kind="minutes")
    motion = by_no(doc, "M-038-2026")
    assert motion.section == "MOTIONS FOR COUNCIL" and motion.outcome.tally == "10-0"
    assert ("David Messner", "appointed") in {(p.name, p.part) for p in motion.people}
    assert not any(it.number == "M-039-2026" for it in doc.items)  # a table row naming it is not the item
    # on other bodies' text nothing opens by this rule
    for fixture in ("cortez-minutes.md", "pylusd-minutes.md", "pueblo-minutes.md"):
        assert not any(re.fullmatch(r"[A-Z]{1,3}-.*", it.number or "") for it in structure(pages(fixture)).items)


def test_a_decision_written_before_the_first_item_is_sent():
    pg = [(1, "COUNTY OF SUMMIT REGULAR COUNCIL MEETING MINUTES\n\nA Caucus was held prior to the meeting to discuss "
              "Agenda items. A final hearing took place regarding the Spring Garden Waldorf School ditch petition. "
              "A motion to affirm the Engineer's schedule of compensation regarding the Spring Garden Waldorf School. "
              "A motion to affirm passed authorizing the County Engineer to proceed with the project.\n\n"
              "The Council Regular Meeting was called to order at 06:28 PM in Council Chambers.\n\n"
              "2026-168 **A Resolution authorizing a contract.**")]
    doc = structure(pg, kind="minutes")
    front = doc.items[0]
    assert front.kind == "front" and front.outcome is not None and front in doc.buying_items()
    title_only = structure(pages("pylusd-minutes.md")).items[0]
    assert title_only.kind == "front" and title_only not in structure(pages("pylusd-minutes.md")).buying_items()


def test_a_long_or_money_bearing_procedural_item_still_goes_to_the_buying_question():
    from ddwb.structure.model import Document, Item
    body = "Call to order at 7 pm. " + "The board approved the purchase of two pumps from Acme Pump Co. for $48,500. " * 2
    short = Item(number=None, section=None, title="Call to order", text="Call to order at 7:02 pm by the chair.", pages=(1, 1),
                 kind="item", procedural="call to order")
    whole = Item(number=None, section=None, title="Call to order", text=body, pages=(1, 2), kind="item", procedural="call to order")
    sent = Document([short, whole], [], {}, {}, "text").buying_items()
    assert whole in sent and short not in sent
