"""Data shapes passed between the webhook, the classifier, and the Zammad client.

Deliberately small and provider-agnostic: every provider returns text, every
parser returns one of these.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TicketEvent:
    """The parts of a Zammad webhook payload the triage worker actually needs."""

    ticket_id: int
    number: str
    title: str
    body: str
    customer_email: str = ""
    group: str = ""
    priority: str = ""
    state: str = ""
    tags: tuple[str, ...] = ()
    article_internal: bool = False
    article_sender: str = ""
    article_type: str = ""
    article_subject: str = ""

    def as_prompt_text(self) -> str:
        """Render the ticket as the user turn sent to the model."""
        lines = [f"Subject: {self.title}"]
        if self.customer_email:
            lines.append(f"From: {self.customer_email}")
        lines.append("")
        lines.append(self.body.strip() or "(no message body)")
        return "\n".join(lines)


@dataclass(frozen=True)
class Classification:
    """A triage decision. Always valid — the parser clamps unknown values."""

    group: str
    priority: str
    tags: tuple[str, ...] = ()
    reason: str = ""
    confidence: float = 0.0
    source: str = "model"

    def is_confident(self, threshold: float) -> bool:
        return self.confidence >= threshold


@dataclass
class TriageResult:
    """What the worker did with a ticket — returned by the webhook for visibility."""

    ticket_id: int
    classification: Classification
    applied: bool
    skipped_reason: str = ""
    changes: dict[str, object] = field(default_factory=dict)
