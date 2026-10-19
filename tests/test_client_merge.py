"""Merging clients: what happens in the practice when two central clients become one."""

import asyncio

from sqlalchemy import Column, ForeignKey, Integer, MetaData, Table

from app.db.base import Base
from app.repositories import client_merge as merge_repository
from app.repositories.client_merge import (
    CONFLICT_RULES,
    HANDLED_TABLES,
    HISTORY,
    MOVED_TABLES,
    REPOINTED,
)
from tests.central_helpers import (
    BAKERY,
    JOINERY,
    KESTREL,
    PLUMBING,
    Team,
    client_row,
    enable,
    enabled,
    link,
    new_team,
    process_changes,
    scalar,
    search,
    send_event,
    sql,
    sync_all,
    sync_client,
    sync_logs,
    unlink,
)
from tests.clients_helpers import create_client, org_url
from tests.factories import audit_entries, create_user_with_org, error
from tests.merge_helpers import client_counts, give_work
from tests.team_helpers import new_rate


async def merge(client, team, source: str, target: str, **kwargs):
    """Deliver the event "``source`` was merged into ``target``"."""
    return await send_event(
        client, team.owner, team.org, "merged", source, into_central_id=target, **kwargs
    )


async def two_clients(client, team, source: str = BAKERY, target: str = PLUMBING):
    one = (await enabled(client, team.owner, team.org, source))["client"]
    two = (await enabled(client, team.owner, team.org, target))["client"]
    return one, two


async def names_of(table: str, client_id: str, column: str = "name") -> list[str]:
    rows = await sql(
        f"SELECT {column} FROM {table} WHERE client_id = :c ORDER BY {column}",  # noqa: S608
        c=client_id,
    )
    return [row[column] for row in rows]


# --- Every table that refers to a client is handled -----------------------------------------


def referencing_tables(metadata: MetaData) -> set[str]:
    """Tables with a ``client_id`` column or a foreign key to ``clients`` (other than the
    ``clients`` table itself, whose ``merged_into_client_id`` the merge handles on its own)."""
    found: set[str] = set()
    for table in metadata.tables.values():
        if table.name == "clients":
            continue
        refers = [fk for fk in table.foreign_keys if fk.column.table.name == "clients"]
        if "client_id" in table.c or refers:
            found.add(table.name)
    return found


def test_every_table_that_refers_to_a_client_is_handled_by_the_merge():
    """Fails when a later module adds a table that names a client and the merge does not know
    it: decide there whether its rows move (REPOINTED / CONFLICT_RULES) or are history."""
    referencing = referencing_tables(Base.metadata)
    unhandled = referencing - HANDLED_TABLES
    assert not unhandled, (
        f"{sorted(unhandled)} refer to a client: handle them in app/repositories/client_merge.py"
    )
    stale = HANDLED_TABLES - referencing
    assert not stale, f"{sorted(stale)} are listed in the merge but do not refer to a client"
    # The registry is consistent with itself.
    assert set(MOVED_TABLES) == set(REPOINTED) | set(CONFLICT_RULES)
    assert set(MOVED_TABLES).isdisjoint(HISTORY)
    assert len(set(REPOINTED)) == len(REPOINTED) and len(set(MOVED_TABLES)) == len(MOVED_TABLES)
    assert set(REPOINTED) == {
        "jobs",
        "time_entries",
        "timers",
        "disbursements",
        "expenses",
        "wip_write_offs",
        "invoices",
        "credit_notes",
        "payments",
        "payment_allocations",
        "statement_sends",
    }
    assert set(CONFLICT_RULES) == {"client_contacts", "client_services", "rate_rules"}
    assert set(HISTORY) == {"rate_changes", "rate_rerun_items"}


def test_the_discovery_finds_a_table_added_later():
    future = MetaData()
    Table(
        "future_estimates",
        future,
        Column("id", Integer, primary_key=True),
        Column("client_id", Integer),
    )
    Table("future_tags", future, Column("id", Integer, primary_key=True))
    assert referencing_tables(future) - HANDLED_TABLES == {"future_estimates"}
    # A foreign key to clients counts even when the column has another name.
    clients = Table("clients", future, Column("id", Integer, primary_key=True))
    Table(
        "future_notes",
        future,
        Column("id", Integer, primary_key=True),
        Column("about", Integer, ForeignKey(clients.c.id)),
    )
    assert referencing_tables(future) - HANDLED_TABLES == {"future_estimates", "future_notes"}


