"""The WIP engine: filters, aggregation and ageing over the WIP atoms.

``aggregate`` turns atoms into one row per client, job, partner, manager or service with the three
components (Time, Fixed fee, Expense), the total, the age buckets of the item dates as at the
report day and the oldest date. Pure: the same code serves today's WIP and a frozen snapshot.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date

from app.core.exceptions import ValidationError
from app.repositories.wip_atoms import WipAtom, WipFilters
from app.utils.money import percentage_bp
from app.utils.pagination import Page, PageParams, decode_cursor, encode_cursor
from app.utils.wip_rules import age_bucket, age_days, empty_buckets

BILLING_TYPE_LABELS = {"hourly": "Hourly", "fixed": "Fixed fee", "non_billable": "Non-billable"}


def matches(atom: WipAtom, f: WipFilters) -> bool:
    """The in-memory twin of the SQL filters (used on frozen snapshots)."""
    if f.kinds is not None and atom.kind not in f.kinds:
        return False
    if f.client_id is not None and atom.client_id != f.client_id:
        return False
    if f.job_id is not None and atom.job_id != f.job_id:
        return False
    if f.service_id is not None and atom.service_id != f.service_id:
        return False
    partner = atom.client_partner_user_id if f.client_responsible else atom.partner_user_id
    manager = atom.client_manager_user_id if f.client_responsible else atom.manager_user_id
    if f.partner_user_id is not None and partner != f.partner_user_id:
        return False
    return not (f.manager_user_id is not None and manager != f.manager_user_id)


@dataclass(slots=True)
class Group:
    """The figures of one row of a summary."""

    group_by: str
    key: uuid.UUID | None
    name: str
    code: str | None = None
    client_id: uuid.UUID | None = None
    client_code: str | None = None
    client_name: str | None = None
    billing_type: str | None = None
    partner_user_id: uuid.UUID | None = None
    manager_user_id: uuid.UUID | None = None
    minutes: int = 0
    time: int = 0
    fixed_fee: int = 0
    expense: int = 0
    ages: dict[str, int] = field(default_factory=empty_buckets)
    oldest: date | None = None
    count: int = 0
    # The legacy figures of ``GET /wip``: all time at its charge value, disbursements, expense lines.
    time_value: int = 0
    disbursement_value: int = 0
    expense_value: int = 0
    # Ledger write-ons and write-offs up to the day (historic WIP), as positive numbers.
    write_ons: int = 0
    write_offs: int = 0

    @property
    def total(self) -> int:
        return self.time + self.fixed_fee + self.expense

    @property
    def over_90_bp(self) -> int | None:
        return percentage_bp(self.ages["90_plus"], self.total) if self.total > 0 else None

    def add(self, atom: WipAtom, as_at: date) -> None:
        self.count += 1
        self.minutes += atom.minutes
        if atom.component == "time":
            self.time += atom.value
        elif atom.component == "fixed_fee":
            self.fixed_fee += atom.value
        else:
            self.expense += atom.value
        self.ages[age_bucket(age_days(atom.item_date, as_at))] += atom.value
        if self.oldest is None or atom.item_date < self.oldest:
            self.oldest = atom.item_date
        if atom.kind == "time":
            self.time_value += atom.charge_value if atom.charge_value is not None else atom.value
        elif atom.kind == "disbursement":
            self.disbursement_value += atom.value
        elif atom.kind == "expense":
            self.expense_value += atom.value


def group_key(atom: WipAtom, group_by: str, client_responsible: bool = False) -> tuple[object, ...]:
    if group_by == "client":
        return (atom.client_id,)
    if group_by == "job":
        return (atom.client_id, atom.job_id)
    if group_by == "billing_type":
        return (atom.billing_basis,)
    if group_by == "partner":
        return (atom.client_partner_user_id if client_responsible else atom.partner_user_id,)
    if group_by == "manager":
        return (atom.client_manager_user_id if client_responsible else atom.manager_user_id,)
    return (atom.service_id,)


def new_group(atom: WipAtom, group_by: str, key: tuple[object, ...]) -> Group:
    """An empty group for the atom's key (identity only: nothing is added yet)."""
    if group_by == "client":
        return Group(
            group_by,
            atom.client_id,
            atom.client_name,
            atom.client_code,
            client_id=atom.client_id,
            client_code=atom.client_code,
            client_name=atom.client_name,
            partner_user_id=atom.client_partner_user_id,
            manager_user_id=atom.client_manager_user_id,
        )
    if group_by == "job":
        return Group(
            group_by,
            atom.job_id,
            atom.job_name or "No job",
            atom.job_number,
            client_id=atom.client_id,
            client_code=atom.client_code,
            client_name=atom.client_name,
            billing_type=atom.billing_basis,
        )
    if group_by == "billing_type":
        basis = key[0] if isinstance(key[0], str) else None
        return Group(group_by, None, BILLING_TYPE_LABELS.get(str(basis), "No job"), billing_type=basis)
    assert key[0] is None or isinstance(key[0], uuid.UUID)
    return Group(group_by, key[0], "")


