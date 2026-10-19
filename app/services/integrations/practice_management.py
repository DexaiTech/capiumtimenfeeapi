"""The practice management system behind one interface (Phase 2A).

Practice management (PM) owns a job's name, client, service, manager, partner and dates; Time &
Fees links its jobs to PM jobs and keeps the *status* of jobs and tasks in step in both
directions. Deadlines are not synchronised. Time & Fees reaches PM only through
:class:`PracticeManagementGateway`; the real system is one more implementation next to
:class:`MockPracticeManagementGateway`.

PM identifies the client by its **central client id**, so a PM job can only be imported for a
client the practice has enabled from the central client service.
"""

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Any, Literal, Protocol

from app.services.integrations.common import (
    DEFAULT_CHANGE_BATCH,
    IntegrationRejectedError,
    IntegrationUnavailableError,
)

PM_JOB_STATUSES = ("not_started", "in_progress", "on_hold", "completed", "closed", "cancelled")
PM_TASK_STATUSES = ("not_started", "in_progress", "on_hold", "completed")
PmEventType = Literal["job_updated", "job_status_changed", "task_status_changed"]
DEFAULT_JOB_PAGE = 100
# What someone can change on a job in PM (the status has its own change).
PM_EDITABLE_FIELDS = (
    "name",
    "service_code",
    "manager_email",
    "partner_email",
    "start_date",
    "due_date",
)

@dataclass(frozen=True, slots=True)
class PmTask:
    pm_task_id: str
    title: str
    status: str
    due_date: date | None = None


@dataclass(frozen=True, slots=True)
class PmJob:
    pm_job_id: str
    name: str
    status: str
    # The client the job is for, by its id at the central client service.
    central_client_id: str
    service_code: str | None
    manager_email: str | None
    partner_email: str | None
    start_date: date | None
    due_date: date | None
    tasks: tuple[PmTask, ...]
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PmJobPage:
    jobs: tuple[PmJob, ...]
    cursor: str | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class PmEvent:
    """One change at PM. ``event_id`` is unique and stable: delivering it again changes
    nothing. ``job_updated`` says "read the job again"; the status events carry the new
    ``status`` of the job, or of the task ``pm_task_id``."""

    event_id: str
    type: PmEventType
    pm_job_id: str
    occurred_at: datetime
    pm_task_id: str | None = None
    status: str | None = None


@dataclass(frozen=True, slots=True)
class PmChanges:
    events: tuple[PmEvent, ...]
    cursor: str | None
    has_more: bool


@dataclass(frozen=True, slots=True)
class PmReceipt:
    """PM's answer to a status push: its reference for the change."""

    external_ref: str


class PracticeManagementGateway(Protocol):
    """A practice management system. Every call raises :class:`IntegrationUnavailableError`
    when the system cannot be used and :class:`IntegrationRejectedError` when it refuses the
    request for good (an unknown job or task, a status it does not know)."""

    name: str

    async def list_jobs(self, cursor: str | None, limit: int = DEFAULT_JOB_PAGE) -> PmJobPage:
        """PM's jobs with their tasks, by id, after ``cursor`` (``None``: from the start)."""
        ...

    async def get_job(self, pm_job_id: str) -> PmJob | None:
        """One job with its tasks, or ``None`` when PM does not know it."""
        ...

    async def set_job_status(
        self, pm_job_id: str, status: str, *, idempotency_key: str
    ) -> PmReceipt:
        """Set a job's status; repeating the same ``idempotency_key`` changes nothing."""
        ...

    async def set_task_status(
        self, pm_job_id: str, pm_task_id: str, status: str, *, idempotency_key: str
    ) -> PmReceipt:
        """Set a task's status; repeating the same ``idempotency_key`` changes nothing."""
        ...

    async def changes_since(
        self, cursor: str | None, limit: int = DEFAULT_CHANGE_BATCH
    ) -> PmChanges:
        """The events after ``cursor`` (``None``: from the start), oldest first."""
        ...


_CATALOGUE_DATE = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def _task(job: int, number: int, title: str, status: str, due: date | None = None) -> PmTask:
    return PmTask(f"PMT-{job}{number}", title, status, due)