# --- A merge moves everything -----------------------------------------------------------------------


async def test_a_merge_moves_every_row_of_every_table_to_the_survivor(client):
    team = await new_team(client)
    source, target = await two_clients(client, team)
    await give_work(client, team, source["id"])
    await give_work(
        client,
        team,
        target["id"],
        services=("ACCS",),
        contacts=(("Tia Target", False),),
        rates=False,
        timer_user=await team.member("manager"),
    )
    before_source = await client_counts(source["id"])
    before_target = await client_counts(target["id"])
    # Every table of the merge has something to move, so a table that is skipped shows.
    assert all(n > 0 for n in before_source.values()), before_source
    assert set(before_source) == set(MOVED_TABLES)

    response = await merge(client, team, BAKERY, PLUMBING, event_id="merge-1")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (data["outcome"], data["duplicate"], data["client_id"]) == (
        "applied",
        False,
        target["id"],
    )
    moved = sum(before_source.values())
    assert data["message"] == f"Merged {source['code']} into {target['code']}: {moved} rows moved."

    assert await client_counts(source["id"]) == dict.fromkeys(MOVED_TABLES, 0)
    assert await client_counts(target["id"]) == {
        table: before_target[table] + before_source[table] for table in MOVED_TABLES
    }

    # The source stays, archived, and points at the survivor.
    row = await client_row(source["id"])
    assert (row["status"], str(row["merged_into_client_id"])) == ("archived", target["id"])
    assert (row["central_client_id"], row["name"]) == (BAKERY, "Northgate Bakery Ltd")
    assert (await client_row(target["id"]))["status"] == "active"
    detail = (
        await client.get(org_url(team.org, f"/clients/{source['id']}"), headers=team.owner.headers)
    ).json()["data"]
    assert detail["merged_into_client_id"] == target["id"]
    assert detail["merged_into"] == {
        "id": target["id"],
        "code": target["code"],
        "name": target["name"],
    }

    # One audit entry with a count per table; no actor (the system did it).
    [entry] = await audit_entries(team.id, "client.central_merged")
    assert entry["actor_user_id"] is None and entry["target_id"] == target["id"]
    meta = entry["metadata"]
    assert meta["mode"] == "merged" and meta["event_id"] == "merge-1"
    assert meta["counts"] == before_source and set(meta["counts"]) == set(MOVED_TABLES)
    assert meta["merged_client"] == {
        "id": source["id"],
        "code": source["code"],
        "name": source["name"],
        "central_id": BAKERY,
    }
    assert meta["survivor"] == {
        "id": target["id"],
        "code": target["code"],
        "name": target["name"],
        "central_id": PLUMBING,
    }
    assert meta["left_behind"] == {} and meta["merged_clients_repointed"] == 0
    assert meta["rate_history_rows"] == before_source["rate_rules"]
    assert meta["contacts_demoted"] == 1  # the target already had a primary contact
    # ... and a log row the practice can read.
    logs = [
        r
        for r in await sync_logs(client, team.owner, team.org, entity_id=target["id"])
        if r["operation"] == "merged"
    ]
    [log] = logs
    assert (log["status"], log["external_id"], log["external_event_id"]) == (
        "ok",
        BAKERY,
        "merge-1",
    )
    assert log["details"]["counts"] == before_source

    # What the survivor shows now is both clients' work.
    jobs = await client.get(
        org_url(team.org, "/jobs"),
        headers=team.owner.headers,
        params={"client_id": target["id"], "limit": 100},
    )
    assert len(jobs.json()["data"]) == before_target["jobs"] + before_source["jobs"]
    invoices = await client.get(
        org_url(team.org, "/invoices"),
        headers=team.owner.headers,
        params={"client_id": target["id"], "limit": 100},
    )
    assert len(invoices.json()["data"]) == before_target["invoices"] + before_source["invoices"]
    payments = await client.get(
        org_url(team.org, "/payments"),
        headers=team.owner.headers,
        params={"client_id": target["id"], "limit": 100},
    )
    assert len(payments.json()["data"]) == before_target["payments"] + before_source["payments"]
    assert (
        await client.get(
            org_url(team.org, f"/invoices?client_id={source['id']}"), headers=team.owner.headers
        )
    ).json()["data"] == []


