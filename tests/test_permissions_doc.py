"""docs/modules/permissions.md lists every permission with its group, its description and the
system roles that hold it, exactly as app/core/permissions.py defines them."""

import re

from app.core.permissions import CATALOGUE, SYSTEM_ROLES
from tests.conftest import ROOT

DOC = ROOT / "docs" / "modules" / "permissions.md"


def cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def read_doc() -> tuple[
    str, dict[str, tuple[str, str, frozenset[str]]], dict[str, tuple[str, int]]
]:
    """The text, the catalogue rows ``code -> (group, description, role keys)`` and the role
    rows ``key -> (name, permission count)``."""
    key_of = {role.name: role.key for role in SYSTEM_ROLES}
    text = DOC.read_text(encoding="utf-8")
    rows: dict[str, tuple[str, str, frozenset[str]]] = {}
    roles: dict[str, tuple[str, int]] = {}
    section = group = ""
    columns: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
        elif line.startswith("### "):
            group = line[4:].strip()
        elif section == "The catalogue" and line.startswith("| Code |"):
            columns = cells(line)[2:]
        elif section == "The catalogue" and line.startswith("| `"):
            code, description, *ticks = cells(line)
            assert len(ticks) == len(columns), line
            holders = frozenset(
                key_of[name] for name, tick in zip(columns, ticks, strict=True) if tick == "✓"
            )
            assert code.strip("`") not in rows, f"{code} is listed twice"
            rows[code.strip("`")] = (group, description, holders)
        elif section == "The system roles" and re.match(r"^\| [A-Z]\w+ \| `", line):
            name, key, count, _purpose = cells(line)
            roles[key.strip("`")] = (name, int(count))
    return text, rows, roles


def test_every_permission_is_documented_exactly_once():
    _, rows, _ = read_doc()
    catalogue = {p.code for p in CATALOGUE}
    assert sorted(catalogue - set(rows)) == [], "add these codes to docs/modules/permissions.md"
    assert sorted(set(rows) - catalogue) == [], "these codes are not in the catalogue any more"


def test_group_description_and_holders_match_the_catalogue():
    _, rows, _ = read_doc()
    for permission in CATALOGUE:
        holders = frozenset(r.key for r in SYSTEM_ROLES if permission.code in r.permissions)
        assert rows[permission.code] == (permission.category, permission.description, holders), (
            permission.code
        )


def test_the_role_table_and_the_counts_match():
    text, _, roles = read_doc()
    assert roles == {r.key: (r.name, len(r.permissions)) for r in SYSTEM_ROLES}
    groups = {p.category for p in CATALOGUE}
    assert f"There are **{len(CATALOGUE)}** permissions in **{len(groups)}** groups." in " ".join(
        text.split()
    )
