"""Pure parsing of Zammad webhook payloads.

Zammad's webhook posts a JSON object containing the ticket and (usually) the
article that triggered it. Exact key placement varies with the Zammad version
and with whether the trigger fires on create or update, so this parser is
deliberately tolerant: it accepts the nested form, the flat form, and an
``article`` that is either an object or a list.

Everything here is a pure function — no I/O, no config. That is what makes it
cheap to test, and the tests are the part of this repo worth copying.
"""

from __future__ import annotations

import html
import re
from typing import Any

from .models import TicketEvent

_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_END_RE = re.compile(r"(?i)</(p|div|br|li|tr|h[1-6])\s*>|<br\s*/?>")
_WS_RE = re.compile(r"[ \t]+")
_BLANKS_RE = re.compile(r"\n{3,}")


def strip_html(value: str) -> str:
    """Flatten an HTML article body to readable plain text.

    Not a sanitizer — the output is only ever sent to the model and logged.
    """
    text = _BLOCK_END_RE.sub("\n", value)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANKS_RE.sub("\n\n", text).strip()


def _first_article(payload: dict[str, Any]) -> dict[str, Any]:
    article = payload.get("article")
    if isinstance(article, list):
        # Zammad sends newest-last; the triggering article is the last one.
        article = next((a for a in reversed(article) if isinstance(a, dict)), None)
    if isinstance(article, dict):
        return article
    articles = payload.get("articles")
    if isinstance(articles, list):
        return next((a for a in reversed(articles) if isinstance(a, dict)), {})
    return {}


def _name_of(value: Any) -> str:
    """Zammad may send an association as a name string or as a nested object."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("name", "displayname", "email", "login"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
    return ""


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def _tags_of(ticket: dict[str, Any]) -> tuple[str, ...]:
    """Zammad sends tags as a list or as a comma-separated string, depending on version."""
    raw = ticket.get("tags")
    if raw is None:
        raw = ticket.get("tag_list")
    if isinstance(raw, str):
        items = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        items = [str(item).strip() for item in raw]
    else:
        return ()
    return tuple(item for item in items if item)


def _article_body(article: dict[str, Any]) -> str:
    body = article.get("body")
    if not isinstance(body, str):
        return ""
    content_type = str(article.get("content_type") or "").lower()
    if "html" in content_type or "<" in body and ">" in body:
        return strip_html(body)
    return body.strip()


def parse_webhook(payload: Any) -> TicketEvent | None:
    """Extract a :class:`TicketEvent`, or ``None`` if this payload isn't usable.

    Returns ``None`` rather than raising: a webhook we cannot understand should
    be acknowledged and dropped, not retried forever by Zammad.
    """
    if not isinstance(payload, dict):
        return None

    ticket = payload.get("ticket")
    if not isinstance(ticket, dict):
        # Flat form: the payload *is* the ticket.
        ticket = payload if "id" in payload or "number" in payload else {}
    if not isinstance(ticket, dict) or not ticket:
        return None

    raw_id = ticket.get("id")
    try:
        ticket_id = int(raw_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None

    article = _first_article(payload)
    title = ticket.get("title") or article.get("subject") or ""
    customer = _name_of(ticket.get("customer")) or _name_of(article.get("from"))

    return TicketEvent(
        ticket_id=ticket_id,
        number=str(ticket.get("number") or ""),
        title=str(title).strip(),
        body=_article_body(article),
        customer_email=str(customer).strip(),
        group=_name_of(ticket.get("group")),
        priority=_name_of(ticket.get("priority")),
        state=_name_of(ticket.get("state")),
        tags=_tags_of(ticket),
        article_internal=_as_bool(article.get("internal")),
        article_sender=_name_of(article.get("sender")),
        article_type=str(article.get("type") or article.get("type_id") or "").strip(),
        article_subject=str(article.get("subject") or "").strip(),
    )
