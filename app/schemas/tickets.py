"""Request and response models of the Application Testing tickets: tickets, tags, comments,
attachments and the bulk action."""

import re
import unicodedata
import uuid
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from app.db.models import TicketAttachment, TicketTag
from app.db.models.tickets import EXCERPT_LENGTH, TICKET_PRIORITIES, TICKET_STATUSES
from app.schemas.common import RequestModel
from app.schemas.settings import blank_is_null, reject_nulls
from app.utils.rich_text import html_to_text, sanitize_html

TicketStatus = Literal[
    "open",
    "in_progress",
    "requirement_pending",
    "clarification_required",
    "blocked",
    "ready_for_ba_review",
    "reopened",
    "closed",
    "invalid",
    "on_hold",
]
TicketPriority = Literal["high", "medium", "low", "not_set"]
TagColor = Literal["violet", "blue", "green", "red", "orange", "grey"]
BulkActionName = Literal["set_status", "set_priority", "add_tag", "remove_tag"]
TicketSort = Literal["number", "code", "subject", "created_at", "status", "priority"]

MAX_TAGS_PER_TICKET = 20
MAX_BULK_TICKETS = 200

_WHITESPACE = re.compile(r"\s+")


def _reject_controls(text: str, allowed: str = "") -> None:
    """Control characters (NUL above all: PostgreSQL cannot store it) never belong in text."""
    for char in text:
        if char not in allowed and unicodedata.category(char) == "Cc":
            raise ValueError("Control characters are not allowed.")


def _single_line(value: str) -> str:
    text = _WHITESPACE.sub(" ", value).strip()
    _reject_controls(text)
    return text


def _multi_line(value: str) -> str:
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    _reject_controls(text, allowed="\n\t")
    return text


# What the editor sends. Never stored as it came: ``_apply_rich_text`` rebuilds it.
RichHtml = Annotated[
    str,
    Field(max_length=60_000, description="Sanitised by the server; its text fills the plain field"),
]
MAX_DESCRIPTION_HTML = 40_000
MAX_COMMENT_HTML = 20_000


def _apply_rich_text(
    model: BaseModel,
    *,
    html_field: str,
    text_field: str,
    max_text: int,
    max_html: int,
    blank_text_is_null: bool,
) -> None:
    """Sanitise the editor's HTML and put its text in the plain field.

    Without HTML nothing is derived, and a plain-text write replaces earlier rich text (the HTML
    is cleared), so the two never disagree. Setting the fields as "set" lets updates see them."""
    given = model.model_fields_set
    html = getattr(model, html_field)
    if html is None:
        if text_field in given:
            setattr(model, html_field, None)
            given.add(html_field)
        else:
            # A bare null for the HTML changes nothing: the text would no longer match it.
            given.discard(html_field)
        return
    clean = sanitize_html(html)
    if len(clean) > max_html:
        raise ValueError("The formatted text is too long.")
    text = html_to_text(clean) if clean else ""
    if len(text) > max_text:
        raise ValueError(f"Use at most {max_text:,} characters.")
    _reject_controls(text, allowed="\n\t")
    setattr(model, html_field, clean or None)
    setattr(model, text_field, (text or None) if blank_text_is_null else text)
    given.update({html_field, text_field})


Subject = Annotated[str, Field(min_length=1, max_length=200), AfterValidator(_single_line)]
Description = Annotated[str, Field(max_length=10_000), AfterValidator(_multi_line)]
CommentBody = Annotated[str, Field(max_length=5_000), AfterValidator(_multi_line)]
TagName = Annotated[str, Field(min_length=1, max_length=40), AfterValidator(_single_line)]


# --- Reads ------------------------------------------------------------------------------


class TagRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    color: TagColor

    @classmethod
    def from_model(cls, tag: TicketTag) -> "TagRef":
        return cls.model_validate(tag)


class TicketPerson(BaseModel):
    id: uuid.UUID
    full_name: str


