"""People data the registry must not pass on: column names that hold people, and values shaped like contacts.

Agencies sometimes type an e-mail address (often a person's) or a phone number into a website field; those values are
dropped from resolver cards and redacted from releases.
"""

from __future__ import annotations

import re

COLUMN = re.compile(r"e_?mail|phone|fax|contact|first_?name|last_?name|middle_?name|full_?name|person|(^|_)poc(_|$)|"
                    r"officer|official|mayor|superintendent|director|manager|clerk|signator|owner_name|birth|ssn", re.I)
EMAIL = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
PHONE = r"(^|[^0-9])\(?[2-9][0-9]{2}\)?[-. ][0-9]{3}[-. ][0-9]{4}([^0-9]|$)"


def sql_contact(column: str) -> str:
    """SQL: true when the column's value looks like an e-mail address or a phone number (false for NULL)."""
    return f"coalesce(regexp_matches({column}, '{EMAIL}') OR regexp_matches({column}, '{PHONE}'), false)"


def is_contact(value: str | None) -> bool:
    return bool(value) and bool(re.search(EMAIL, value) or re.search(PHONE, value))
