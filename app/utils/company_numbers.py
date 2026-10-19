"""Company numbers as the practice and the central service write them.

Companies House numbers are eight characters: digits, or a two-letter prefix and six digits
(``SC123456``, ``OC301234``, ``NI012345``). They are typed with spaces, in any case, and without
the leading zeros a spreadsheet drops, so two numbers are compared in their normal form.
"""

import re

_NOT_ALPHANUMERIC = re.compile(r"[^A-Za-z0-9]")
COMPANY_NUMBER_LENGTH = 8


def normalise_company_number(value: str | None) -> str | None:
    """Upper case, only letters and digits, and an all-digit number padded to eight digits.

    ``" sc 123456 "`` -> ``"SC123456"``, ``"1234567"`` -> ``"01234567"``; blank -> ``None``.
    """
    if value is None:
        return None
    cleaned = _NOT_ALPHANUMERIC.sub("", value).upper()
    if not cleaned:
        return None
    if cleaned.isdigit() and len(cleaned) < COMPANY_NUMBER_LENGTH:
        return cleaned.zfill(COMPANY_NUMBER_LENGTH)
    return cleaned
