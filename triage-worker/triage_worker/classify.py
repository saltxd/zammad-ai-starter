"""Prompt construction, response parsing, and the rules-based fallback.

Every function here is pure. The model call lives in :mod:`providers`; this
module only turns a ticket into a prompt and a model response into a
:class:`Classification`. Split that way, the interesting logic — "what happens
when the model returns prose, or a group that doesn't exist, or nothing at
all" — is testable without a network or an API key.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .models import Classification, TicketEvent

SYSTEM_PROMPT = """\
You are the triage step of a helpdesk. You classify one inbound ticket and \
nothing else. You do not answer the ticket, apologise, or address the customer.

Route the ticket to a queue, set a priority, and apply tags.

Available queues (choose exactly one):
{groups}

Priorities (choose exactly one):
{priorities}

Tags (choose zero or more that apply):
{tags}

Guidance:
- Judge urgency by business impact, not by the customer's tone. An angry \
message about a cosmetic issue is still low priority; a calm message saying \
"nobody in the warehouse can log in" is high.
- If several queues could fit, choose the one that can take the FIRST action.
- Prefer the default queue "{default_group}" over guessing. A ticket in the \
wrong specialist queue is more expensive than one in the general queue.
- Set confidence honestly. Below {confidence_threshold:.2f} the ticket is left \
untouched for a human, which is the correct outcome when the ticket is vague, \
is in a language you are unsure of, or could plausibly go to two queues. Do \
not inflate confidence to seem useful.
- reason: one sentence, for the agent who picks this up. Say what drove the \
routing, not what the ticket said.
"""

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)
_TAG_SPLIT_RE = re.compile(r"[,\n;]")


class ClassificationError(ValueError):
    """The model response contained no usable classification."""


def build_prompt(
    *,
    groups: list[str],
    priorities: list[str],
    tags: list[str],
    default_group: str,
    confidence_threshold: float,
) -> str:
    """Render the system prompt for a given taxonomy."""

    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {item}" for item in items) if items else "- (none)"

    return SYSTEM_PROMPT.format(
        groups=bullets(groups),
        priorities=bullets(priorities),
        tags=bullets(tags),
        default_group=default_group,
        confidence_threshold=confidence_threshold,
    )


def response_schema(*, groups: list[str], priorities: list[str], tags: list[str]) -> dict[str, Any]:
    """JSON Schema for the classification, for providers that can enforce one.

    Parsing never relies on this — :func:`parse_classification` validates the
    same constraints — but constraining the model is cheaper than repairing it.
    """
    tag_items: dict[str, Any] = {"type": "string"}
    if tags:
        tag_items["enum"] = list(tags)
    return {
        "type": "object",
        "properties": {
            "group": {"type": "string", "enum": list(groups)},
            "priority": {"type": "string", "enum": list(priorities)},
            "tags": {"type": "array", "items": tag_items},
            "reason": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": ["group", "priority", "tags", "reason", "confidence"],
        "additionalProperties": False,
    }


def extract_json_object(text: str) -> dict[str, Any]:
    """Pull the first balanced JSON object out of a model response.

    Handles bare JSON, fenced JSON, and JSON with prose on either side.
    """
    if not text or not text.strip():
        raise ClassificationError("empty model response")

    cleaned = _FENCE_RE.sub("", text).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed

    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(cleaned):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                candidate = cleaned[start : index + 1]
                try:
                    obj = json.loads(candidate)
                except json.JSONDecodeError:
                    start = -1
                    continue
                if isinstance(obj, dict):
                    return obj
                start = -1

    raise ClassificationError("no JSON object found in model response")


def _match_choice(value: Any, allowed: list[str], default: str) -> str:
    """Case- and whitespace-insensitive match against an allowed list."""
    if not isinstance(value, str):
        return default
    needle = value.strip().casefold()
    if not needle:
        return default
    for choice in allowed:
        if choice.casefold() == needle:
            return choice
    # Zammad priorities are "1 low" / "2 normal" / "3 high"; models often reply
    # with just "low". Match on the label, ignoring any leading sort key.
    for choice in allowed:
        stripped = choice.split(" ", 1)[-1].casefold()
        if stripped == needle:
            return choice
    return default


def normalize_tags(value: Any, *, allowed: list[str], limit: int) -> tuple[str, ...]:
    """Lower-case, de-duplicate, and (if a vocabulary is set) filter tags."""
    if isinstance(value, str):
        raw = list(_TAG_SPLIT_RE.split(value))
    elif isinstance(value, (list, tuple)):
        raw = [item for item in value if isinstance(item, str)]
    else:
        return ()

    lookup = {tag.casefold(): tag for tag in allowed}
    seen: list[str] = []
    for item in raw:
        tag = item.strip().strip("#").casefold()
        if not tag:
            continue
        if lookup:
            canonical = lookup.get(tag)
            if canonical is None:
                continue
        else:
            canonical = tag
        if canonical not in seen:
            seen.append(canonical)
    return tuple(seen[:limit])


def parse_classification(
    text: str,
    *,
    groups: list[str],
    priorities: list[str],
    tags: list[str],
    default_group: str,
    default_priority: str,
    max_tags: int = 5,
) -> Classification:
    """Turn a raw model response into a valid :class:`Classification`.

    Unknown groups, priorities, and tags are clamped to defaults rather than
    rejected — a ticket landing in the default queue is a much better failure
    than a 500 and a retry storm. Only a response with no JSON at all raises.
    """
    payload = extract_json_object(text)

    confidence_raw = payload.get("confidence", 0.0)
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence > 1.0:
        # Models sometimes answer on a 0-100 scale despite the schema.
        confidence = confidence / 100.0 if confidence <= 100.0 else 1.0
    confidence = max(0.0, min(1.0, confidence))

    reason = payload.get("reason", "")
    if not isinstance(reason, str):
        reason = ""

    return Classification(
        group=_match_choice(payload.get("group"), groups, default_group),
        priority=_match_choice(payload.get("priority"), priorities, default_priority),
        tags=normalize_tags(payload.get("tags"), allowed=tags, limit=max_tags),
        reason=reason.strip()[:500],
        confidence=confidence,
        source="model",
    )


# --- rules-based fallback -------------------------------------------------
#
# Used only when every provider fails. It is intentionally dumb: its job is to
# keep the queue moving during an outage, not to be clever. It reports low
# confidence so the "leave it for a human" path stays in control.

_HIGH_SIGNALS = (
    "outage",
    "down",
    "cannot log in",
    "can't log in",
    "unable to log in",
    "not working for everyone",
    "everyone",
    "all users",
    "production",
    "urgent",
    "critical",
    "data loss",
    "breach",
)
_LOW_SIGNALS = (
    "question",
    "how do i",
    "how to",
    "documentation",
    "feature request",
    "suggestion",
    "nice to have",
    "whenever you get a chance",
)


def heuristic_classification(
    event: TicketEvent,
    *,
    default_group: str,
    priorities: list[str],
    default_priority: str,
    tags: list[str],
) -> Classification:
    """Keyword-only classification for when the model is unreachable."""
    haystack = f"{event.title}\n{event.body}".casefold()

    priority = default_priority
    if any(signal in haystack for signal in _HIGH_SIGNALS):
        priority = _match_choice("high", priorities, default_priority)
    elif any(signal in haystack for signal in _LOW_SIGNALS):
        priority = _match_choice("low", priorities, default_priority)

    return Classification(
        group=default_group,
        priority=priority,
        tags=normalize_tags(["needs-review"], allowed=tags, limit=1),
        reason="Rules-based fallback: no AI provider was reachable.",
        confidence=0.0,
        source="fallback",
    )
