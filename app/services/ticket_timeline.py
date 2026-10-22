"""A ticket's timeline: what happened to it, newest first (docs/modules/tickets.md).

It is read from the audit log, which already holds every change of a ticket: the entries about
the ticket itself (created, updated, status changed) and those that name it in their details
(its comments and files). One entry that changed several fields (``ticket.updated``) becomes one
event per field. Reading needs ``ticket.read``; no other permission is involved, so the timeline
shows the ticket's own activity and nothing else of the audit log.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.db.models import AuditLog
from app.repositories.audit import AuditRepository
from app.repositories.tickets import TicketRepository
from app.schemas.audit import AuditFilters
from app.schemas.tickets import TicketPerson, TimelineEvent, TimelineKind
from app.services.policy import OrgContext
from app.utils.pagination import Page, PageParams

_RECORD_TYPES = frozenset({"ticket", "ticket_comment", "ticket_attachment"})
# Entries written before the statuses were renamed (the audit log is append-only).
_LEGACY_STATUS = {"hold": "on_hold", "reopen": "reopened"}
_UNASSIGNED = "Unassigned"


def _status(value: Any) -> str | None:
    return _LEGACY_STATUS.get(value, value) if isinstance(value, str) else None


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _names(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _pair(raw: Any) -> tuple[Any, Any]:
    if isinstance(raw, list | tuple) and len(raw) == 2:
        return raw[0], raw[1]
    return None, None


class TicketTimelineService:
    def __init__(self, session: AsyncSession) -> None:
        self.audit = AuditRepository(session)
        self.tickets = TicketRepository(session)

    async def timeline(
        self, ctx: OrgContext, ticket_id: uuid.UUID, params: PageParams
    ) -> Page[TimelineEvent]:
        org_id = ctx.organization_id
        if await self.tickets.get(org_id, ticket_id) is None:
            raise NotFoundError("Ticket not found.")
        page = await self.audit.history_page(
            org_id,
            "ticket",
            ticket_id,
            AuditFilters(),
            params,
            related_key="ticket_id",
            record_types=_RECORD_TYPES,
        )
        people = await self._people(page.items)
        events: list[TimelineEvent] = []
        for entry in page.items:
            actor = people.get(entry.actor_user_id) if entry.actor_user_id else None
            events.extend(self._events(entry, actor, people))
        return Page(items=events, next_cursor=page.next_cursor)

    async def _people(self, entries: list[AuditLog]) -> dict[uuid.UUID, TicketPerson]:
        """The actors and the assignees named in a page of entries, in one query."""
        ids: set[uuid.UUID] = set()
        for entry in entries:
            if entry.actor_user_id:
                ids.add(entry.actor_user_id)
            changes = entry.meta.get("changes") if isinstance(entry.meta, dict) else None
            if isinstance(changes, dict):
                for raw in _pair(changes.get("assignee_id")):
                    try:
                        ids.add(uuid.UUID(str(raw))) if raw else None
                    except ValueError:
                        continue
        users = await self.audit.get_actors(ids)
        return {uid: TicketPerson(id=u.id, full_name=u.full_name) for uid, u in users.items()}

    @staticmethod
    def _events(
        entry: AuditLog, actor: TicketPerson | None, people: dict[uuid.UUID, TicketPerson]
    ) -> list[TimelineEvent]:
        meta: dict[str, Any] = entry.meta if isinstance(entry.meta, dict) else {}
        bulk = bool(meta.get("bulk"))

        def event(kind: TimelineKind, suffix: str = "", **values: Any) -> TimelineEvent:
            return TimelineEvent(
                id=f"{entry.id}{suffix}",
                kind=kind,
                at=entry.created_at,
                actor=actor,
                before=values.get("before"),
                after=values.get("after"),
                added=values.get("added", []),
                removed=values.get("removed", []),
                detail=values.get("detail"),
                comment_id=values.get("comment_id"),
                bulk=bulk,
            )

        def person(raw: Any) -> str:
            try:
                found = people.get(uuid.UUID(str(raw))) if raw else None
            except ValueError:
                found = None
            return found.full_name if found else (_UNASSIGNED if not raw else "A former member")

        match entry.action:
            case "ticket.created":
                return [event("created", after=_status(meta.get("status")))]
            case "ticket.status_changed":
                return [
                    event(
                        "status_changed",
                        before=_status(meta.get("from")),
                        after=_status(meta.get("to")),
                    )
                ]
            case "ticket.updated":
                changes = meta.get("changes")
                return TicketTimelineService._field_events(
                    changes if isinstance(changes, dict) else {}, event, person
                )
            case "ticket_comment.created":
                return [
                    event(
                        "comment_added",
                        detail=_text(meta.get("excerpt")),
                        comment_id=entry.target_id,
                    )
                ]
            case "ticket_comment.updated":
                _, new = _pair((meta.get("changes") or {}).get("body"))
                return [event("comment_edited", detail=_text(new), comment_id=entry.target_id)]
            case "ticket_comment.deleted":
                return [event("comment_deleted")]
            case "ticket_attachment.added":
                raw = meta.get("comment_id")
                try:
                    comment_id = uuid.UUID(str(raw)) if raw else None
                except ValueError:
                    comment_id = None
                return [
                    event("file_added", detail=_text(meta.get("file_name")), comment_id=comment_id)
                ]
            case "ticket_attachment.deleted":
                return [event("file_removed", detail=_text(meta.get("file_name")))]
        return []

    @staticmethod
    def _field_events(changes: dict[str, Any], event: Any, person: Any) -> list[TimelineEvent]:
        found: list[TimelineEvent] = []
        for field, raw in changes.items():
            old, new = _pair(raw)
            match field:
                case "priority":
                    found.append(
                        event("priority_changed", f":{field}", before=_text(old), after=_text(new))
                    )
                case "assignee_id":
                    found.append(
                        event(
                            "assignee_changed", f":{field}", before=person(old), after=person(new)
                        )
                    )
                case "tags":
                    before, after = _names(old), _names(new)
                    added = [n for n in after if n not in before]
                    removed = [n for n in before if n not in after]
                    found.append(event("tags_changed", f":{field}", added=added, removed=removed))
                case "subject":
                    found.append(
                        event("subject_changed", f":{field}", before=_text(old), after=_text(new))
                    )
                case "description":
                    found.append(event("description_changed", f":{field}"))
        return found