def _build_catalogue() -> dict[str, PmJob]:
    def job(
        number: int,
        name: str,
        status: str,
        central_client_id: str,
        service_code: str | None,
        start: date | None,
        due: date | None,
        tasks: tuple[PmTask, ...],
    ) -> PmJob:
        return PmJob(
            pm_job_id=f"PMJ-{number}",
            name=name,
            status=status,
            central_client_id=central_client_id,
            service_code=service_code,
            manager_email=None,
            partner_email=None,
            start_date=start,
            due_date=due,
            tasks=tasks,
            updated_at=_CATALOGUE_DATE,
        )

    jobs = (
        job(
            1001,
            "Year-end accounts 2026",
            "in_progress",
            "CC-0001",
            "ACCS",
            date(2026, 10, 1),
            date(2026, 12, 31),
            (
                _task(1001, 1, "Request records", "completed", date(2026, 10, 15)),
                _task(1001, 2, "Prepare draft accounts", "in_progress", date(2026, 11, 15)),
                _task(1001, 3, "Partner review", "not_started", date(2026, 12, 1)),
            ),
        ),
        job(
            1002,
            "VAT return Q3 2026",
            "not_started",
            "CC-0002",
            "VAT",
            date(2026, 10, 12),
            date(2026, 11, 7),
            (
                _task(1002, 1, "Reconcile ledgers", "not_started", date(2026, 10, 26)),
                _task(1002, 2, "File the return", "not_started", date(2026, 11, 5)),
            ),
        ),
        job(
            1003,
            "Corporation tax return 2026",
            "in_progress",
            "CC-0003",
            "CT",
            date(2026, 9, 1),
            date(2026, 12, 15),
            (_task(1003, 1, "Computation", "in_progress", date(2026, 11, 20)),),
        ),
        job(
            1004,
            "Payroll October 2026",
            "in_progress",
            "CC-0004",
            "PAYROLL",
            date(2026, 10, 1),
            date(2026, 10, 28),
            (),
        ),
        job(
            1005,
            "Bookkeeping Q3 2026",
            "on_hold",
            "CC-0005",
            "BOOKS",
            date(2026, 10, 5),
            date(2026, 11, 30),
            (_task(1005, 1, "Bank reconciliation", "on_hold"),),
        ),
        job(
            1006,
            "Self assessment 2025/26",
            "not_started",
            "CC-0009",
            "SA",
            date(2026, 11, 1),
            date(2027, 1, 31),
            (),
        ),
        job(
            1007,
            "Company secretarial review",
            "completed",
            "CC-0010",
            "SECRETARIAL",
            date(2026, 8, 1),
            date(2026, 9, 30),
            (),
        ),
        job(
            1008,
            "Year-end accounts 2025",
            "closed",
            "CC-0001",
            "ACCS",
            date(2025, 10, 1),
            date(2025, 12, 31),
            (_task(1008, 1, "File accounts", "completed", date(2025, 12, 20)),),
        ),
    )
    return {entry.pm_job_id: entry for entry in jobs}


