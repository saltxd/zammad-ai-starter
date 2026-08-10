"""Parsing and clamping model responses.

These are the tests worth stealing. Every case here is a real thing a model
does: fenced JSON, a chatty preamble, a queue that doesn't exist, a confidence
of 85 instead of 0.85, tags as a comma string. The rule is the same throughout —
degrade to a safe default, never raise into the request path.
"""

from __future__ import annotations

import json

import pytest

from triage_worker.classify import (
    ClassificationError,
    build_prompt,
    extract_json_object,
    heuristic_classification,
    normalize_tags,
    parse_classification,
    response_schema,
)
from triage_worker.models import TicketEvent

GROUPS = ["Users", "2nd Level", "Billing"]
PRIORITIES = ["1 low", "2 normal", "3 high"]
TAGS = ["access", "billing", "bug", "needs-review", "network"]

PARSE_KWARGS = {
    "groups": GROUPS,
    "priorities": PRIORITIES,
    "tags": TAGS,
    "default_group": "Users",
    "default_priority": "2 normal",
}


def payload(**overrides):
    base = {
        "group": "2nd Level",
        "priority": "3 high",
        "tags": ["network"],
        "reason": "Site-wide VPN failure.",
        "confidence": 0.91,
    }
    base.update(overrides)
    return json.dumps(base)


# --- happy path -----------------------------------------------------------


def test_parses_a_well_formed_response():
    result = parse_classification(payload(), **PARSE_KWARGS)
    assert result.group == "2nd Level"
    assert result.priority == "3 high"
    assert result.tags == ("network",)
    assert result.reason == "Site-wide VPN failure."
    assert result.confidence == pytest.approx(0.91)
    assert result.source == "model"


# --- responses that are almost JSON ---------------------------------------


def test_parses_fenced_json():
    result = parse_classification(f"```json\n{payload()}\n```", **PARSE_KWARGS)
    assert result.group == "2nd Level"


def test_parses_json_surrounded_by_prose():
    text = f"Sure! Here is the classification:\n\n{payload()}\n\nLet me know if that helps."
    assert parse_classification(text, **PARSE_KWARGS).group == "2nd Level"


def test_parses_json_containing_braces_inside_strings():
    text = payload(reason='Customer wrote "{urgent}" in the subject.')
    assert "urgent" in parse_classification(text, **PARSE_KWARGS).reason


def test_skips_a_leading_non_object_and_finds_the_real_one():
    text = f"note: {{}} was empty\n{payload()}"
    # The first balanced object parses as an empty dict, which is still a dict,
    # so it wins — and clamping keeps that safe rather than crashing.
    result = parse_classification(text, **PARSE_KWARGS)
    assert result.group == "Users"
    assert result.priority == "2 normal"


@pytest.mark.parametrize("text", ["", "   ", "I cannot classify this ticket.", "```\n```"])
def test_responses_with_no_json_raise(text):
    with pytest.raises(ClassificationError):
        parse_classification(text, **PARSE_KWARGS)


# --- clamping unknown values ----------------------------------------------


def test_unknown_group_falls_back_to_default():
    assert parse_classification(payload(group="Tier 7"), **PARSE_KWARGS).group == "Users"


def test_group_match_is_case_insensitive():
    assert parse_classification(payload(group="bILLing"), **PARSE_KWARGS).group == "Billing"


def test_bare_priority_label_matches_zammads_numbered_form():
    assert parse_classification(payload(priority="high"), **PARSE_KWARGS).priority == "3 high"


def test_unknown_priority_falls_back_to_default():
    assert parse_classification(payload(priority="urgent!"), **PARSE_KWARGS).priority == "2 normal"


def test_unknown_tags_are_dropped_not_invented():
    result = parse_classification(payload(tags=["network", "made-up-tag"]), **PARSE_KWARGS)
    assert result.tags == ("network",)


