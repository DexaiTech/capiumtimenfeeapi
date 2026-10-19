"""Rules shared by jobs, time entries, the timer and timesheets (docs/domain.md)."""

import math
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppException, ConflictError, ValidationError
from app.db.models import Client, Job, Organization
from app.services import clients as clients_service

CLIENT_WORK_STATUSES = ("prospect", "active")
OPEN_JOB_STATUSES = ("planned", "in_progress", "on_hold")
EDITABLE_SHEET_STATUSES = ("open", "rejected")
MAX_DAY_MINUTES = 24 * 60


class ClientInactiveError(ConflictError):
    code = "CLIENT_INACTIVE"
    message = "This client is inactive or archived and does not accept new work."


class JobClosedError(ConflictError):
    code = "JOB_CLOSED"
    message = "Time cannot be logged on a completed, closed or cancelled job."


class TimeLockedError(ConflictError):
    code = "TIME_LOCKED"
    message = "This time is locked: its timesheet is submitted or approved, or it is billed."


class InvalidTransitionError(ConflictError):
    code = "INVALID_STATUS_TRANSITION"
    message = "That status change is not allowed."


class DayLimitError(ValidationError):
    code = "DAY_LIMIT_EXCEEDED"
    message = "A person can log at most 24 hours in a day."


def practice_now() -> datetime:
    return datetime.now(UTC)


def practice_date(organization: Organization, moment: datetime) -> date:
    """The calendar date of a UTC timestamp in the practice's time zone."""
    return moment.astimezone(ZoneInfo(organization.timezone)).date()


def practice_today(organization: Organization) -> date:
    return practice_date(organization, practice_now())


def week_start_for(organization: Organization, day: date) -> date:
    """The practice ``week_start`` ISO weekday (1 = Monday) on or before ``day``."""
    return day - timedelta(days=(day.isoweekday() - organization.week_start) % 7)


def round_minutes(organization: Organization, minutes: int) -> int:
    """Round up to the practice's time increment (6 = tenths of an hour)."""
    increment = organization.time_increment_minutes
    return math.ceil(minutes / increment) * increment


async def ensure_client_accepts_work(session: AsyncSession, client: Client) -> None:
    """New jobs, time and timers: ``prospect`` and ``active`` clients only (409
    ``CLIENT_INACTIVE``).

    Delegates to the clients module, which also takes a ``FOR KEY SHARE`` lock on the
    client so a concurrent archive waits for this transaction. Call inside the unit of work.
    """
    if client.status not in CLIENT_WORK_STATUSES:
        raise ClientInactiveError(details={"client_id": str(client.id), "status": client.status})
    await clients_service.ensure_client_accepts_work(session, client.organization_id, client.id)


def ensure_job_open(job: Job) -> None:
    if job.status not in OPEN_JOB_STATUSES:
        raise JobClosedError(details={"job_id": str(job.id), "status": job.status})


def field_error(field: str, message: str, code: str = "invalid") -> AppException:
    return ValidationError.for_field(field, message, code)