async def test_the_merge_keeps_money_and_allocations_consistent(client):
    team = await new_team(client)
    source, target = await two_clients(client, team)
    work = await give_work(client, team, source["id"])
    before = await sql(
        "SELECT i.id, i.number, i.status, i.net_total, i.gross_total, i.paid_total, i.credited_total "
        "FROM invoices i WHERE i.client_id = :c ORDER BY i.number",
        c=source["id"],
    )
    allocated = await sql(
        "SELECT p.id, a.invoice_id, a.amount FROM payment_allocations a "
        "JOIN payments p ON p.id = a.payment_id WHERE a.client_id = :c",
        c=source["id"],
    )
    assert len(allocated) == 1
    response = await merge(client, team, BAKERY, PLUMBING)
    assert response.json()["data"]["outcome"] == "applied"
    after = await sql(
        "SELECT i.id, i.number, i.status, i.net_total, i.gross_total, i.paid_total, i.credited_total "
        "FROM invoices i WHERE i.client_id = :c ORDER BY i.number",
        c=target["id"],
    )
    assert after == before  # the same invoices, the same figures, a new owner
    still = await sql(
        "SELECT p.id, a.invoice_id, a.amount FROM payment_allocations a "
        "JOIN payments p ON p.id = a.payment_id WHERE a.client_id = :c",
        c=target["id"],
    )
    assert still == allocated
    credit = await sql("SELECT client_id FROM credit_notes WHERE id = :i", i=work.credit_note_id)
    assert str(credit[0]["client_id"]) == target["id"]
    # The invoice and the payment still read as they did.
    invoice = await client.get(
        org_url(team.org, f"/invoices/{work.invoices[0]}"), headers=team.owner.headers
    )
    assert invoice.status_code == 200 and invoice.json()["data"]["client"]["id"] == target["id"]
    assert invoice.json()["data"]["credited_total"] == 1_200
    # The WIP the source had (the open time and expense) is the survivor's now.
    wip = await client.get(org_url(team.org, "/wip"), headers=team.owner.headers)
    assert [row["client"]["id"] for row in wip.json()["data"]] == [target["id"]]
    assert wip.json()["data"][0]["total"] == 45 * 12_000 // 60 + 2_500


async def test_merging_clients_with_no_work_still_archives_the_source(client):
    team = await new_team(client)
    source, target = await two_clients(client, team)
    response = await merge(client, team, BAKERY, PLUMBING)
    assert (
        response.json()["data"]["message"]
        == f"Merged {source['code']} into {target['code']}: 0 rows moved."
    )
    [entry] = await audit_entries(team.id, "client.central_merged")
    assert entry["metadata"]["counts"] == dict.fromkeys(MOVED_TABLES, 0)
    assert (await client_row(source["id"]))["status"] == "archived"


# --- When both have the same thing ------------------------------------------------------------------