class AttachmentRead(BaseModel):
    id: uuid.UUID
    ticket_id: uuid.UUID
    comment_id: uuid.UUID | None = Field(
        description="The comment the file belongs to; null for a file on the ticket itself"
    )
    file_name: str = Field(description="The sanitised name the file was uploaded with")
    content_type: str = Field(description="What the content really is (sniffed), not the claim")
    size_bytes: int
    is_image: bool = Field(description="True for images: show a thumbnail (`inline` download)")
    uploaded_by: uuid.UUID
    uploaded_by_name: str | None
    created_at: datetime

    @classmethod
    def from_model(cls, row: TicketAttachment, names: dict[uuid.UUID, str]) -> "AttachmentRead":
        return cls(
            id=row.id,
            ticket_id=row.ticket_id,
            comment_id=row.comment_id,
            file_name=row.file_name,
            content_type=row.content_type,
            size_bytes=row.size_bytes,
            is_image=row.content_type.startswith("image/"),
            uploaded_by=row.uploaded_by,
            uploaded_by_name=names.get(row.uploaded_by),
            created_at=row.created_at,
        )


class TicketBase(BaseModel):
    id: uuid.UUID
    code: str = Field(description="`APT271`: the practice's prefix and the number")
    number: int
    category: Literal["application"]
    subject: str
    status: TicketStatus
    priority: TicketPriority
    tags: list[TagRef]
    assignee: TicketPerson | None
    created_by: uuid.UUID
    created_by_name: str | None
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None
    comment_count: int
    attachment_count: int = Field(description="Every file on the ticket, comments' included")


class TicketListItem(TicketBase):
    description_excerpt: str | None = Field(
        description=f"The first {EXCERPT_LENGTH} characters of the description"
    )


class TicketDetail(TicketBase):
    description: str | None
    description_html: str | None = Field(
        default=None,
        description=(
            "Sanitised HTML of the description; null for a plain-text one (use `description`)"
        ),
    )
    attachments: list[AttachmentRead] = Field(
        description=(
            "Every file of the ticket, oldest first; the ticket's own have `comment_id` null"
        )
    )


class CommentRead(BaseModel):
    id: uuid.UUID
    ticket_id: uuid.UUID
    author: TicketPerson
    body: str
    body_html: str | None = Field(
        default=None,
        description="Sanitised HTML of the comment; null for a plain-text one (use `body`)",
    )
    edited_at: datetime | None
    created_at: datetime
    attachments: list[AttachmentRead]


class TagRead(BaseModel):
    id: uuid.UUID
    name: str
    color: TagColor
    ticket_count: int = Field(description="Tickets that carry the tag")
    created_at: datetime
    updated_at: datetime


class BulkResult(BaseModel):
    action: BulkActionName
    affected: int = Field(description="Distinct tickets the action was applied to (the selection)")
    changed: int = Field(
        description="Of those, the tickets that actually changed (one already in the wanted state "
        "does not count)"
    )


class TagDeleted(BaseModel):
    id: uuid.UUID
    tickets_untagged: int = Field(description="Tickets the tag was removed from")


# --- Writes -----------------------------------------------------------------------------


class TicketCreate(RequestModel):
    subject: Subject
    description: Description | None = None
    description_html: RichHtml | None = None
    status: TicketStatus = "open"
    priority: TicketPriority = "not_set"
    tag_ids: list[uuid.UUID] = Field(default_factory=list, max_length=MAX_TAGS_PER_TICKET)
    assignee_id: uuid.UUID | None = Field(
        default=None, description="An active member of the practice"
    )

    _description = field_validator("description")(blank_is_null)

    @model_validator(mode="after")
    def _rich_text(self) -> Self:
        _apply_rich_text(
            self,
            html_field="description_html",
            text_field="description",
            max_text=10_000,
            max_html=MAX_DESCRIPTION_HTML,
            blank_text_is_null=True,
        )
        return self


class TicketUpdate(RequestModel):
    """Every field optional. `assignee_id` accepts null to clear it; `tag_ids` replaces the
    ticket's tags. The subject and the description are written once, when the ticket is
    created: sending them answers 422 (unknown field)."""

    status: TicketStatus | None = None
    priority: TicketPriority | None = None
    tag_ids: list[uuid.UUID] | None = Field(default=None, max_length=MAX_TAGS_PER_TICKET)
    assignee_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _no_nulls(self) -> Self:
        reject_nulls(self, "status", "priority", "tag_ids")
        return self


