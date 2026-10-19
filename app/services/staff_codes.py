"""Staff codes: the short unique code of a staff member (S001, S002, ...)."""

import re
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError
from app.repositories.staff import StaffRepository
from app.services import sequences

_ATTEMPTS = 1000
_VALID = re.compile(r"^[A-Z0-9_-]{1,20}$")


async def next_staff_code(session: AsyncSession, organization_id: uuid.UUID) -> str:
    """The next number of the ``staff`` sequence that no staff member uses.

    Run it inside the transaction that stores the code.
    """
    staff = StaffRepository(session)
    for _ in range(_ATTEMPTS):
        code = (await sequences.next_number(session, organization_id, "staff")).upper()
        if not _VALID.fullmatch(code):
            raise ConflictError(
                "The staff numbering produces an invalid code. Fix it in Settings.",
                code="SEQUENCE_INVALID",
            )
        if not await staff.staff_code_taken(organization_id, code):
            return code
    raise ConflictError(
        "No free staff code is left in the numbering sequence. Change it in Settings.",
        code="SEQUENCE_INVALID",
    )
