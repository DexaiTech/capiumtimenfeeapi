"""Budget tracking: budget against actual per job, the totals, the filters and the breakdown."""

from httpx import AsyncClient

from tests.domain_factories import make_client, make_staff_rate, practice, service_id, staff
from tests.job_helpers import add_task, create_job, job_url, log_on_job, role_without, set_job_row
from tests.time_helpers import err, ok_data, org_url

URL = "/jobs/budgets"


async def _budgets(client: AsyncClient, actor, org, **params):
    response = await client.get(org_url(org, URL), headers=actor.headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _names(body: dict) -> list[str]:
    return [row["name"] for row in body["data"]["rows"]]


async def _scenario(client: AsyncClient):
    """Four jobs with budgets (one of each band) and one without; £60 an hour (100p a minute)."""
    owner, org = await practice(client)
    acme, beta = await make_client(org, "Acme Ltd"), await make_client(org, "Beta plc")
    manager = await staff(org, "manager", full_name="Mia Manager")
    partner = await staff(org, "partner", full_name="Pat Partner")
    books = await service_id(org, "BOOKS")
    await make_staff_rate(org, owner, charge_rate=6000, cost_rate=3000)
    common = {"hourly_rate": 6000}
    alpha = await create_job(
        client, owner, org, acme, "Alpha", budget_minutes=600, budget_fee=60_000,
        manager_user_id=manager.id, partner_user_id=partner.id, service_id=books,
        due_date="2026-12-01", **common,
    )  # fmt: skip
    bravo = await create_job(
        client, owner, org, acme, "Bravo", budget_minutes=600, due_date="2026-10-01", **common
    )
    charlie = await create_job(
        client, owner, org, beta, "Charlie", budget_fee=10_000, status="in_progress", **common
    )
    await create_job(client, owner, org, beta, "Delta", **common)
    echo = await create_job(client, owner, org, beta, "Echo", budget_minutes=600, **common)
    await log_on_job(client, owner, org, alpha, 150)
    await log_on_job(client, owner, org, bravo, 480)
    await log_on_job(client, owner, org, charlie, 120)
    await set_job_row(echo["id"], status="completed")
    jobs = {"alpha": alpha, "bravo": bravo, "charlie": charlie, "echo": echo}
    return owner, org, jobs, {"acme": acme, "beta": beta, "manager": manager, "partner": partner}


async def test_the_list_with_bands_and_totals(client: AsyncClient) -> None:
    owner, org, jobs, _ = await _scenario(client)
    body = await _budgets(client, owner, org)
    # The most used job first; a job without a budget is not tracked.
    assert _names(body) == ["Charlie", "Bravo", "Alpha", "Echo"]
    rows = {row["name"]: row for row in body["data"]["rows"]}

    alpha = rows["Alpha"]
    assert alpha["job_id"] == jobs["alpha"]["id"] and alpha["number"] == jobs["alpha"]["number"]
    assert (alpha["budget_minutes"], alpha["logged_minutes"], alpha["billable_minutes"]) == (
        600,
        150,
        150,
    )
    assert (alpha["budget_fee"], alpha["time_value"], alpha["wip_value"]) == (
        60_000,
        15_000,
        15_000,
    )
    assert (alpha["budget_minutes_used_bp"], alpha["budget_fee_used_bp"]) == (2500, 2500)
    assert (alpha["used_bp"], alpha["rag"]) == (2500, "green")
    assert (alpha["client_name"], alpha["service_name"]) == ("Acme Ltd", "Bookkeeping")
    assert (alpha["manager_name"], alpha["partner_name"]) == ("Mia Manager", "Pat Partner")
    assert (alpha["status"], alpha["billing_basis"], alpha["due_date"]) == (
        "planned",
        "hourly",
        "2026-12-01",
    )
    assert alpha["alerts"] == []
    # Cost and margin: £30 an hour against £60 an hour.
    assert (alpha["cost_value"], alpha["margin_value"], alpha["margin_bp"]) == (7500, 7500, 5000)

    bravo = rows["Bravo"]
    assert (bravo["used_bp"], bravo["rag"]) == (8000, "amber")
    assert bravo["budget_fee"] is None and bravo["budget_fee_used_bp"] is None
    assert [(a["kind"], a["threshold"]) for a in bravo["alerts"]] == [("minutes", 80)]
    charlie = rows["Charlie"]
    assert (charlie["used_bp"], charlie["rag"]) == (12_000, "red")
    assert charlie["budget_minutes"] is None and charlie["budget_minutes_used_bp"] is None
    assert charlie["budget_fee_used_bp"] == 12_000
    assert [(a["kind"], a["threshold"]) for a in charlie["alerts"]] == [("fee", 80), ("fee", 100)]
    echo = rows["Echo"]
    assert (echo["used_bp"], echo["rag"], echo["status"]) == (0, "green", "completed")

    totals = body["data"]["totals"]
    assert (totals["jobs"], totals["green"], totals["amber"], totals["red"]) == (4, 2, 1, 1)
    assert (totals["budget_minutes"], totals["logged_minutes"]) == (1800, 750)
    assert totals["budget_minutes_used_bp"] == 3500  # 630 of 1800: Charlie has no hours budget
    assert (totals["budget_fee"], totals["time_value"], totals["wip_value"]) == (
        70_000,
        75_000,
        75_000,
    )
    assert totals["budget_fee_used_bp"] == 3857  # 27000 of 70000: Bravo has no fee budget
    assert (totals["cost_value"], totals["margin_value"], totals["margin_bp"]) == (
        37_500,
        37_500,
        5000,
    )
    assert body["meta"]["has_more"] is False and body["meta"]["next_cursor"] is None


async def test_cost_and_margin_need_rate_read(client: AsyncClient) -> None:
    owner, org, _, _ = await _scenario(client)
    roles = (
        ("manager", True),
        ("partner", True),
        ("staff", False),
        ("viewer", False),
        ("billing", False),
    )
    for role, sees in roles:
        person = await staff(org, role)
        body = await _budgets(client, person, org)
        row = body["data"]["rows"][0]
        totals = body["data"]["totals"]
        figures = (row["cost_value"], row["margin_value"], row["margin_bp"])
        summary = (totals["cost_value"], totals["margin_value"], totals["margin_bp"])
        assert (None not in figures and None not in summary) is sees, role
        assert row["time_value"] == 12_000  # the value of the time is not a cost figure
    viewer = await staff(org, "viewer")
    first = (await _budgets(client, owner, org))["data"]["rows"][0]["job_id"]
    detail = ok_data(await client.get(job_url(org, first, "/budget"), headers=viewer.headers))
    assert detail["cost_value"] is None and detail["margin_value"] is None
    assert all(p["cost_value"] is None for p in detail["by_person"])
    assert all(t["cost_value"] is None for t in detail["by_task"])
    # With rate.read the same job shows its cost.
    manager = await staff(org, "manager")
    seen = ok_data(await client.get(job_url(org, first, "/budget"), headers=manager.headers))
    assert seen["cost_value"] == 6000 and seen["margin_value"] == 6000
    assert [p["cost_value"] for p in seen["by_person"]] == [6000]


async def test_filters(client: AsyncClient) -> None:
    owner, org, jobs, people = await _scenario(client)

    async def names(**params) -> list[str]:
        return _names(await _budgets(client, owner, org, **params))

    assert await names(status="completed") == ["Echo"]
    assert await names(status=["planned", "in_progress"]) == ["Charlie", "Bravo", "Alpha"]
    assert await names(client_id=people["acme"].id) == ["Bravo", "Alpha"]
    assert await names(client_id=people["beta"].id) == ["Charlie", "Echo"]
    assert await names(manager_user_id=people["manager"].id) == ["Alpha"]
    assert await names(partner_user_id=people["partner"].id) == ["Alpha"]
    assert await names(manager_user_id="me") == []
    assert await names(service_id=await service_id(org, "BOOKS")) == ["Alpha"]
    assert await names(used_from_bp=8000) == ["Charlie", "Bravo"]
    assert await names(used_from_bp=8001) == ["Charlie"]
    assert await names(used_to_bp=2500) == ["Alpha", "Echo"]
    assert await names(used_from_bp=2500, used_to_bp=8000) == ["Bravo", "Alpha"]
    assert await names(q="rav") == ["Bravo"]
    assert await names(q="beta") == ["Charlie", "Echo"]
    assert await names(q=jobs["alpha"]["number"]) == ["Alpha"]
    assert await names(q="Delta") == []  # no budget, so not tracked
    # The totals follow the filters.
    body = await _budgets(client, owner, org, client_id=people["beta"].id)
    assert (body["data"]["totals"]["jobs"], body["data"]["totals"]["red"]) == (2, 1)
    empty = await _budgets(client, owner, org, q="nothing like this")
    assert empty["data"]["rows"] == [] and empty["data"]["totals"]["jobs"] == 0
    assert empty["data"]["totals"]["budget_minutes_used_bp"] is None
    error = err(
        await client.get(
            org_url(org, URL),
            headers=owner.headers,
            params={"used_from_bp": 9000, "used_to_bp": 100},
        ),
        422,
    )
    assert error["details"][0]["field"] == "used_to_bp"
    for bad in (
        {"status": "bad"},
        {"sort": "bogus"},
        {"order": "up"},
        {"used_from_bp": -1},
        {"manager_user_id": "x"},
        {"cursor": "garbage"},
    ):
        assert (
            await client.get(org_url(org, URL), headers=owner.headers, params=bad)
        ).status_code == 422, bad


async def test_sorting_and_pagination(client: AsyncClient) -> None:
    owner, org, _, _ = await _scenario(client)

    async def names(**params) -> list[str]:
        return _names(await _budgets(client, owner, org, **params))

    assert await names(sort="used", order="asc") == ["Echo", "Alpha", "Bravo", "Charlie"]
    assert await names(sort="number") == ["Alpha", "Bravo", "Charlie", "Echo"]
    assert await names(sort="number", order="desc") == ["Echo", "Charlie", "Bravo", "Alpha"]
    assert await names(sort="name", order="desc") == ["Echo", "Charlie", "Bravo", "Alpha"]
    assert await names(sort="client") == ["Alpha", "Bravo", "Charlie", "Echo"]
    assert await names(sort="due_date") == ["Bravo", "Alpha", "Charlie", "Echo"]  # no due date last
    assert await names(sort="logged_minutes", order="desc") == ["Bravo", "Alpha", "Charlie", "Echo"]
    assert await names(sort="time_value", order="desc") == ["Bravo", "Alpha", "Charlie", "Echo"]
    for sort in ("used", "number", "name", "client", "due_date", "logged_minutes", "time_value"):
        for order in ("asc", "desc"):
            expected = await names(sort=sort, order=order)
            seen: list[str] = []
            cursor = None
            while True:
                params = {
                    "sort": sort,
                    "order": order,
                    "limit": 1,
                    **({"cursor": cursor} if cursor else {}),
                }
                body = await _budgets(client, owner, org, **params)
                seen += _names(body)
                cursor = body["meta"]["next_cursor"]
                if not cursor:
                    break
            assert seen == expected, (sort, order)
            # The totals cover every job, not the page.
            assert body["data"]["totals"]["jobs"] == 4


async def test_the_breakdown_of_one_job(client: AsyncClient) -> None:
    owner, org = await practice(client)
    ann = await staff(org, full_name="Ann Able")
    acme = await make_client(org)
    await make_staff_rate(org, owner, charge_rate=6000, cost_rate=3000)
    await make_staff_rate(org, ann, charge_rate=6000, cost_rate=1200)
    job = await create_job(
        client,
        owner,
        org,
        acme,
        "Accounts",
        budget_minutes=300,
        budget_fee=40_000,
        hourly_rate=6000,
    )
    draft = await add_task(client, owner, org, job, "Draft", budget_minutes=120)
    review = await add_task(client, owner, org, job, "Review")
    await add_task(client, owner, org, job, "Idle task")
    await log_on_job(client, owner, org, job, 90, task_id=draft["id"])
    await log_on_job(client, ann, org, job, 60, task_id=draft["id"], entry_date="2026-09-08")
    await log_on_job(client, ann, org, job, 30, task_id=review["id"], entry_date="2026-09-08")
    await log_on_job(client, owner, org, job, 120, entry_date="2026-09-09")  # no task
    detail = ok_data(await client.get(job_url(org, job["id"], "/budget"), headers=owner.headers))

    assert (detail["job_id"], detail["number"], detail["name"]) == (
        job["id"],
        job["number"],
        "Accounts",
    )
    assert (detail["client_name"], detail["status"], detail["billing_basis"]) == (
        acme.name,
        "planned",
        "hourly",
    )
    assert (detail["budget_minutes"], detail["logged_minutes"], detail["billable_minutes"]) == (
        300,
        300,
        300,
    )
    assert (detail["budget_fee"], detail["time_value"], detail["wip_value"]) == (
        40_000,
        30_000,
        30_000,
    )
    assert (detail["used_bp"], detail["rag"]) == (10_000, "red")  # 300 of 300 minutes
    assert (detail["remaining_minutes"], detail["remaining_fee"]) == (0, 10_000)
    assert (detail["billed_value"], detail["written_off_value"]) == (0, 0)
    # Owner: 210 minutes at £30 an hour; Ann: 90 minutes at £12 an hour.
    assert (detail["cost_value"], detail["margin_value"]) == (10_500 + 1_800, 30_000 - 12_300)

    by_task = {t["title"]: t for t in detail["by_task"] if t["task_id"]}
    assert by_task["Draft"]["minutes"] == 150 and by_task["Draft"]["used_bp"] == 12_500
    assert by_task["Draft"]["value"] == 15_000 and by_task["Draft"]["status"] == "not_started"
    assert (by_task["Review"]["minutes"], by_task["Review"]["used_bp"]) == (30, None)
    assert by_task["Idle task"]["minutes"] == 0
    unlinked = [t for t in detail["by_task"] if t["task_id"] is None]
    assert len(unlinked) == 1 and unlinked[0]["minutes"] == 120 and unlinked[0]["title"] is None
    assert unlinked[0]["value"] == 12_000 and unlinked[0]["used_bp"] is None
    assert [t["title"] for t in detail["by_task"]] == ["Draft", "Review", "Idle task", None]

    people = detail["by_person"]
    assert [(p["full_name"], p["minutes"]) for p in people] == [
        (owner.full_name, 210),
        ("Ann Able", 90),
    ]
    assert people[0]["value"] == 21_000 and people[0]["billable_minutes"] == 210
    assert people[1]["value"] == 9_000 and people[1]["cost_value"] == 1800
    assert sum(t["minutes"] for t in detail["by_task"]) == detail["logged_minutes"]


async def test_a_job_without_time_or_budget(client: AsyncClient) -> None:
    owner, org = await practice(client)
    job = await create_job(client, owner, org, await make_client(org), "Quiet")
    detail = ok_data(await client.get(job_url(org, job["id"], "/budget"), headers=owner.headers))
    assert (detail["used_bp"], detail["rag"]) == (None, None)
    assert (detail["remaining_minutes"], detail["remaining_fee"]) == (None, None)
    assert detail["by_task"] == [] and detail["by_person"] == [] and detail["alerts"] == []
    assert (detail["logged_minutes"], detail["time_value"]) == (0, 0)


async def test_access_and_tenancy(client: AsyncClient) -> None:
    owner, org, jobs, _ = await _scenario(client)
    other_owner, other = await practice(client, "Other & Co")
    theirs = await create_job(
        client, other_owner, other, await make_client(other), "Theirs", budget_minutes=60
    )
    for role in ("staff", "viewer", "billing", "manager"):
        person = await staff(org, role)
        assert (await client.get(org_url(org, URL), headers=person.headers)).status_code == 200, (
            role
        )
        detail = await client.get(
            job_url(org, jobs["alpha"]["id"], "/budget"), headers=person.headers
        )
        assert detail.status_code == 200, role
    nobody = await role_without(org, "job.read")
    assert (await client.get(org_url(org, URL), headers=nobody.headers)).status_code == 403
    assert (
        await client.get(job_url(org, jobs["alpha"]["id"], "/budget"), headers=nobody.headers)
    ).status_code == 403
    assert (await client.get(org_url(org, URL))).status_code == 401
    assert (
        await client.get(job_url(org, theirs["id"], "/budget"), headers=owner.headers)
    ).status_code == 404
    assert (await client.get(org_url(org, URL), headers=other_owner.headers)).status_code == 404
    names = _names(await _budgets(client, other_owner, other))
    assert names == ["Theirs"]
    unknown = job_url(org, "00000000-0000-4000-8000-000000000000", "/budget")
    assert (await client.get(unknown, headers=owner.headers)).status_code == 404
    await client.delete(job_url(org, jobs["echo"]["id"]), headers=owner.headers)
    assert (
        await client.get(job_url(org, jobs["echo"]["id"], "/budget"), headers=owner.headers)
    ).status_code == 404
    assert "Echo" not in _names(await _budgets(client, owner, org))


async def test_a_zero_budget_is_no_budget(client: AsyncClient) -> None:
    owner, org = await practice(client)
    job = await create_job(
        client, owner, org, await make_client(org), "Zero", budget_minutes=0, budget_fee=0
    )
    assert _names(await _budgets(client, owner, org)) == []
    detail = ok_data(await client.get(job_url(org, job["id"], "/budget"), headers=owner.headers))
    assert (detail["used_bp"], detail["rag"]) == (None, None)
