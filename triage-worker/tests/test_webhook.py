"""Payload parsing: the shapes Zammad actually sends, and the junk it sometimes does."""

from __future__ import annotations

import pytest

from triage_worker.webhook import parse_webhook, strip_html

NESTED = {
    "ticket": {
        "id": 42,
        "number": "10042",
        "title": "VPN drops every few minutes",
        "group": "Users",
        "priority": "2 normal",
        "state": "new",
        "customer": {"email": "dana@example.org", "firstname": "Dana"},
    },
    "article": {
        "id": 99,
        "ticket_id": 42,
        "subject": "VPN drops every few minutes",
        "body": "Since this morning the VPN disconnects roughly every 5 minutes.",
        "content_type": "text/plain",
        "from": "dana@example.org",
    },
}


def test_parses_nested_ticket_and_article():
    event = parse_webhook(NESTED)
    assert event is not None
    assert event.ticket_id == 42
    assert event.number == "10042"
    assert event.title == "VPN drops every few minutes"
    assert "disconnects roughly every 5 minutes" in event.body
    assert event.customer_email == "dana@example.org"
    assert event.group == "Users"
    assert event.priority == "2 normal"


def test_parses_flat_payload_without_ticket_wrapper():
    event = parse_webhook(
        {"id": 7, "number": "10007", "title": "Printer offline", "article": {"body": "hi"}}
    )
    assert event is not None
    assert event.ticket_id == 7
    assert event.body == "hi"


def test_uses_last_article_when_given_a_list():
    payload = {
        "ticket": {"id": 3, "title": "t"},
        "article": [
            {"body": "first message"},
            {"body": "second message"},
        ],
    }
    event = parse_webhook(payload)
    assert event is not None
    assert event.body == "second message"


def test_html_article_is_flattened():
    payload = {
        "ticket": {"id": 5, "title": "t"},
        "article": {
            "content_type": "text/html",
            "body": "<div>Line one</div><p>Line &amp; two</p><br>Line three",
        },
    }
    event = parse_webhook(payload)
    assert event is not None
    assert event.body == "Line one\nLine & two\n\nLine three"


def test_customer_falls_back_to_article_from():
    payload = {"ticket": {"id": 8, "title": "t"}, "article": {"from": "sam@example.org"}}
    event = parse_webhook(payload)
    assert event is not None
    assert event.customer_email == "sam@example.org"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "not a dict",
        {},
        {"ticket": {"title": "no id"}},
        {"ticket": {"id": "not-a-number"}},
        {"unrelated": "payload"},
    ],
)
def test_unusable_payloads_return_none_instead_of_raising(payload):
    assert parse_webhook(payload) is None


def test_missing_body_yields_placeholder_in_prompt():
    event = parse_webhook({"ticket": {"id": 1, "title": "Just a subject"}})
    assert event is not None
    assert event.body == ""
    assert "(no message body)" in event.as_prompt_text()


def test_prompt_text_includes_subject_and_sender():
    event = parse_webhook(NESTED)
    assert event is not None
    text = event.as_prompt_text()
    assert text.startswith("Subject: VPN drops every few minutes")
    assert "From: dana@example.org" in text


def test_strip_html_collapses_whitespace_and_blank_lines():
    assert strip_html("<p>a  b</p>\n\n\n<p>c</p>") == "a b\n\nc"


def test_strip_html_unescapes_entities():
    assert strip_html("5 &lt; 10 &amp; rising") == "5 < 10 & rising"


def test_parses_article_flags_and_ticket_tags():
    """Loop protection needs to know *which* article fired the trigger."""
    payload = {
        "ticket": {
            "id": 9,
            "title": "VPN",
            "tags": ["network", "triaged"],
        },
        "article": {
            "body": "Routed to 2nd Level.",
            "internal": True,
            "sender": "Agent",
            "type": "note",
            "subject": "AI triage",
        },
    }
    event = parse_webhook(payload)
    assert event is not None
    assert event.article_internal is True
    assert event.article_sender == "Agent"
    assert event.article_type == "note"
    assert event.article_subject == "AI triage"
    assert event.tags == ("network", "triaged")


def test_parses_tags_sent_as_a_comma_string():
    event = parse_webhook({"ticket": {"id": 1, "title": "t", "tags": "access, billing"}})
    assert event is not None
    assert event.tags == ("access", "billing")


def test_customer_article_defaults_are_safe_to_triage():
    event = parse_webhook(NESTED)
    assert event is not None
    assert event.article_internal is False
    assert event.tags == ()