async def test_conflicts_are_resolved_in_favour_of_the_survivor_and_reported(client):
    team = await new_team(client)
    source, target = await two_clients(client, team)
    from tests.domain_factories import service_id

    vat, books = await service_id(team.org, "VAT"), await service_id(team.org, "BOOKS")

    async def engage(client_id: str, service, fee: int) -> dict:
        response = await client.post(
            org_url(team.org, f"/clients/{client_id}/services"),
            headers=team.owner.headers,
            json={"service_id": str(service), "fee": fee, "frequency": "quarterly"},
        )
        assert response.status_code == 201, response.text
        return response.json()["data"]

    their_vat = await engage(target["id"], vat, 20_000)
    my_vat = await engage(source["id"], vat, 15_000)
    my_books = await engage(source["id"], books, 9_000)

    async def add_contact(client_id: str, name: str, **body) -> dict:
        response = await client.post(
            org_url(team.org, f"/clients/{client_id}/contacts"),
            headers=team.owner.headers,
            json={"name": name, **body},
        )
        assert response.status_code == 201, response.text
        return response.json()["data"]

    await add_contact(target["id"], "Tia Target")  # the target's primary contact
    await add_contact(source["id"], "Sam Source")  # primary there too
    await add_contact(source["id"], "Sue Second", is_billing=True)

    theirs = await new_rate(
        client,
        team.owner,
        team.org,
        kind="charge",
        client_id=target["id"],
        rate=7_000,
        effective_from="2026-01-01",
    )
    overlapping = await new_rate(
        client,
        team.owner,
        team.org,
        kind="charge",
        client_id=source["id"],
        rate=6_000,
        effective_from="2026-03-01",
        effective_to="2026-12-31",
    )
    scoped = await new_rate(
        client,
        team.owner,
        team.org,
        kind="charge",
        client_id=source["id"],
        service_id=str(vat),
        rate=8_200,
        effective_from="2026-01-01",
    )
    earlier = await new_rate(
        client,
        team.owner,
        team.org,
        kind="charge",
        client_id=source["id"],
        rate=5_000,
        effective_from="2024-01-01",
        effective_to="2025-12-31",
    )

    response = await merge(client, team, BAKERY, PLUMBING)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["outcome"] == "applied"

    # Engagements: the service the survivor takes already stays as it was agreed.
    engagements = await sql(
        "SELECT id, client_id, fee FROM client_services WHERE organization_id = :o ORDER BY fee",
        o=team.id,
    )
    owners = {str(e["id"]): (str(e["client_id"]), e["fee"]) for e in engagements}
    assert owners[their_vat["id"]] == (target["id"], 20_000)
    assert owners[my_books["id"]] == (target["id"], 9_000)  # moved
    assert owners[my_vat["id"]] == (source["id"], 15_000)  # left on the archived source

    # Contacts: all move; a client has one primary contact, the survivor's.
    assert await names_of("client_contacts", target["id"]) == [
        "Sam Source",
        "Sue Second",
        "Tia Target",
    ]
    primaries = await sql(
        "SELECT name FROM client_contacts WHERE client_id = :c AND is_primary", c=target["id"]
    )
    assert [p["name"] for p in primaries] == ["Tia Target"]
    billing = await sql(
        "SELECT name FROM client_contacts WHERE client_id = :c AND is_billing", c=target["id"]
    )
    assert [b["name"] for b in billing] == ["Sue Second"]

    # Rates: the survivor's own rate wins where periods overlap; the rest moves.
    rules = await sql("SELECT id, client_id FROM rate_rules WHERE organization_id = :o", o=team.id)
    by_rule = {str(r["id"]): str(r["client_id"]) for r in rules}
    assert by_rule[theirs["id"]] == target["id"]
    assert by_rule[scoped["id"]] == target["id"] and by_rule[earlier["id"]] == target["id"]
    assert by_rule[overlapping["id"]] == source["id"]

    [entry] = await audit_entries(team.id, "client.central_merged")
    meta = entry["metadata"]
    assert meta["counts"]["client_services"] == 1 and meta["counts"]["client_contacts"] == 2
    assert meta["counts"]["rate_rules"] == 2 and meta["contacts_demoted"] == 1
    [left_service] = meta["left_behind"]["client_services"]
    assert (left_service["id"], left_service["service_code"], left_service["fee"]) == (
        my_vat["id"],
        "VAT",
        15_000,
    )
    assert left_service["service_name"] == "VAT return" and left_service["frequency"] == "quarterly"
    [left_rule] = meta["left_behind"]["rate_rules"]
    assert (left_rule["id"], left_rule["kind"], left_rule["rate"]) == (
        overlapping["id"],
        "charge",
        6_000,
    )
    assert (left_rule["effective_from"], left_rule["effective_to"]) == ("2026-03-01", "2026-12-31")

    # The rate history is append-only: each moved rule says where it came from.
    history = await sql(
        "SELECT rule_id, action, client_id, source, old, new FROM rate_changes "
        "WHERE organization_id = :o AND source = 'client_merge' ORDER BY rule_id",
        o=team.id,
    )
    assert sorted(str(h["rule_id"]) for h in history) == sorted([scoped["id"], earlier["id"]])
    for row in history:
        assert row["action"] == "updated" and str(row["client_id"]) == target["id"]
        assert row["old"]["client_id"] == source["id"] and row["new"]["client_id"] == target["id"]
    assert meta["rate_history_rows"] == 2
    # The earlier history stays with the client it was written for.
    created = await sql(
        "SELECT count(*) AS n FROM rate_changes WHERE organization_id = :o AND client_id = :c AND source <> 'client_merge'",
        o=team.id,
        c=source["id"],
    )
    assert created[0]["n"] == 3