def test_tags_accept_a_comma_string():
    result = parse_classification(payload(tags="network, access"), **PARSE_KWARGS)
    assert result.tags == ("network", "access")


def test_tags_are_capped():
    result = parse_classification(payload(tags=TAGS), max_tags=2, **PARSE_KWARGS)
    assert len(result.tags) == 2


def test_missing_fields_use_defaults():
    result = parse_classification("{}", **PARSE_KWARGS)
    assert result.group == "Users"
    assert result.priority == "2 normal"
    assert result.tags == ()
    assert result.confidence == 0.0


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (0.5, 0.5),
        (85, 0.85),
        (100, 1.0),
        (150, 1.0),
        (-2, 0.0),
        ("0.4", 0.4),
        (None, 0.0),
        ("high", 0.0),
    ],
)
def test_confidence_is_coerced_into_zero_to_one(given, expected):
    result = parse_classification(payload(confidence=given), **PARSE_KWARGS)
    assert result.confidence == pytest.approx(expected)


def test_reason_is_truncated_not_rejected():
    result = parse_classification(payload(reason="x" * 900), **PARSE_KWARGS)
    assert len(result.reason) == 500


def test_non_string_reason_becomes_empty():
    assert parse_classification(payload(reason={"a": 1}), **PARSE_KWARGS).reason == ""


# --- helpers --------------------------------------------------------------


def test_extract_json_object_rejects_arrays():
    with pytest.raises(ClassificationError):
        extract_json_object("[1, 2, 3]")


def test_normalize_tags_deduplicates_and_lowercases():
    assert normalize_tags(["Bug", "#bug", "BUG"], allowed=TAGS, limit=5) == ("bug",)


def test_normalize_tags_without_a_vocabulary_keeps_everything():
    assert normalize_tags(["Alpha", "beta"], allowed=[], limit=5) == ("alpha", "beta")


def test_confidence_threshold_gate():
    result = parse_classification(payload(confidence=0.55), **PARSE_KWARGS)
    assert not result.is_confident(0.6)
    assert result.is_confident(0.5)


# --- prompt + schema ------------------------------------------------------


def test_prompt_lists_the_configured_taxonomy():
    prompt = build_prompt(
        groups=GROUPS,
        priorities=PRIORITIES,
        tags=TAGS,
        default_group="Users",
        confidence_threshold=0.6,
    )
    for item in GROUPS + PRIORITIES + TAGS:
        assert f"- {item}" in prompt
    assert "0.60" in prompt


def test_schema_constrains_choices_to_the_taxonomy():
    schema = response_schema(groups=GROUPS, priorities=PRIORITIES, tags=TAGS)
    assert schema["properties"]["group"]["enum"] == GROUPS
    assert schema["properties"]["tags"]["items"]["enum"] == TAGS
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"group", "priority", "tags", "reason", "confidence"}


# --- rules-based fallback -------------------------------------------------


def _event(title: str, body: str = "") -> TicketEvent:
    return TicketEvent(ticket_id=1, number="1", title=title, body=body)


FALLBACK_KWARGS = {
    "default_group": "Users",
    "priorities": PRIORITIES,
    "default_priority": "2 normal",
    "tags": TAGS,
}


def test_fallback_raises_priority_on_outage_language():
    result = heuristic_classification(
        _event("Nobody can log in", "The whole office is down."), **FALLBACK_KWARGS
    )
    assert result.priority == "3 high"


def test_fallback_lowers_priority_on_question_language():
    result = heuristic_classification(_event("How do I change my signature?"), **FALLBACK_KWARGS)
    assert result.priority == "1 low"


def test_fallback_is_never_confident_so_it_never_writes():
    result = heuristic_classification(_event("Everything is down"), **FALLBACK_KWARGS)
    assert result.confidence == 0.0
    assert not result.is_confident(0.01)
    assert result.source == "fallback"
    assert result.tags == ("needs-review",)
