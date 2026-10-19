"""Expenses as work in progress: ``wip_eligible_expenses`` is the one function the WIP and billing
modules call. An expense line is WIP when its claim is approved and it is billable, has a client,
is unbilled and has something to recharge."""

import dataclasses
import uuid
from datetime import date

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.db.models import Expense
from app.db.session import get_sessionmaker
from app.repositories.expense_lines import (
    WipExpense,
    wip_eligible_expenses,
    wip_expense_conditions,
)
from tests.billing_helpers import set_org
from tests.clients_helpers import sql
from tests.domain_factories import make_client, make_job, vat_rate_id
from tests.expense_helpers import (
    act,
    add_line,
    create_claim,
    expense_team,
    make_claim,
    make_line,
    patch_line,
)
from tests.factories import add_member

D1, D2, D3 = date(2026, 9, 1), date(2026, 9, 5), date(2026, 9, 10)


async def wip(org: dict, **filters) -> list[WipExpense]:
    async with get_sessionmaker()() as session:
        return await wip_eligible_expenses(session, uuid.UUID(org["id"]), **filters)


async def scene(client):
    """Approved claims with three lines that are WIP and a mix of lines that are not."""
    team = await expense_team(client)
    org = team.org
    staff = await team.member("staff")
    acme = await make_client(org, "Acme Ltd")
    beta = await make_client(org, "Beta Ltd")
    job = await make_job(org, acme, "Audit")
    standard = await vat_rate_id(org, "standard")
    approved = await make_claim(org, staff, status="approved", decided_by=team.owner)
    ids = {
        "ok1": await make_line(
            org,
            approved,
            client=acme,
            job=job,
            is_billable=True,
            net=1000,
            vat=200,
            recharge_amount=1200,
            incurred_on=D1,
            description="Train to Leeds",
            supplier="LNER",
            vat_rate_id=standard,
            vat_rate_bp=2000,
        ),
        "ok2": await make_line(
            org, approved, client=acme, is_billable=True, recharge_amount=500, incurred_on=D2
        ),
        "ok3": await make_line(
            org, approved, client=beta, is_billable=True, recharge_amount=700, incurred_on=D3
        ),
        # Lines of an approved claim that are not WIP.
        "not_billable": await make_line(org, approved, client=acme, incurred_on=D1),
        "nothing_to_recharge": await make_line(
            org, approved, client=acme, is_billable=True, recharge_amount=0, incurred_on=D1
        ),
        "in_draft": await make_line(
            org,
            approved,
            client=acme,
            is_billable=True,
            recharge_amount=300,
            billing_status="in_draft",
        ),
        "billed": await make_line(
            org,
            approved,
            client=acme,
            is_billable=True,
            recharge_amount=300,
            billing_status="billed",
        ),
        "written_off": await make_line(
            org,
            approved,
            client=acme,
            is_billable=True,
            recharge_amount=300,
            billing_status="written_off",
        ),
    }
    # Billable lines of claims that are not approved.
    for status in ("draft", "submitted", "returned"):
        claim = await make_claim(org, staff, status=status, decided_by=team.owner)
        ids[f"claim_{status}"] = await make_line(
            org, claim, client=acme, is_billable=True, recharge_amount=900
        )
    return team, staff, acme, beta, job, standard, ids