async def test_the_sources_primary_contact_stays_primary_when_the_survivor_has_none(client):
    team = await new_team(client)
    source, target = await two_clients(client, team)
    await give_work(client, team, source["id"], contacts=(("Sam Source", False),))
    await merge(client, team, BAKERY, PLUMBING)
    primaries = await sql(
        "SELECT name FROM client_contacts WHERE client_id = :c AND is_primary", c=target["id"]
    )
    assert [p["name"] for p in primaries] == ["Sam Source"]
    [entry] = await audit_entries(team.id, "client.central_merged")
    assert entry["metadata"]["contacts_demoted"] == 0


# --- Chains of merges ---------------------------------------------------------------------------------


async def test_a_chain_of_merges_ends_with_every_client_pointing_at_the_survivor(client):
    team = await new_team(client)
    first, second = await two_clients(client, team, BAKERY, PLUMBING)
    third = (await enabled(client, team.owner, team.org, JOINERY))["client"]
    await give_work(
        client, team, first["id"], services=("VAT",), contacts=(("One", False),), rates=False
    )
    await give_work(
        client,
        team,
        second["id"],
        services=("BOOKS",),
        contacts=(("Two", False),),
        rates=False,
        timer_user=await team.member("manager"),
    )
    await give_work(
        client,
        team,
        third["id"],
        services=("ACCS",),
        contacts=(("Three", False),),
        rates=False,
        timer_user=await team.member("staff"),
    )
    totals = {
        table: sum([(await client_counts(c["id"]))[table] for c in (first, second, third)])
        for table in MOVED_TABLES
    }
    assert (await merge(client, team, BAKERY, PLUMBING)).json()["data"]["outcome"] == "applied"
    assert (await merge(client, team, PLUMBING, JOINERY)).json()["data"]["outcome"] == "applied"
    # The first client was merged into the second, which was merged into the third: it now
    # points straight at the last survivor.
    for merged in (first, second):
        row = await client_row(merged["id"])
        assert (row["status"], str(row["merged_into_client_id"])) == ("archived", third["id"])
    assert await client_counts(third["id"]) == totals
    assert (
        await client_counts(first["id"])
        == await client_counts(second["id"])
        == dict.fromkeys(MOVED_TABLES, 0)
    )
    second_entry = (await audit_entries(team.id, "client.central_merged"))[1]
    assert second_entry["metadata"]["merged_clients_repointed"] == 1


async def test_merging_into_a_client_that_was_merged_away_goes_to_its_survivor(client):
    team = await new_team(client)
    first, second = await two_clients(client, team, BAKERY, PLUMBING)
    third = (await enabled(client, team.owner, team.org, JOINERY))["client"]
    await give_work(
        client, team, third["id"], services=("ACCS",), contacts=(("Three", False),), rates=False
    )
    await merge(client, team, BAKERY, PLUMBING)  # first -> second
    before = await client_counts(second["id"])
    moved = await client_counts(third["id"])
    # The central service now says the third client was merged into the first one, which is
    # the second one here: the work goes to the second.
    done = await merge(client, team, JOINERY, BAKERY)
    assert (
        done.json()["data"]["outcome"] == "applied"
        and done.json()["data"]["client_id"] == second["id"]
    )
    assert await client_counts(second["id"]) == {t: before[t] + moved[t] for t in MOVED_TABLES}
    assert str((await client_row(third["id"]))["merged_into_client_id"]) == second["id"]
    assert str((await client_row(first["id"]))["merged_into_client_id"]) == second["id"]


# --- When only one of the two is enabled here ------------------------------------------------------