def aggregate(
    atoms: list[WipAtom], group_by: str, as_at: date, *, client_responsible: bool = False
) -> dict[tuple[object, ...], Group]:
    """One group per key. Names of people and services are filled in by the caller (``name`` is
    empty for those groups)."""
    groups: dict[tuple[object, ...], Group] = {}
    for atom in atoms:
        key = group_key(atom, group_by, client_responsible)
        group = groups.get(key)
        if group is None:
            group = groups[key] = new_group(atom, group_by, key)
        group.add(atom, as_at)
    return groups


def totals_of(atoms: list[WipAtom], as_at: date) -> Group:
    """The figures of all the atoms together."""
    total = Group("client", None, "Total")
    for atom in atoms:
        total.add(atom, as_at)
    return total


def sum_groups(groups: list[Group]) -> Group:
    """The figures of the groups together (the totals row of a filtered, paged list)."""
    total = Group("client", None, "Total")
    for g in groups:
        total.minutes += g.minutes
        total.time += g.time
        total.fixed_fee += g.fixed_fee
        total.expense += g.expense
        total.count += g.count
        total.time_value += g.time_value
        total.disbursement_value += g.disbursement_value
        total.expense_value += g.expense_value
        for bucket, value in g.ages.items():
            total.ages[bucket] += value
        if g.oldest is not None and (total.oldest is None or g.oldest < total.oldest):
            total.oldest = g.oldest
    return total


@dataclass(frozen=True, slots=True)
class SortSpec:
    key: str
    descending: bool


SORT_KEYS = ("name", "total", "time", "fixed_fee", "expense", "oldest", "over_90", "items")


def sort_groups(groups: list[Group], spec: SortSpec) -> list[Group]:
    """Sorted by the column, ties broken by name then id, so a page boundary is stable."""

    def value(group: Group) -> int | str:
        match spec.key:
            case "total":
                return group.total
            case "time":
                return group.time
            case "fixed_fee":
                return group.fixed_fee
            case "expense":
                return group.expense
            case "oldest":
                return group.oldest.toordinal() if group.oldest else 0
            case "over_90":
                return group.ages["90_plus"]
            case "items":
                return group.count
            case _:
                return group.name.lower()

    ordered = sorted(groups, key=lambda g: (g.name.lower(), str(g.key), str(g.client_id)))
    return sorted(ordered, key=value, reverse=spec.descending)


_ZERO_ID = uuid.UUID(int=0)


def page_of[T](rows: list[T], params: PageParams) -> Page[T]:
    """One page of an ordered list. The cursor is the offset of the next row, in the same opaque
    form as every other cursor; the figures are recomputed on each request."""
    offset = 0
    if params.cursor:
        value, _ = decode_cursor(params.cursor)
        if not isinstance(value, int) or value < 0:
            raise ValidationError.for_field("cursor", "Invalid cursor.")
        offset = value
    chunk = rows[offset : offset + params.limit]
    more = offset + params.limit < len(rows)
    return Page(items=chunk, next_cursor=encode_cursor(offset + params.limit, _ZERO_ID) if more else None)