class MockPracticeManagementGateway:
    """In-memory PM for development and tests: eight jobs of clients of the mock central client
    catalogue, a change feed, an outage switch and a record of what Time & Fees pushed. State
    lives in the process."""

    name = "mock"

    def __init__(self) -> None:
        self.reset()

    # --- State for tests and demos ---------------------------------------------------------

    def reset(self) -> None:
        """Back to the catalogue, switched on, with an empty feed and nothing pushed."""
        self._jobs = _build_catalogue()
        self._feed: list[tuple[int, PmEvent]] = []
        self._available = True
        self._fail_next: list[bool] = []
        self._pushes: dict[str, str] = {}
        self.pushed: list[tuple[str, str, str | None]] = []

    @property
    def available(self) -> bool:
        return self._available

    def set_available(self, available: bool) -> None:
        self._available = available

    def fail_next(self, count: int = 1, *, permanent: bool = False) -> None:
        """Make the next ``count`` status pushes fail (``permanent``: PM refuses them)."""
        self._fail_next.extend([permanent] * count)

    def catalogue(self) -> list[PmJob]:
        return [self._jobs[key] for key in sorted(self._jobs)]

    def add_job(self, job: PmJob) -> None:
        self._jobs[job.pm_job_id] = job

    def _append(
        self,
        event_type: PmEventType,
        pm_job_id: str,
        *,
        pm_task_id: str | None = None,
        status: str | None = None,
    ) -> PmEvent:
        sequence = len(self._feed) + 1
        event = PmEvent(
            event_id=f"mock-pm-evt-{sequence:06d}",
            type=event_type,
            pm_job_id=pm_job_id,
            occurred_at=datetime.now(UTC),
            pm_task_id=pm_task_id,
            status=status,
        )
        self._feed.append((sequence, event))
        return event

    def update_job(self, pm_job_id: str, **fields: Any) -> PmEvent:
        """Someone changes the job in PM (``PM_EDITABLE_FIELDS``)."""
        unknown = set(fields) - set(PM_EDITABLE_FIELDS)
        if unknown:
            raise ValueError(f"Not editable in PM: {sorted(unknown)}")
        job = replace(self._jobs[pm_job_id], updated_at=datetime.now(UTC), **fields)
        self._jobs[pm_job_id] = job
        return self._append("job_updated", pm_job_id)

    def change_job_status(self, pm_job_id: str, status: str) -> PmEvent:
        """Someone changes the job's status in PM."""
        job = self._jobs[pm_job_id]
        self._jobs[pm_job_id] = replace(job, status=status, updated_at=datetime.now(UTC))
        return self._append("job_status_changed", pm_job_id, status=status)

    def change_task_status(self, pm_job_id: str, pm_task_id: str, status: str) -> PmEvent:
        """Someone changes a task's status in PM."""
        job = self._jobs[pm_job_id]
        tasks = tuple(
            replace(task, status=status) if task.pm_task_id == pm_task_id else task
            for task in job.tasks
        )
        if tasks == job.tasks:
            raise KeyError(pm_task_id)
        self._jobs[pm_job_id] = replace(job, tasks=tasks, updated_at=datetime.now(UTC))
        return self._append("task_status_changed", pm_job_id, pm_task_id=pm_task_id, status=status)

    def status_of(self, pm_job_id: str, pm_task_id: str | None = None) -> str:
        """The status PM holds now for a job, or for one of its tasks."""
        job = self._jobs[pm_job_id]
        if pm_task_id is None:
            return job.status
        return next(task.status for task in job.tasks if task.pm_task_id == pm_task_id)

    # --- The gateway interface -------------------------------------------------------------

    def _ensure_available(self) -> None:
        if not self._available:
            raise IntegrationUnavailableError("The mock practice management system is off.")

    def _next_push(self) -> None:
        self._ensure_available()
        if self._fail_next:
            if self._fail_next.pop(0):
                raise IntegrationRejectedError("Practice management refused the change.")
            raise IntegrationUnavailableError("Practice management did not answer.")

    async def list_jobs(self, cursor: str | None, limit: int = DEFAULT_JOB_PAGE) -> PmJobPage:
        self._ensure_available()
        ordered = [self._jobs[key] for key in sorted(self._jobs)]
        pending = [job for job in ordered if cursor is None or job.pm_job_id > cursor]
        page = pending[:limit]
        return PmJobPage(
            jobs=tuple(page),
            cursor=page[-1].pm_job_id if page else cursor,
            has_more=len(pending) > len(page),
        )

    async def get_job(self, pm_job_id: str) -> PmJob | None:
        self._ensure_available()
        return self._jobs.get(pm_job_id)

    async def set_job_status(
        self, pm_job_id: str, status: str, *, idempotency_key: str
    ) -> PmReceipt:
        self._next_push()
        job = self._jobs.get(pm_job_id)
        if job is None:
            raise IntegrationRejectedError(f"Practice management has no job {pm_job_id}.")
        if status not in PM_JOB_STATUSES:
            raise IntegrationRejectedError(f"Practice management has no job status {status}.")
        if idempotency_key not in self._pushes:
            self._pushes[idempotency_key] = f"{pm_job_id}:{status}"
            self._jobs[pm_job_id] = replace(job, status=status, updated_at=datetime.now(UTC))
            self.pushed.append((pm_job_id, status, None))
        return PmReceipt(external_ref=self._pushes[idempotency_key])

    async def set_task_status(
        self, pm_job_id: str, pm_task_id: str, status: str, *, idempotency_key: str
    ) -> PmReceipt:
        self._next_push()
        job = self._jobs.get(pm_job_id)
        if job is None or all(task.pm_task_id != pm_task_id for task in job.tasks):
            raise IntegrationRejectedError(f"Practice management has no task {pm_task_id}.")
        if status not in PM_TASK_STATUSES:
            raise IntegrationRejectedError(f"Practice management has no task status {status}.")
        if idempotency_key not in self._pushes:
            self._pushes[idempotency_key] = f"{pm_task_id}:{status}"
            tasks = tuple(
                replace(task, status=status) if task.pm_task_id == pm_task_id else task
                for task in job.tasks
            )
            self._jobs[pm_job_id] = replace(job, tasks=tasks, updated_at=datetime.now(UTC))
            self.pushed.append((pm_job_id, status, pm_task_id))
        return PmReceipt(external_ref=self._pushes[idempotency_key])

    async def changes_since(
        self, cursor: str | None, limit: int = DEFAULT_CHANGE_BATCH
    ) -> PmChanges:
        self._ensure_available()
        after = int(cursor) if cursor and cursor.isdigit() else 0
        pending = [(seq, event) for seq, event in self._feed if seq > after]
        batch = pending[:limit]
        last = batch[-1][0] if batch else after
        return PmChanges(
            events=tuple(event for _, event in batch),
            cursor=str(last),
            has_more=len(pending) > len(batch),
        )