async def test_when_only_the_merged_client_is_enabled_it_follows_the_surviving_central_client(
    client,
):
    team = await new_team(client)
    source = (await enabled(client, team.owner, team.org, BAKERY))["client"]
    await give_work(client, team, source["id"], rates=False)
    before = await client_counts(source["id"])
    response = await merge(
        client,
        team,
        BAKERY,
        PLUMBING,
        name="Brightwater Plumbing & Heating Ltd",
        address_line1="Unit 4, Riverside Park",
        address_line2="Wharf Road",
        city="Nottingham",
        postcode="NG2 3HT",
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert (data["outcome"], data["client_id"]) == ("applied", source["id"])
    assert data["message"] == f"{source['code']} now follows the surviving central client."
    row = await client_row(source["id"])
    assert (row["central_client_id"], row["name"], row["city"]) == (
        PLUMBING,
        "Brightwater Plumbing & Heating Ltd",
        "Nottingham",
    )
    assert (row["status"], row["merged_into_client_id"], row["central_sync_state"]) == (
        "active",
        None,
        "synced",
    )
    assert await client_counts(source["id"]) == before  # nothing moved
    [entry] = await audit_entries(team.id, "client.central_merged")
    meta = entry["metadata"]
    assert (meta["mode"], meta["from_central_id"], meta["to_central_id"]) == (
        "relinked",
        BAKERY,
        PLUMBING,
    )
    assert meta["changes"]["name"] == ["Northgate Bakery Ltd", "Brightwater Plumbing & Heating Ltd"]
    assert meta["client_id"] == source["id"] and entry["actor_user_id"] is None
    # The surviving central client can now be enabled only through this client.
    other = await create_client(client, team.owner, team.org, "Other")
    again = await link(client, team.owner, team.org, other["id"], PLUMBING)
    assert again.status_code == 409 and error(again)["code"] == "CENTRAL_CLIENT_ALREADY_LINKED"
    assert error(again)["details"]["client_id"] == source["id"]


async def test_a_relink_without_values_takes_the_cached_ones_and_waits_for_a_sync(client):
    team = await new_team(client)
    source = (await enabled(client, team.owner, team.org, BAKERY))["client"]
    await search(client, team.owner, team.org, q="brightwater")  # caches the survivor
    response = await merge(client, team, BAKERY, PLUMBING)
    assert response.json()["data"]["outcome"] == "applied"
    row = await client_row(source["id"])
    assert (row["central_client_id"], row["name"], row["postcode"]) == (
        PLUMBING,
        "Brightwater Plumbing & Heating Ltd",
        "NG2 3HT",
    )
    assert row["central_sync_state"] == "stale"  # the values are the cache's, not confirmed
    synced = (await sync_client(client, team.owner, team.org, source["id"])).json()["data"]
    assert synced["state"] == "synced"


async def test_a_relink_without_values_or_a_cache_keeps_the_name_and_marks_the_client_stale(client):
    team = await new_team(client)
    source = (await enabled(client, team.owner, team.org, BAKERY))["client"]
    response = await merge(client, team, BAKERY, JOINERY)
    assert response.json()["data"]["outcome"] == "applied"
    row = await client_row(source["id"])
    assert (row["central_client_id"], row["name"], row["central_sync_state"]) == (
        JOINERY,
        "Northgate Bakery Ltd",
        "stale",
    )
    # The next synchronisation brings the survivor's name and address.
    synced = (await sync_client(client, team.owner, team.org, source["id"])).json()["data"]
    assert synced["name"] == "Calder Valley Joinery Ltd" and synced["state"] == "synced"


async def test_when_only_the_survivor_is_enabled_the_event_is_skipped(client):
    team = await new_team(client)
    target = (await enabled(client, team.owner, team.org, PLUMBING))["client"]
    await give_work(
        client, team, target["id"], services=("VAT",), contacts=(("Tia", False),), rates=False
    )
    before = await client_counts(target["id"])
    response = await merge(client, team, BAKERY, PLUMBING)
    data = response.json()["data"]
    assert (data["outcome"], data["client_id"]) == ("skipped", target["id"])
    assert data["message"] == "The merged central client is not enabled in this practice."
    assert await client_counts(target["id"]) == before
    assert (await client_row(target["id"]))["status"] == "active"
    assert await audit_entries(team.id, "client.central_merged") == []


async def test_when_neither_is_enabled_the_event_is_skipped(client):
    team = await new_team(client)
    response = await merge(client, team, BAKERY, PLUMBING)
    assert response.json()["data"]["outcome"] == "skipped"
    assert (
        response.json()["data"]["message"] == "Neither central client is enabled in this practice."
    )
    [log] = [r for r in await sync_logs(client, team.owner, team.org) if r["operation"] == "merged"]
    assert log["status"] == "skipped" and log["entity_id"] is None


async def test_a_deleted_client_counts_as_not_enabled(client):
    team = await new_team(client)
    source, target = await two_clients(client, team)
    gone = await client.delete(
        org_url(team.org, f"/clients/{target['id']}"), headers=team.owner.headers
    )
    assert gone.status_code in (200, 204), gone.text
    response = await merge(client, team, BAKERY, PLUMBING)
    assert response.json()["data"]["outcome"] == "applied"
    assert (await client_row(source["id"]))["central_client_id"] == PLUMBING  # relinked, not merged
    assert (await client_row(source["id"]))["status"] == "active"


# --- After a merge ------------------------------------------------------------------------------------


async def test_a_merge_event_is_applied_once(client):
    team = await new_team(client)
    source, target = await two_clients(client, team)
    await give_work(client, team, source["id"], services=("VAT",), rates=False)
    first = await merge(client, team, BAKERY, PLUMBING, event_id="same-merge")
    assert first.json()["data"]["outcome"] == "applied"
    after = await client_counts(target["id"])
    again = await merge(client, team, BAKERY, PLUMBING, event_id="same-merge")
    assert (again.json()["data"]["outcome"], again.json()["data"]["duplicate"]) == ("skipped", True)
    # Another delivery of the same merge under a new id finds the source merged already.
    other = await merge(client, team, BAKERY, PLUMBING)
    assert other.json()["data"]["outcome"] == "skipped"
    assert other.json()["data"]["message"] == "This client was already merged into another client."
    assert await client_counts(target["id"]) == after
    assert len(await audit_entries(team.id, "client.central_merged")) == 1


async def test_a_merged_client_ignores_later_events_and_refuses_synchronisation_and_links(client):
    team = await new_team(client)
    source, target = await two_clients(client, team)
    await merge(client, team, BAKERY, PLUMBING)
    for type_, extra in (
        ("renamed", {"name": "Late Name Ltd"}),
        ("archived", {}),
        ("restored", {}),
    ):
        response = await send_event(client, team.owner, team.org, type_, BAKERY, **extra)
        data = response.json()["data"]
        assert (data["outcome"], data["message"]) == (
            "skipped",
            "This client was merged into another client.",
        ), type_
    assert (await client_row(source["id"]))["name"] == "Northgate Bakery Ltd"
    refused = await sync_client(client, team.owner, team.org, source["id"])
    assert refused.status_code == 409 and error(refused)["code"] == "CLIENT_MERGED"
    # It still carries its central id, so it must be unlinked before it could be linked again,
    # and a merged client cannot be linked to anything.
    relink = await link(client, team.owner, team.org, source["id"], KESTREL)
    assert relink.status_code == 409 and error(relink)["code"] == "CLIENT_ALREADY_LINKED"
    assert (await unlink(client, team.owner, team.org, source["id"])).status_code == 200
    merged_link = await link(client, team.owner, team.org, source["id"], KESTREL)
    assert merged_link.status_code == 409 and error(merged_link)["code"] == "CLIENT_MERGED"
    # Synchronising everything leaves it alone.
    everything = (await sync_all(client, team.owner, team.org)).json()["data"]
    assert [r["client_id"] for r in everything["results"]] == [target["id"]]


async def test_the_merged_clients_activity_shows_on_the_survivors_overview(client):
    team = await new_team(client)
    _, target = await two_clients(client, team)
    await merge(client, team, BAKERY, PLUMBING)
    overview = await client.get(
        org_url(team.org, f"/clients/{target['id']}/overview"), headers=team.owner.headers
    )
    assert overview.status_code == 200, overview.text
    actions = [event["action"] for event in overview.json()["data"]["recent_activity"]]
    assert "client.central_merged" in actions and "client.central_enabled" in actions


# --- Atomicity and concurrency ---------------------------------------------------------------------


async def test_a_failing_merge_changes_nothing_and_can_be_delivered_again(client, monkeypatch):
    team = await new_team(client)
    source, target = await two_clients(client, team)
    await give_work(client, team, source["id"], services=("VAT",), rates=False)
    before_source = await client_counts(source["id"])
    before_target = await client_counts(target["id"])

    async def boom(self, *args, **kwargs):
        raise RuntimeError("the last step fails")

    with monkeypatch.context() as patch:
        patch.setattr(merge_repository.ClientMergeRepository, "repoint_merged_sources", boom)
        failed = await merge(client, team, BAKERY, PLUMBING, event_id="merge-f")
    assert failed.status_code == 500
    # Everything the earlier steps did was rolled back with it.
    assert await client_counts(source["id"]) == before_source
    assert await client_counts(target["id"]) == before_target
    row = await client_row(source["id"])
    assert (row["status"], row["merged_into_client_id"]) == ("active", None)
    assert await audit_entries(team.id, "client.central_merged") == []
    [log] = await sync_logs(client, team.owner, team.org, status="failed")
    assert (log["operation"], log["external_id"]) == ("merged", BAKERY)
    retry = await merge(client, team, BAKERY, PLUMBING, event_id="merge-f")
    assert retry.json()["data"]["outcome"] == "applied"
    assert await client_counts(source["id"]) == dict.fromkeys(MOVED_TABLES, 0)


async def test_merges_in_opposite_directions_at_once_apply_exactly_one(client):
    team = await new_team(client)
    first, second = await two_clients(client, team)
    await give_work(client, team, first["id"], services=("VAT",), rates=False)
    await give_work(
        client,
        team,
        second["id"],
        services=("BOOKS",),
        contacts=(("Two", False),),
        rates=False,
        timer_user=await team.member("manager"),
    )
    total = sum((await client_counts(first["id"])).values()) + sum(
        (await client_counts(second["id"])).values()
    )
    forward, backward = await asyncio.gather(
        merge(client, team, BAKERY, PLUMBING, event_id="fwd"),
        merge(client, team, PLUMBING, BAKERY, event_id="bwd"),
    )
    assert forward.status_code == backward.status_code == 200, (forward.text, backward.text)
    outcomes = sorted([forward.json()["data"]["outcome"], backward.json()["data"]["outcome"]])
    assert outcomes == ["applied", "skipped"]
    rows = [await client_row(first["id"]), await client_row(second["id"])]
    assert sorted(r["status"] for r in rows) == ["active", "archived"]
    survivor = next(r for r in rows if r["status"] == "active")
    assert sum((await client_counts(survivor["id"])).values()) == total
    assert len(await audit_entries(team.id, "client.central_merged")) == 1


async def test_a_merge_does_not_touch_another_practice(client):
    team = await new_team(client)
    owner, org = await create_user_with_org(client, "Second Practice")
    _, mine = await two_clients(client, team)
    theirs_source = (await enabled(client, owner, org, BAKERY))["client"]
    theirs_target = (await enabled(client, owner, org, PLUMBING))["client"]
    await give_work(client, Team(owner, org), theirs_source["id"], services=("VAT",), rates=False)
    before = await client_counts(theirs_source["id"])
    response = await merge(client, team, BAKERY, PLUMBING)
    assert response.json()["data"]["client_id"] == mine["id"]
    assert await client_counts(theirs_source["id"]) == before
    assert (await client_row(theirs_source["id"]))["status"] == "active"
    assert (await client_row(theirs_target["id"]))["status"] == "active"


async def test_a_merge_from_the_change_feed_goes_through_the_same_handler(client, central_gateway):
    team = await new_team(client)
    source, target = await two_clients(client, team)
    await give_work(client, team, source["id"], services=("VAT",), rates=False)
    moved = sum((await client_counts(source["id"])).values())
    central_gateway.merge(BAKERY, PLUMBING)
    data = (await process_changes(client, team.owner, team.org)).json()["data"]
    assert (data["fetched"], data["applied"], data["failed"]) == (1, 1, 0)
    [event] = data["events"]
    assert event["message"] == f"Merged {source['code']} into {target['code']}: {moved} rows moved."
    [entry] = await audit_entries(team.id, "client.central_merged")
    assert entry["metadata"]["merged_client"]["id"] == source["id"]
    assert (entry["metadata"]["event_id"], entry["metadata"]["source"]) == (
        event["event_id"],
        "feed",
    )
    assert (await client_row(source["id"]))["status"] == "archived"
    # The central service archived the merged client. It cannot be enabled again: asking for it
    # answers with the archived client that was linked to it.
    again = await enable(client, team.owner, team.org, BAKERY)
    assert again.status_code == 200 and again.json()["data"]["result"] == "already_enabled"
    assert again.json()["data"]["client"]["id"] == source["id"]
    assert await scalar("SELECT count(*) FROM clients WHERE organization_id = :o", o=team.id) == 2
