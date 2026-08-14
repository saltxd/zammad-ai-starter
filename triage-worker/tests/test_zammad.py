"""The Zammad client, against a mock transport.

Pins the two things that are easy to get wrong and expensive to discover in
production: the request bodies actually sent, and what happens when Zammad
rejects a name it doesn't recognise.
"""

from __future__ import annotations

import json

import httpx
import pytest

from triage_worker.zammad import ZammadClient, ZammadError


def client_recording(responses: dict[str, httpx.Response] | None = None):
    """A ZammadClient whose requests are captured instead of sent."""
    seen: list[httpx.Request] = []
    responses = responses or {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses.get(request.url.path, httpx.Response(200, json={}))

    client = ZammadClient(
        base_url="http://zammad.invalid",
        auth_header="Token token=abc123",
        transport=httpx.MockTransport(handler),
    )
    return client, seen


async def test_list_tags_reads_current_ticket_tags():
    """Retries resend the original payload; live tags are what make them idempotent."""
    client, seen = client_recording(
        {"/api/v1/tags": httpx.Response(200, json={"tags": ["network", "triaged"]})}
    )

    tags = await client.list_tags(42)

    assert tags == ["network", "triaged"]
    assert len(seen) == 1
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/api/v1/tags"
    assert seen[0].url.params["object"] == "Ticket"
    assert seen[0].url.params["o_id"] == "42"
    await client.aclose()


async def test_update_sends_group_and_priority_by_name():
    """By name, not by id — that's what keeps the token on ticket.agent."""
    client, seen = client_recording()

    payload = await client.update_ticket(42, group="2nd Level", priority="3 high")

    assert len(seen) == 1
    request = seen[0]
    assert request.method == "PUT"
    assert request.url.path == "/api/v1/tickets/42"
    assert json.loads(request.content) == {"group": "2nd Level", "priority": "3 high"}
    assert payload == {"group": "2nd Level", "priority": "3 high"}
    await client.aclose()


async def test_update_with_nothing_to_change_makes_no_request():
    client, seen = client_recording()
    assert await client.update_ticket(42) == {}
    assert seen == []
    await client.aclose()


async def test_unknown_group_raises_with_zammads_message():
    """Zammad answers 422 and leaves the ticket alone; surface that, don't swallow it."""
    body = {"error": "No lookup value found for 'group': \"Tier 7\""}
    client, _ = client_recording({"/api/v1/tickets/42": httpx.Response(422, json=body)})

    with pytest.raises(ZammadError) as excinfo:
        await client.update_ticket(42, group="Tier 7")

    assert "422" in str(excinfo.value)
    assert "No lookup value found" in str(excinfo.value)
    await client.aclose()


async def test_tags_are_added_one_call_each():
    client, seen = client_recording()

    await client.add_tags(42, ("network", "access"))

    assert [r.url.params.get("item") for r in seen] == ["network", "access"]
    assert all(r.url.params.get("object") == "Ticket" for r in seen)
    assert all(r.url.params.get("o_id") == "42" for r in seen)
    await client.aclose()


async def test_internal_note_is_marked_internal():
    """A triage rationale visible to the customer would be a support incident."""
    client, seen = client_recording()

    await client.add_internal_note(42, "Routed to Billing.")

    body = json.loads(seen[0].content)
    assert body["internal"] is True
    assert body["ticket_id"] == 42
    assert "Routed to Billing." in body["body"]
    await client.aclose()


async def test_auth_header_is_sent():
    client, seen = client_recording()
    await client.ping()
    assert seen[0].headers["Authorization"] == "Token token=abc123"
    await client.aclose()


async def test_ping_is_false_rather_than_raising_when_unauthorized():
    client, _ = client_recording({"/api/v1/users/me": httpx.Response(401, json={})})
    assert await client.ping() is False
    await client.aclose()


def test_missing_credentials_fail_fast_at_construction():
    with pytest.raises(ValueError, match="No Zammad credentials"):
        ZammadClient(base_url="http://zammad.invalid", auth_header="")
