"""Rich text in tickets and comments: the editor's HTML is sanitised, its text kept beside it."""

from tests.factories import error
from tests.ticket_helpers import make_ticket, new_team, ticket_url
from tests.time_helpers import ok_data

HTML = (
    "<p>Steps:</p><ol><li><p>Open the <strong>Clients</strong> page</p></li><li>Click "
    '<a href="https://example.com/x">here</a></li></ol>'
)
TEXT = "Steps:\n\n1. Open the Clients page\n2. Click here"


def comments_url(t, ticket_id, comment_id=None) -> str:
    return ticket_url(t.org, ticket_id, "/comments" + (f"/{comment_id}" if comment_id else ""))


async def create(client, t, **body) -> dict:
    response = await client.post(
        ticket_url(t.org), headers=t.owner.headers, json={"subject": "Bug", **body}
    )
    return ok_data(response, 201)


async def test_a_ticket_keeps_the_formatted_description_and_its_text(client):
    t = await new_team(client)
    ticket = await create(client, t, description_html=HTML)
    assert ticket["description"] == TEXT
    assert "<strong>Clients</strong>" in ticket["description_html"]
    assert 'rel="noopener noreferrer nofollow"' in ticket["description_html"]
    again = ok_data(await client.get(ticket_url(t.org, ticket["id"]), headers=t.owner.headers))
    assert again["description_html"] == ticket["description_html"]


async def test_a_plain_ticket_has_no_html(client):
    t = await new_team(client)
    ticket = await create(client, t, description="Just text <b>not bold</b>")
    assert ticket["description"] == "Just text <b>not bold</b>"
    assert ticket["description_html"] is None


async def test_script_and_event_handlers_are_removed_from_the_description(client):
    t = await new_team(client)
    ticket = await create(
        client,
        t,
        description_html=(
            '<p onclick="x()">Hi</p><script>alert(1)</script><img src=x onerror=alert(1)>'
            '<a href="javascript:alert(1)">link</a>'
        ),
    )
    html = ticket["description_html"]
    assert html == "<p>Hi</p><p>link</p>" or html == "<p>Hi</p>link"
    for needle in ("script", "onclick", "onerror", "javascript", "<img"):
        assert needle not in html
    assert "alert" not in ticket["description"]


async def test_html_with_no_text_is_no_description(client):
    t = await new_team(client)
    ticket = await create(client, t, description_html="<p></p><p><br></p>")
    assert ticket["description"] is None and ticket["description_html"] is None


async def test_the_description_text_is_still_limited_to_10000_characters(client):
    t = await new_team(client)
    response = await client.post(
        ticket_url(t.org),
        headers=t.owner.headers,
        json={"subject": "Bug", "description_html": "<p>" + "x" * 10_001 + "</p>"},
    )
    assert response.status_code == 422


async def test_a_comment_keeps_the_formatted_body_and_its_text(client):
    t = await new_team(client)
    ticket = await make_ticket(client, t.owner, t.org)
    sent = ok_data(
        await client.post(
            comments_url(t, ticket["id"]),
            headers=t.owner.headers,
            json={"body_html": "<p>See <strong>this</strong></p><ul><li>a</li><li>b</li></ul>"},
        ),
        201,
    )
    assert sent["body"] == "See this\n\n- a\n- b"
    assert sent["body_html"] == "<p>See <strong>this</strong></p><ul><li>a</li><li>b</li></ul>"
    listed = ok_data(await client.get(comments_url(t, ticket["id"]), headers=t.owner.headers))
    assert listed[0]["body_html"] == sent["body_html"]


async def test_a_plain_comment_has_no_html(client):
    t = await new_team(client)
    ticket = await make_ticket(client, t.owner, t.org)
    sent = ok_data(
        await client.post(
            comments_url(t, ticket["id"]), headers=t.owner.headers, json={"body": "plain"}
        ),
        201,
    )
    assert sent["body"] == "plain" and sent["body_html"] is None


async def test_a_comment_of_only_empty_html_is_refused(client):
    t = await new_team(client)
    ticket = await make_ticket(client, t.owner, t.org)
    response = await client.post(
        comments_url(t, ticket["id"]),
        headers=t.owner.headers,
        json={"body_html": "<p></p>"},
    )
    assert response.status_code == 422
    assert error(response)["code"] == "VALIDATION_ERROR"


async def test_search_and_excerpt_use_the_text_not_the_tags(client):
    t = await new_team(client)
    await create(client, t, description_html="<p>The <strong>invoice</strong> total is wrong</p>")
    listed = ok_data(
        await client.get(ticket_url(t.org) + "?q=invoice+total", headers=t.owner.headers)
    )
    assert len(listed) == 1
    assert listed[0]["description_excerpt"] == "The invoice total is wrong"
    assert ok_data(await client.get(ticket_url(t.org) + "?q=strong", headers=t.owner.headers)) == []