async def test_only_approved_billable_unbilled_lines_with_a_recharge_are_work_in_progress(client):
    team, staff, acme, beta, job, standard, ids = await scene(client)
    lines = await wip(team.org)
    assert [line.id for line in lines] == [ids["ok1"], ids["ok2"], ids["ok3"]]  # oldest first
    first = lines[0]
    approved_claim = first.claim_id
    assert first == WipExpense(
        id=ids["ok1"],
        claim_id=approved_claim,
        user_id=staff.id,
        client_id=acme.id,
        job_id=job.id,
        incurred_on=D1,
        description="Train to Leeds",
        supplier="LNER",
        net=1000,
        vat=200,
        recharge_amount=1200,
        vat_rate_id=standard,
        vat_rate_bp=2000,
    )
    assert (lines[1].client_id, lines[1].job_id, lines[1].vat_rate_id) == (acme.id, None, None)
    assert (lines[2].client_id, lines[2].recharge_amount) == (beta.id, 700)
    assert {line.claim_id for line in lines} == {approved_claim}
    # The contract is a frozen record with exactly these fields.
    assert [f.name for f in dataclasses.fields(WipExpense)] == [
        "id",
        "claim_id",
        "user_id",
        "client_id",
        "job_id",
        "incurred_on",
        "description",
        "supplier",
        "net",
        "vat",
        "recharge_amount",
        "vat_rate_id",
        "vat_rate_bp",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.net = 1  # type: ignore[misc]


async def test_another_practices_lines_are_never_wip_here(client):
    team, _staff, _acme, *_ = await scene(client)
    other = await expense_team(client, "Other Practice")
    their_staff = await other.member("staff")
    their_client = await make_client(other.org, "Their Client")
    claim = await make_claim(other.org, their_staff, status="approved", decided_by=other.owner)
    theirs = await make_line(
        other.org, claim, client=their_client, is_billable=True, recharge_amount=5000
    )
    assert [line.id for line in await wip(other.org)] == [theirs]
    assert theirs not in {line.id for line in await wip(team.org)}
    assert len(await wip(team.org)) == 3
    # Asking for another practice's client or line ids finds nothing.
    assert await wip(team.org, client_id=their_client.id) == []
    assert await wip(team.org, ids=[theirs]) == []


async def test_the_filters_narrow_the_lines(client):
    team, _staff, acme, beta, job, _standard, ids = await scene(client)
    org = team.org

    async def found(**filters) -> list[uuid.UUID]:
        return [line.id for line in await wip(org, **filters)]

    assert await found(client_id=acme.id) == [ids["ok1"], ids["ok2"]]
    assert await found(client_id=beta.id) == [ids["ok3"]]
    assert await found(client_id=uuid.uuid4()) == []
    assert await found(job_id=job.id) == [ids["ok1"]]
    assert await found(job_id=uuid.uuid4()) == []
    # The dates are inclusive.
    assert await found(date_from=D2) == [ids["ok2"], ids["ok3"]]
    assert await found(date_to=D2) == [ids["ok1"], ids["ok2"]]
    assert await found(date_from=D2, date_to=D2) == [ids["ok2"]]
    assert await found(date_from=date(2026, 9, 6), date_to=date(2026, 9, 9)) == []
    assert await found(date_from=D3, date_to=D1) == []
    # Only the lines asked for, and only if they are WIP.
    assert await found(ids=[ids["ok3"], ids["ok1"]]) == [ids["ok1"], ids["ok3"]]
    assert await found(ids=[ids["ok1"], ids["billed"], ids["claim_draft"], uuid.uuid4()]) == [
        ids["ok1"]
    ]
    assert await found(ids=[]) == []
    assert await found(ids=set()) == []
    # They combine.
    assert await found(client_id=acme.id, date_from=D2) == [ids["ok2"]]
    assert await found(client_id=acme.id, ids=[ids["ok3"]]) == []


async def test_lines_on_the_same_day_come_in_a_stable_order(client):
    team = await expense_team(client)
    org = team.org
    staff = await team.member("staff")
    acme = await make_client(org, "Acme Ltd")
    claim = await make_claim(org, staff, status="approved", decided_by=team.owner)
    made = [
        await make_line(
            org, claim, client=acme, is_billable=True, recharge_amount=100, incurred_on=D1
        )
        for _ in range(4)
    ]
    first = [line.id for line in await wip(org)]
    assert sorted(first) == sorted(made)
    assert [line.id for line in await wip(org)] == first
    assert first == sorted(first)  # by id within the day


async def test_the_lines_can_be_locked_for_billing(client):
    team, *_ = await scene(client)
    org_id = uuid.UUID(team.org["id"])
    async with get_sessionmaker()() as billing:
        locked = await wip_eligible_expenses(billing, org_id, lock=True)
        assert len(locked) == 3
        async with get_sessionmaker()() as other:
            for line in locked:
                with pytest.raises(DBAPIError):  # somebody else cannot take the line meanwhile
                    await other.execute(
                        text("SELECT id FROM expenses WHERE id = :i FOR UPDATE NOWAIT"),
                        {"i": line.id},
                    )
                await other.rollback()
            # Lines that are not WIP were not locked.
            free = await other.scalar(
                text("SELECT id FROM expenses WHERE billing_status = 'billed' FOR UPDATE NOWAIT")
            )
            assert free is not None
        await billing.rollback()
    # Without ``lock`` nothing is held.
    async with get_sessionmaker()() as reader, get_sessionmaker()() as other:
        assert len(await wip_eligible_expenses(reader, org_id)) == 3
        assert (
            await other.scalar(
                text("SELECT count(*) FROM (SELECT id FROM expenses FOR UPDATE NOWAIT) s")
            )
            == 11
        )


async def test_the_aggregating_conditions_are_the_same_rule(client):
    team, _staff, acme, beta, _job, _standard, _ids = await scene(client)
    org_id = uuid.UUID(team.org["id"])
    async with get_sessionmaker()() as session:
        rows = (
            await session.execute(
                select(Expense.client_id, func.sum(Expense.recharge_amount), func.count())
                .where(*wip_expense_conditions(org_id))
                .group_by(Expense.client_id)
            )
        ).all()
        by_client = {row[0]: (int(row[1]), row[2]) for row in rows}
    assert by_client == {acme.id: (1700, 2), beta.id: (700, 1)}
    lines = await wip(team.org)
    assert sum(line.recharge_amount for line in lines) == sum(v[0] for v in by_client.values())


async def test_the_partial_index_for_wip_exists(client):
    [row] = await sql("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_expenses_wip'")
    definition = row["indexdef"]
    assert "(organization_id, client_id, incurred_on)" in definition
    assert "is_billable" in definition and "billing_status = 'unbilled'" in definition
    assert "recharge_amount > 0" in definition


# --- Through the workflow -------------------------------------------------------------------


async def test_a_billable_line_becomes_wip_when_its_claim_is_approved_and_stops_when_it_changes(
    client,
):
    team = await expense_team(client)
    org, owner = team.org, team.owner
    await set_org(org, expense_markup_bp=1_000)
    acme = await make_client(org, "Acme Ltd")
    staff = await team.member("staff")
    manager = await add_member(org["id"], "manager")
    claim = await create_claim(client, staff, org)
    cid = claim["id"]
    line = await add_line(client, staff, org, cid, client_id=str(acme.id), is_billable=True)
    lid = line["id"]
    assert line["recharge_amount"] == 1100 and line["billing_status"] == "unbilled"

    async def wip_ids() -> list[str]:
        return [str(w.id) for w in await wip(org)]

    # Nothing before approval: not as a draft, waiting, or returned.
    assert await wip_ids() == []
    await act(client, staff, org, cid, "submit")
    assert await wip_ids() == []
    await act(client, manager, org, cid, "return", json={"reason": "Redo"})
    assert await wip_ids() == []
    await act(client, staff, org, cid, "submit")
    assert await wip_ids() == []
    await act(client, owner, org, cid, "approve")
    [found] = await wip(org)
    assert (str(found.id), found.client_id, found.recharge_amount) == (lid, acme.id, 1100)
    assert (found.net, found.vat, str(found.claim_id), found.user_id) == (1000, 0, cid, staff.id)

    # A correction is seen at once.
    await patch_line(client, owner, org, cid, lid, net=2000, reason="Receipt says 20.00")
    assert [w.recharge_amount for w in await wip(org)] == [2200]
    await patch_line(client, owner, org, cid, lid, recharge_amount=0, reason="Absorb it")
    assert await wip_ids() == []
    await patch_line(client, owner, org, cid, lid, recharge_amount=500, reason="Recharge 5.00")
    assert [w.recharge_amount for w in await wip(org)] == [500]
    await patch_line(client, owner, org, cid, lid, is_billable=False, reason="Not billable")
    assert await wip_ids() == []
    await patch_line(client, owner, org, cid, lid, is_billable=True, reason="Billable again")
    assert [w.recharge_amount for w in await wip(org)] == [2200]

    # Once billing has taken it, it is no longer WIP; released, it is again.
    for status in ("in_draft", "billed", "written_off"):
        await sql("UPDATE expenses SET billing_status = :s WHERE id = :i", s=status, i=lid)
        assert await wip_ids() == [], status
    await sql("UPDATE expenses SET billing_status = 'unbilled' WHERE id = :i", i=lid)
    assert await wip_ids() == [lid]


async def test_a_line_without_a_client_or_that_is_not_billable_is_never_wip(client):
    team = await expense_team(client)
    org, owner = team.org, team.owner
    staff = await team.member("staff")
    claim = await create_claim(client, staff, org, lines=2)  # no client: not billable
    await act(client, staff, org, claim["id"], "submit")
    await act(client, owner, org, claim["id"], "approve")
    assert [line["is_billable"] for line in claim["lines"]] == [False, False]
    assert await wip(org) == []
    # The database does not let a billable line have no client either.
    with pytest.raises(DBAPIError):
        await sql(
            "UPDATE expenses SET is_billable = true, recharge_amount = 100 WHERE claim_id = :c",
            c=claim["id"],
        )