class BulkAction(RequestModel):
    ticket_ids: list[uuid.UUID] = Field(min_length=1, max_length=MAX_BULK_TICKETS)
    action: BulkActionName
    value: str | None = Field(
        default=None,
        description=(
            "`set_status`: a status; `set_priority`: a priority; `add_tag` / `remove_tag`: a tag id"
        ),
    )

    @model_validator(mode="after")
    def _value_fits_the_action(self) -> Self:
        value = self.value
        if value is None:
            raise ValueError("value: This field is required for this action.")
        elif self.action == "set_status" and value not in TICKET_STATUSES:
            raise ValueError(f"value: Use one of {', '.join(TICKET_STATUSES)}.")
        elif self.action == "set_priority" and value not in TICKET_PRIORITIES:
            raise ValueError(f"value: Use one of {', '.join(TICKET_PRIORITIES)}.")
        elif self.action in ("add_tag", "remove_tag"):
            try:
                uuid.UUID(value)
            except ValueError as exc:
                raise ValueError("value: Use the id of a tag.") from exc
        return self

    @property
    def tag_id(self) -> uuid.UUID:
        assert self.value is not None
        return uuid.UUID(self.value)


class TagCreate(RequestModel):
    name: TagName
    color: TagColor = "grey"


class TagUpdate(RequestModel):
    name: TagName | None = None
    color: TagColor | None = None

    @model_validator(mode="after")
    def _no_nulls(self) -> Self:
        reject_nulls(self, "name", "color")
        return self


class CommentCreate(RequestModel):
    body: CommentBody = ""
    body_html: RichHtml | None = None
    attachment_ids: list[uuid.UUID] = Field(
        default_factory=list,
        max_length=25,
        description="Files uploaded to this ticket beforehand and not yet on a comment",
    )

    @model_validator(mode="after")
    def _rich_text(self) -> Self:
        _apply_rich_text(
            self,
            html_field="body_html",
            text_field="body",
            max_text=5_000,
            max_html=MAX_COMMENT_HTML,
            blank_text_is_null=False,
        )
        return self


__all__ = [
    "EXCERPT_LENGTH",
    "MAX_BULK_TICKETS",
    "MAX_TAGS_PER_TICKET",
    "AttachmentRead",
    "BulkAction",
    "BulkActionName",
    "BulkResult",
    "CommentCreate",
    "CommentRead",
    "TagColor",
    "TagCreate",
    "TagDeleted",
    "TagRead",
    "TagRef",
    "TagUpdate",
    "TicketBase",
    "TicketCreate",
    "TicketDetail",
    "TicketListItem",
    "TicketPerson",
    "TicketPriority",
    "TicketSort",
    "TicketStatus",
    "TicketUpdate",
]


TimelineKind = Literal[
    "created",
    "status_changed",
    "priority_changed",
    "assignee_changed",
    "tags_changed",
    "subject_changed",
    "description_changed",
    "comment_added",
    "comment_edited",
    "comment_deleted",
    "file_added",
    "file_removed",
]


class TimelineEvent(BaseModel):
    """One thing that happened to a ticket, read from the audit log."""

    id: str = Field(description="Stable id of the event (an entry can hold several changes)")
    kind: TimelineKind
    at: datetime
    actor: TicketPerson | None = Field(description="Who did it; null for the system")
    before: str | None = Field(
        description=(
            "What it was: a status or priority code, a person's name (`Unassigned` for none) "
            "or the old subject. Null when the event has no before."
        )
    )
    after: str | None = Field(description="What it became (same forms as `before`)")
    added: list[str] = Field(description="Tags added (`tags_changed`)")
    removed: list[str] = Field(description="Tags removed (`tags_changed`)")
    detail: str | None = Field(
        description="An excerpt of the comment, or the name of the file; null otherwise"
    )
    comment_id: uuid.UUID | None = Field(description="The comment the event is about, if any")
    bulk: bool = Field(description="True when it came from a bulk action on several tickets")
