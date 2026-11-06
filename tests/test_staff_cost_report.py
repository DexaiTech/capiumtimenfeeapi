"""The Staff Cost report (APT81, phase D): access (`report.staff_cost`, plus `report.export` to
download), the filters, and the CSV, Excel and PDF downloads."""

from datetime import timedelta

from httpx import AsyncClient

from tests.domain_factories import staff
from tests.factories import add_member, audit_entries
from tests.foundation_helpers import add_custom_role
from tests.overtime_helpers import MONDAY, log, scene
from tests.reports_helpers import fetch, report
from tests.team_helpers import grade_id

WEEK = {"date_from": MONDAY, "date_to": MONDAY + timedelta(days=6)}


async def overtime_week(client: AsyncClient):
    """Sam: 600 standard minutes; 60 overtime minutes on Job X and 30 on the Internal client."""
    s = await scene(client, standard_minutes=600)
    await log(client, s, MONDAY, 600)
    await log(client, s, MONDAY + timedelta(days=1), 60, confirm=True)
    await log(client, s, MONDAY + timedelta(days=2), 30, s.internal_job, confirm=True)
    return s


async def test_only_report_staff_cost_opens_it(client: AsyncClient) -> None:
    s = await overtime_week(client)
    manager = await staff(s.org, "manager")
    for name in ("staff-cost", "staff-cost/jobs"):
        assert (await fetch(client, manager, s.org, name, **WEEK)).status_code == 403
        assert (await fetch(client, s.person, s.org, name, **WEEK)).status_code == 403
    partner = await staff(s.org, "partner")
    assert (await fetch(client, partner, s.org, "staff-cost", **WEEK)).status_code == 200
    # The permission alone shows the report but downloads need report.export as well.
    await add_custom_role(s.org["id"], "Costs", ["report.staff_cost"])
    costs = await add_member(s.org["id"], "Costs")
    assert (await fetch(client, costs, s.org, "staff-cost", **WEEK)).status_code == 200
    csv = await fetch(client, costs, s.org, "staff-cost", format="csv", **WEEK)
    assert csv.status_code == 403


async def test_the_filters(client: AsyncClient) -> None:
    s = await overtime_week(client)

    async def totals(name: str = "staff-cost", **params) -> dict:
        return (await report(client, s.owner, s.org, name, **WEEK, **params))["totals"]

    everything = await totals()
    assert (
        everything["standard_minutes"],
        everything["overtime_minutes_allocated"],
        everything["overtime_minutes_unallocated"],
    ) == (600, 60, 30)
    allocated = await totals(overtime_type="allocated")
    assert (allocated["overtime_minutes_allocated"], allocated["overtime_minutes_unallocated"]) == (
        60,
        0,
    )
    assert (await report(client, s.owner, s.org, "staff-cost", status="approved", **WEEK))[
        "rows"
    ] == []
    assert (await totals(job_id=s.job_x.id))["overtime_minutes_unallocated"] == 0
    acme = await totals(client_id=s.acme.id)
    assert (acme["standard_minutes"], acme["overtime_minutes_unallocated"]) == (600, 0)
    assert (await totals(grade_id=await grade_id(s.org, "SNR")))["total_cost"] == everything[
        "total_cost"
    ]
    assert (await report(client, s.owner, s.org, "staff-cost", user_id=s.owner.id, **WEEK))[
        "rows"
    ] == []
    unknown = await fetch(
        client, s.owner, s.org, "staff-cost", grade_id="00000000-0000-0000-0000-000000000000"
    )
    assert unknown.status_code == 404
    jobs = await report(
        client, s.owner, s.org, "staff-cost/jobs", overtime_type="unallocated", **WEEK
    )
    assert [(r["client"]["name"], r["overtime_minutes"]) for r in jobs["rows"]] == [
        ("Internal", 30)
    ]


async def test_downloads(client: AsyncClient) -> None:
    s = await overtime_week(client)
    for name in ("staff-cost", "staff-cost/jobs"):
        csv = await fetch(client, s.owner, s.org, name, format="csv", **WEEK)
        assert csv.status_code == 200, csv.text
        assert csv.headers["content-type"].startswith("text/csv")
        text = csv.content.decode("utf-8-sig")
        assert "Overtime cost" in text.splitlines()[0]
        assert text.splitlines()[-1].startswith("Total")
        for fmt, media in (("xlsx", "spreadsheetml"), ("pdf", "application/pdf")):
            file = await fetch(client, s.owner, s.org, name, format=fmt, **WEEK)
            assert file.status_code == 200 and media in file.headers["content-type"]
    csv = await fetch(client, s.owner, s.org, "staff-cost", format="csv", **WEEK)
    assert "Sam Senior" in csv.content.decode("utf-8-sig")
    exported = await audit_entries(s.org["id"], "report.exported")
    assert {e["metadata"]["report"] for e in exported} == {"staff-cost", "staff-cost-jobs"}
