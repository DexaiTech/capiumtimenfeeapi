"""Turning a stored audit entry into what a person reads: a summary line and the before/after
changes.

Services write ``metadata`` freely. What changed appears in a few shapes, all normalised here to
``[{field, before, after}]``:

* ``changes: {field: [old, new]}``, the usual one;
* ``old`` and ``new`` objects (a member's role, a status) or plain values (a job's status);
* ``old_status``/``new_status`` style pairs (timesheets, expense claims) and ``from``/``to``.

Entries that record a creation or a deletion carry no changes: the summary names the record.
"""

from collections.abc import Mapping
from typing import Any

from app.schemas.audit import AuditChange

SUMMARY_LENGTH = 200
# The number or code of a record, then what it is called; both appear in the summary.
_REFERENCE_KEYS = ("number", "code")
_NAME_KEYS = (
    "name",
    "title",
    "subject",
    "member_name",
    "staff_name",
    "user_name",
    "template_name",
    "file_name",
    "description",
)
_PAIRS = (("old", "new"), ("before", "after"), ("from", "to"))


def _text(meta: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def summarise(label: str, meta: Mapping[str, Any]) -> str:
    """``"Invoice issued: INV-00012 (Babbage Ltd)"``: the label, the record, the client."""
    parts = (_text(meta, _REFERENCE_KEYS), _text(meta, _NAME_KEYS))
    subject = " · ".join(dict.fromkeys(part for part in parts if part))
    client = _text(meta, ("client_name",))
    text = f"{label}: {subject}" if subject else label
    if client and client not in subject:
        text = f"{text} ({client})"
    if len(text) > SUMMARY_LENGTH:
        text = text[: SUMMARY_LENGTH - 1] + "…"
    return text


def _from_changes(raw: Any) -> list[AuditChange]:
    if not isinstance(raw, Mapping):
        return []
    found: list[AuditChange] = []
    for field, value in raw.items():
        if isinstance(value, list | tuple) and len(value) == 2:
            found.append(AuditChange(field=str(field), before=value[0], after=value[1]))
        elif isinstance(value, Mapping):
            for old_key, new_key in _PAIRS:
                if old_key in value and new_key in value:
                    found.append(
                        AuditChange(field=str(field), before=value[old_key], after=value[new_key])
                    )
                    break
    return found


def _field_of(action: str) -> str:
    """``job.status_changed`` -> ``status``; ``value`` when the action does not say."""
    tail = action.rsplit(".", 1)[-1]
    stem = tail.removesuffix("_changed")
    return stem if stem != tail and stem else "value"


def _diff(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[AuditChange]:
    """The keys both objects hold whose values differ; ``role_id`` yields to ``role_name``."""
    changed = [
        AuditChange(field=str(key), before=old[key], after=new[key])
        for key in new
        if key in old and old[key] != new[key]
    ]
    names = {change.field for change in changed}
    return [
        change
        for change in changed
        if not (change.field.endswith("_id") and f"{change.field[:-3]}_name" in names)
    ]


def _from_pairs(action: str, meta: Mapping[str, Any]) -> list[AuditChange]:
    old, new = meta.get("old"), meta.get("new")
    if isinstance(old, Mapping) and isinstance(new, Mapping):
        return _diff(old, new)
    if "old" in meta and "new" in meta:
        return [AuditChange(field=_field_of(action), before=old, after=new)]
    paired = [
        AuditChange(field=key[4:], before=value, after=meta[f"new_{key[4:]}"])
        for key, value in meta.items()
        if key.startswith("old_") and f"new_{key[4:]}" in meta
    ]
    if paired:
        return paired
    if "from" in meta and "to" in meta:
        return [AuditChange(field=_field_of(action), before=meta["from"], after=meta["to"])]
    return []


def extract_changes(action: str, meta: Mapping[str, Any]) -> list[AuditChange]:
    """What the entry says changed, as ``[{field, before, after}]`` (empty when it says none)."""
    return _from_changes(meta.get("changes")) or _from_pairs(action, meta)
