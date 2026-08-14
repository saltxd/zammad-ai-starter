"""End-to-end through the HTTP layer, with the model and Zammad stubbed.

This is the test that would have caught every integration bug I hit writing
this: the signature is computed over raw bytes, a webhook we can't parse must
still return 2xx, and a low-confidence answer must not touch the ticket.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from triage_worker.config import Settings
from triage_worker.main import Triager, create_app, verify_signature
from triage_worker.zammad import ZammadError

GROUPS = ["Users", "2nd Level"]
PRIORITIES = ["1 low", "2 normal", "3 high"]
TAGS = ["access", "needs-review", "network"]

WEBHOOK = {
    "ticket": {"id": 42, "number": "10042", "title": "VPN keeps dropping"},
    "article": {"body": "Nobody in the Berlin office can stay connected."},
}


class StubChain:
    """Stands in for ProviderChain: returns canned text or raises."""

    def __init__(self, response: str | None = None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls: list[tuple[str, str]] = []
        self.names = ["stub"]

    async def complete(self, system: str, user: str, schema: dict) -> str:
        self.calls.append((system, user))
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response

    async def aclose(self) -> None:
        pass


class StubZammad:
    """Records what would have been written to Zammad."""

    def __init__(self, fail: bool = False, live_tags: tuple[str, ...] = ()):
        self.fail = fail
        self.live_tags = live_tags
        self.updates: list[dict] = []
        self.tags: list[tuple[int, tuple[str, ...]]] = []
        self.notes: list[tuple[int, str]] = []
        self.tag_lookups: list[int] = []

    async def update_ticket(self, ticket_id, *, group=None, priority=None):
        if self.fail:
            raise ZammadError("boom")
        payload = {"group": group, "priority": priority}
        self.updates.append({"ticket_id": ticket_id, **payload})
        return payload

    async def add_tags(self, ticket_id, tags):
        self.tags.append((ticket_id, tuple(tags)))

    async def add_internal_note(self, ticket_id, body):
        self.notes.append((ticket_id, body))

    async def list_tags(self, ticket_id):
        self.tag_lookups.append(ticket_id)
        return list(self.live_tags)

    async def ping(self):
        return not self.fail

    async def aclose(self):
        pass


def settings(**overrides) -> Settings:
    base = {
        "zammad_url": "http://zammad.invalid",
        "zammad_token": "stub",
        "groups": GROUPS,
        "priorities": PRIORITIES,
        "tags": TAGS,
        "default_group": "Users",
        "default_priority": "2 normal",
        "confidence_threshold": 0.6,
        "provider_order": [],
    }
    base.update(overrides)
    return Settings(**base)


def model_says(**overrides) -> str:
    base = {
        "group": "2nd Level",
        "priority": "3 high",
        "tags": ["network"],
        "reason": "Whole office affected.",
        "confidence": 0.93,
    }
    base.update(overrides)
    return json.dumps(base)


@pytest.fixture
def make_client():
    """A TestClient whose Triager has been swapped for stubs."""
    clients = []

    def _make(chain, zammad, config=None):
        config = config or settings()
        app = create_app(config)
        client = TestClient(app)
        client.__enter__()  # run lifespan so app.state exists
        app.state.triager = Triager(config, chain, zammad)
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.__exit__(None, None, None)


# --- the path that matters ------------------------------------------------


def test_confident_classification_is_written_back(make_client):
    zammad = StubZammad()
    client = make_client(StubChain(model_says()), zammad)

    response = client.post("/webhook/zammad", json=WEBHOOK)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "applied"
    assert body["ticket_id"] == 42
    assert body["group"] == "2nd Level"
    assert body["priority"] == "3 high"
    assert body["tags"] == ["network"]

    assert zammad.updates == [{"ticket_id": 42, "group": "2nd Level", "priority": "3 high"}]
    assert zammad.tags == [(42, ("network", "triaged"))]
    assert len(zammad.notes) == 1
    assert "Whole office affected." in zammad.notes[0][1]


def test_ticket_text_reaches_the_model(make_client):
    chain = StubChain(model_says())
    make_client(chain, StubZammad()).post("/webhook/zammad", json=WEBHOOK)

    system, user = chain.calls[0]
    assert "VPN keeps dropping" in user
    assert "Berlin office" in user
    assert "- 2nd Level" in system  # taxonomy made it into the prompt


# --- the paths that protect the queue -------------------------------------


def test_low_confidence_leaves_the_ticket_untouched(make_client):
    zammad = StubZammad()
    client = make_client(StubChain(model_says(confidence=0.42)), zammad)

    body = client.post("/webhook/zammad", json=WEBHOOK).json()

    assert body["status"] == "skipped"
    assert body["skipped_reason"] == "below confidence threshold"
    assert zammad.updates == []
    assert zammad.tags == []
    assert zammad.notes == []


def test_provider_outage_falls_back_to_rules_and_writes_nothing(make_client):
    zammad = StubZammad()
    client = make_client(StubChain(error=RuntimeError("api down")), zammad)

    body = client.post("/webhook/zammad", json=WEBHOOK).json()

    assert body["source"] == "fallback"
    assert body["status"] == "skipped"
    assert zammad.updates == []


def test_dry_run_classifies_but_does_not_write(make_client):
    zammad = StubZammad()
    client = make_client(StubChain(model_says()), zammad, settings(dry_run=True))

    body = client.post("/webhook/zammad", json=WEBHOOK).json()

    assert body["status"] == "skipped"
    assert body["skipped_reason"] == "dry run"
    assert body["group"] == "2nd Level"  # it still tells you what it would do
    assert zammad.updates == []


def test_zammad_failure_is_reported_without_a_5xx(make_client):
    """Zammad retries 5xx. A failed write must not become a retry storm."""
    client = make_client(StubChain(model_says()), StubZammad(fail=True))

    response = client.post("/webhook/zammad", json=WEBHOOK)

    assert response.status_code == 200
    assert response.json()["status"] == "error"


def test_unparseable_payload_is_acknowledged_not_retried(make_client):
    client = make_client(StubChain(model_says()), StubZammad())
    response = client.post("/webhook/zammad", json={"hello": "world"})
    assert response.status_code == 202
    assert response.json()["status"] == "ignored"


def test_invalid_json_is_rejected(make_client):
    client = make_client(StubChain(model_says()), StubZammad())
    response = client.post(
        "/webhook/zammad",
        content=b"not json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400


def test_tags_can_be_disabled(make_client):
    zammad = StubZammad()
    client = make_client(
        StubChain(model_says()), zammad, settings(apply_tags=False, add_note=False)
    )
    client.post("/webhook/zammad", json=WEBHOOK)
    assert zammad.updates  # group/priority still applied
    assert zammad.notes == []
    # The done-tag is operational, not a classification tag — it still stamps.
    assert any("triaged" in tags for _, tags in zammad.tags)


def test_internal_article_is_acknowledged_without_classifying(make_client):
    """Our own note (or any agent note) must not re-enter the model."""
    chain = StubChain(model_says())
    zammad = StubZammad()
    client = make_client(chain, zammad)

    payload = {
        "ticket": {"id": 42, "number": "10042", "title": "VPN keeps dropping"},
        "article": {
            "body": "Routed to 2nd Level at 3 high (confidence 0.93).",
            "internal": True,
            "sender": "Agent",
            "type": "note",
            "subject": "AI triage",
        },
    }
    body = client.post("/webhook/zammad", json=payload).json()

    assert body["status"] == "skipped"
    assert body["skipped_reason"] == "internal article"
    assert chain.calls == []
    assert zammad.updates == []
    assert zammad.notes == []


def test_payload_already_tagged_skips_model_and_write(make_client):
    chain = StubChain(model_says())
    zammad = StubZammad()
    client = make_client(chain, zammad)

    payload = {
        **WEBHOOK,
        "ticket": {**WEBHOOK["ticket"], "tags": ["triaged"]},
    }
    body = client.post("/webhook/zammad", json=payload).json()

    assert body["status"] == "skipped"
    assert body["skipped_reason"] == "already triaged"
    assert chain.calls == []
    assert zammad.updates == []
    assert zammad.tag_lookups == []  # payload was enough; no extra GET


def test_live_tags_catch_a_retried_webhook(make_client):
    """Zammad retries the original payload, which does not yet contain the tag."""
    chain = StubChain(model_says())
    zammad = StubZammad(live_tags=("triaged",))
    client = make_client(chain, zammad)

    body = client.post("/webhook/zammad", json=WEBHOOK).json()

    assert body["status"] == "skipped"
    assert body["skipped_reason"] == "already triaged"
    assert zammad.tag_lookups == [42]
    assert chain.calls == []
    assert zammad.updates == []


def test_successful_apply_stamps_the_done_tag(make_client):
    zammad = StubZammad()
    client = make_client(StubChain(model_says()), zammad)

    client.post("/webhook/zammad", json=WEBHOOK)

    stamped = [tags for _, tags in zammad.tags if "triaged" in tags]
    assert stamped, "applied tickets must be stamped so a retry is a no-op"


# --- signature verification -----------------------------------------------


def sign(body: bytes, secret: str) -> str:
    return "sha1=" + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()


def test_valid_signature_is_accepted(make_client):
    secret = "s3cret"
    zammad = StubZammad()
    client = make_client(StubChain(model_says()), zammad, settings(webhook_secret=secret))
    body = json.dumps(WEBHOOK).encode()

    response = client.post(
        "/webhook/zammad",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature": sign(body, secret)},
    )

    assert response.status_code == 200
    assert zammad.updates


def test_bad_signature_is_rejected(make_client):
    zammad = StubZammad()
    client = make_client(StubChain(model_says()), zammad, settings(webhook_secret="s3cret"))
    body = json.dumps(WEBHOOK).encode()

    response = client.post(
        "/webhook/zammad",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature": sign(body, "wrong")},
    )

    assert response.status_code == 401
    assert zammad.updates == []


def test_missing_signature_is_rejected_when_a_secret_is_set(make_client):
    client = make_client(StubChain(model_says()), StubZammad(), settings(webhook_secret="s3cret"))
    assert client.post("/webhook/zammad", json=WEBHOOK).status_code == 401


@pytest.mark.parametrize(
    ("header", "secret", "expected"),
    [
        (None, "", True),  # no secret configured: verification is off
        ("anything", "", True),
        (None, "s", False),
        ("sha1=deadbeef", "s", False),
    ],
)
def test_verify_signature_matrix(header, secret, expected):
    assert verify_signature(b"body", header, secret) is expected


def test_signature_is_computed_over_raw_bytes_not_reserialized_json():
    """Whitespace differences must not break verification."""
    secret = "s"
    body = b'{"a":  1}'
    assert verify_signature(body, sign(body, secret), secret)
    assert not verify_signature(b'{"a": 1}', sign(body, secret), secret)


# --- health ---------------------------------------------------------------


def test_healthz_reports_wiring(make_client):
    client = make_client(StubChain(model_says()), StubZammad())
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["providers"] == ["stub"]
    assert body["zammad"] is True


def test_readyz_is_503_when_zammad_is_unreachable(make_client):
    client = make_client(StubChain(model_says()), StubZammad(fail=True))
    assert client.get("/readyz").status_code == 503
