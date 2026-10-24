"""Request and response models of the task endpoints (the work inside a job)."""

import uuid
from datetime import date, datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, model_validator

from app.db.models import Task
from app.repositories.job_tasks import TaskActuals, TaskView
from app.schemas.bulk import MAX_BULK_IDS, parse_day, parse_user_id
from app.schemas.common import RequestModel
from app.schemas.jobs import JobStatus, Minutes, UserIds
from app.utils.money import percentage_bp

TaskStatus = Literal["not_started", "in_progress", "on_hold", "completed"]
TaskSort = Literal["due_date", "-due_date", "title", "-title", "job", "-job", "status", "-status"]
TaskTitle = Annotated[str, Field(min_length=1, max_length=200)]
TaskDescription = Annotated[str, Field(max_length=2000)]


class AssigneeRef(BaseModel):
    user_id: uuid.UUID
    full_name: str | None


class TaskRead(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID
    title: str
    description: str | None
    status: TaskStatus
    budget_minutes: int | None
    logged_minutes: int = Field(description="Time logged on the task, by anyone")
    budget_used_bp: int | None = Field(
        description="logged_minutes / budget_minutes in basis points; null without a budget"
    )
    due_date: date | None
    is_overdue: bool = Field(description="Due before today (practice time zone) and not completed")
    sort_order: int = Field(description="Position in the job's task list, 0 first")
    completed_at: datetime | None
    pm_task_id: str | None = Field(
        default=None,
        description="The practice management task this task is linked to (null: a local task)",
    )
    assignees: list[AssigneeRef]
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def build(
        cls, task: Task, actuals: TaskActuals | None, assignees: list[AssigneeRef], today: date
    ) -> "TaskRead":
        return cls(**_fields(task, actuals, assignees, today))


class TaskListItem(TaskRead):
    """A task with the job and client it belongs to (the cross-job list)."""

    job_number: str
    job_name: str
    job_status: JobStatus
    client_id: uuid.UUID
    client_name: str

    @classmethod
    def from_view(
        cls, view: TaskView, actuals: TaskActuals | None, assignees: list[AssigneeRef], today: date
    ) -> "TaskListItem":
        return cls(
            **_fields(view.task, actuals, assignees, today),
            job_number=view.job_number,
            job_name=view.job_name,
            job_status=view.job_status,
            client_id=view.client_id,
            client_name=view.client_name,
        )


def _fields(
    task: Task, actuals: TaskActuals | None, assignees: list[AssigneeRef], today: date
) -> dict[str, object]:
    logged = actuals.minutes if actuals else 0
    return {
        "id": task.id,
        "job_id": task.job_id,
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "budget_minutes": task.budget_minutes,
        "logged_minutes": logged,
        "budget_used_bp": (
            percentage_bp(logged, task.budget_minutes) if task.budget_minutes else None
        ),
        "due_date": task.due_date,
        "is_overdue": (
            task.due_date is not None and task.due_date < today and task.status != "completed"
        ),
        "sort_order": task.sort_order,
        "completed_at": task.completed_at,
        "pm_task_id": task.pm_task_id,
        "assignees": assignees,
        "created_by": task.created_by,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


class TaskCreate(RequestModel):
    title: TaskTitle
    description: TaskDescription | None = None
    status: TaskStatus = "not_started"
    budget_minutes: Minutes | None = Field(
        default=None, description="Hours budget of the task, in whole minutes"
    )
    due_date: date | None = Field(
        default=None, description="Within the job's start and due dates, where the job has them"
    )
    assignee_user_ids: UserIds = Field(
        default_factory=list, description="Active members of the practice"
    )


class TaskUpdate(RequestModel):
    """Every field optional. Status and assignees have their own endpoints."""

    title: TaskTitle | None = None
    description: TaskDescription | None = None
    budget_minutes: Minutes | None = None
    due_date: date | None = None

    @model_validator(mode="after")
    def _validate(self) -> Self:
        if "title" in self.model_fields_set and self.title is None:
            raise ValueError("title: This field cannot be null")
        return self


class TaskStatusChange(RequestModel):
    status: TaskStatus


class TaskAssigneesSet(RequestModel):
    user_ids: UserIds = Field(description="The complete list of assignees (active members)")


class TaskReorder(RequestModel):
    task_ids: list[uuid.UUID] = Field(
        min_length=1,
        max_length=500,
        description="Every task of the job exactly once, in the new order",
    )


MAX_BULK_NEW_TASKS = 100
TaskBulkActionName = Literal[
    "set_status", "set_due_date", "add_assignee", "remove_assignee", "delete"
]


class TaskBulkCreate(RequestModel):
    """Several tasks for one job, all or nothing; they are added in the order sent."""

    tasks: list[TaskCreate] = Field(min_length=1, max_length=MAX_BULK_NEW_TASKS)


class TaskBulkAction(RequestModel):
    """One action on up to 200 tasks (of any jobs), all or nothing."""

    task_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_BULK_IDS)
    action: TaskBulkActionName
    value: str | None = Field(
        default=None,
        description=(
            "`set_status`: a task status; `set_due_date`: `YYYY-MM-DD` or null to clear; "
            "`add_assignee` / `remove_assignee`: a staff id; `delete`: no value"
        ),
    )

    @model_validator(mode="after")
    def _value_fits_the_action(self) -> Self:
        action, value = self.action, self.value
        if action == "set_status":
            if value not in ("not_started", "in_progress", "on_hold", "completed"):
                raise ValueError("value: Use not_started, in_progress, on_hold or completed.")
        elif action == "set_due_date":
            if value is not None:
                parse_day(value)
        elif action in ("add_assignee", "remove_assignee"):
            if value is None:
                raise ValueError("value: This field is required for this action.")
            parse_user_id(value)
        elif value is not None:
            raise ValueError("value: This action takes no value.")
        return self


class TasksFromService(BaseModel):
    """What `POST /jobs/{id}/tasks/from-service` did."""

    added: int = Field(description="Default tasks added to the job")
    skipped: int = Field(description="Default tasks left out: a task of that title already exists")
