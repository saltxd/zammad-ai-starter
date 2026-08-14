"""FastAPI webhook receiver: Zammad ticket created -> classify -> write back.

The request path is deliberately short. Everything with a decision in it lives
in :mod:`classify` (pure) or :mod:`zammad` (I/O); this module wires them
together and owns three policies:

1. Never 500 on a webhook. Zammad retries failures, and a retry storm on top of
   a provider outage is how one bad afternoon becomes an incident. Failures are
   logged and acknowledged.
2. Never write a low-confidence classification. Below the threshold the ticket
   is left exactly as the customer sent it, which is the state a human agent
   already knows how to handle.
3. Never re-triage a ticket the worker already touched. An update trigger that
   fires on the internal note, or Zammad retrying the original create payload,
   must be a no-op — not a second model call and a second note.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from .classify import (
    build_prompt,
    heuristic_classification,
    parse_classification,
    response_schema,
)
from .config import Settings
from .models import Classification, TicketEvent, TriageResult
from .providers import ProviderChain, build_chain
from .webhook import parse_webhook
from .zammad import ZammadClient, ZammadError

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)
logger = logging.getLogger("triage_worker")


def verify_signature(body: bytes, header: str | None, secret: str) -> bool:
    """Verify Zammad's ``X-Hub-Signature`` (HMAC-SHA1, GitHub style).

    Zammad computes the digest over the raw request body, so this must run on
    the bytes as received — re-serialising the parsed JSON will not match.
    """
    if not secret:
        return True
    if not header:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()
    received = header.split("=", 1)[-1].strip()
    return hmac.compare_digest(expected, received)


class Triager:
    """Classify one ticket and apply the result."""

    def __init__(
        self, settings: Settings, providers: ProviderChain, zammad: ZammadClient | None
    ) -> None:
        self.settings = settings
        self.providers = providers
        self.zammad = zammad
        self.system_prompt = build_prompt(
            groups=settings.groups,
            priorities=settings.priorities,
            tags=settings.tags,
            default_group=settings.default_group,
            confidence_threshold=settings.confidence_threshold,
        )
        self.schema = response_schema(
            groups=settings.groups,
            priorities=settings.priorities,
            tags=settings.tags,
        )

    def _untouched(self, event: TicketEvent) -> Classification:
        """A placeholder classification for tickets we refused to look at."""
        return Classification(
            group=event.group or self.settings.default_group,
            priority=event.priority or self.settings.default_priority,
            tags=event.tags,
            source="skipped",
        )

    async def skip_reason(self, event: TicketEvent) -> str:
        """Why this webhook should not reach the model, or ``""`` to proceed.

        Two cheap checks first (no I/O), then a live tag read. Zammad retries
        resend the original payload, so the tag on the ticket in that JSON is
        still the pre-triage set — only the API knows we already stamped it.
        """
        if event.article_internal:
            return "internal article"

        done = self.settings.done_tag
        if done and done in event.tags:
            return "already triaged"

        if done and self.zammad is not None and not self.settings.dry_run:
            try:
                live = await self.zammad.list_tags(event.ticket_id)
            except ZammadError as exc:
                logger.warning("ticket %s: could not read tags: %s", event.ticket_id, exc)
            else:
                if done in live:
                    return "already triaged"
        return ""

    async def classify(self, event: TicketEvent) -> Classification:
        settings = self.settings
        user_turn = event.as_prompt_text()[: settings.max_body_chars]
        try:
            raw = await self.providers.complete(self.system_prompt, user_turn, self.schema)
            return parse_classification(
                raw,
                groups=settings.groups,
                priorities=settings.priorities,
                tags=settings.tags,
                default_group=settings.default_group,
                default_priority=settings.default_priority,
                max_tags=settings.max_tags,
            )
        except Exception as exc:  # noqa: BLE001 - provider or parse failure; rules take over
            logger.warning("ticket %s: falling back to rules (%s)", event.ticket_id, exc)
            return heuristic_classification(
                event,
                default_group=settings.default_group,
                priorities=settings.priorities,
                default_priority=settings.default_priority,
                tags=settings.tags,
            )

    async def apply(self, event: TicketEvent, classification: Classification) -> TriageResult:
        settings = self.settings

        if not classification.is_confident(settings.confidence_threshold):
            logger.info(
                "ticket %s: confidence %.2f < %.2f — leaving for a human",
                event.ticket_id,
                classification.confidence,
                settings.confidence_threshold,
            )
            return TriageResult(
                event.ticket_id,
                classification,
                applied=False,
                skipped_reason="below confidence threshold",
            )

        if settings.dry_run or self.zammad is None:
            logger.info(
                "ticket %s: DRY RUN -> group=%s priority=%s tags=%s",
                event.ticket_id,
                classification.group,
                classification.priority,
                ",".join(classification.tags) or "-",
            )
            return TriageResult(
                event.ticket_id,
                classification,
                applied=False,
                skipped_reason="dry run",
            )

        changes = await self.zammad.update_ticket(
            event.ticket_id,
            group=classification.group,
            priority=classification.priority,
        )
        tags_to_add = list(classification.tags) if settings.apply_tags else []
        if settings.done_tag and settings.done_tag not in tags_to_add:
            tags_to_add.append(settings.done_tag)
        if tags_to_add:
            await self.zammad.add_tags(event.ticket_id, tags_to_add)
            changes["tags"] = tags_to_add
        if settings.add_note and classification.reason:
            await self.zammad.add_internal_note(
                event.ticket_id,
                f"Routed to {classification.group} at {classification.priority} "
                f"(confidence {classification.confidence:.2f}).\n\n"
                f"{classification.reason}",
            )

        logger.info(
            "ticket %s: group=%s priority=%s tags=%s confidence=%.2f source=%s",
            event.ticket_id,
            classification.group,
            classification.priority,
            ",".join(classification.tags) or "-",
            classification.confidence,
            classification.source,
        )
        return TriageResult(event.ticket_id, classification, applied=True, changes=changes)

    async def handle(self, event: TicketEvent) -> TriageResult:
        reason = await self.skip_reason(event)
        if reason:
            logger.info("ticket %s: skipping — %s", event.ticket_id, reason)
            return TriageResult(
                event.ticket_id,
                self._untouched(event),
                applied=False,
                skipped_reason=reason,
            )
        return await self.apply(event, await self.classify(event))


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        providers = build_chain(settings)
        zammad: ZammadClient | None = None
        if not settings.dry_run:
            try:
                zammad = ZammadClient(
                    base_url=settings.zammad_url,
                    auth_header=settings.zammad_auth_header,
                    timeout=settings.request_timeout,
                )
            except ValueError as exc:
                logger.error("%s Running in dry-run mode.", exc)

        app.state.triager = Triager(settings, providers, zammad)
        app.state.settings = settings
        logger.info(
            "triage worker ready: providers=%s zammad=%s groups=%s threshold=%.2f",
            providers.names or ["(none — rules only)"],
            settings.zammad_url if zammad else "disabled",
            settings.groups,
            settings.confidence_threshold,
        )
        try:
            yield
        finally:
            await providers.aclose()
            if zammad is not None:
                await zammad.aclose()

    app = FastAPI(
        title="Zammad AI triage worker",
        version="1.0.0",
        summary="Classifies inbound Zammad tickets and writes back group, priority, and tags.",
        lifespan=lifespan,
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        triager: Triager = app.state.triager
        return {
            "status": "ok",
            "providers": triager.providers.names,
            "zammad": triager.zammad is not None,
            "dry_run": triager.settings.dry_run,
        }

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        """Ready only once Zammad answers — used by the compose healthcheck."""
        triager: Triager = app.state.triager
        if triager.zammad is None:
            return JSONResponse({"status": "ok", "zammad": "disabled"})
        ok = await triager.zammad.ping()
        return JSONResponse(
            {"status": "ok" if ok else "unavailable", "zammad": ok},
            status_code=200 if ok else 503,
        )

    @app.post("/webhook/zammad")
    async def zammad_webhook(
        request: Request,
        x_hub_signature: str | None = Header(default=None),
    ) -> JSONResponse:
        triager: Triager = app.state.triager
        body = await request.body()

        if not verify_signature(body, x_hub_signature, triager.settings.webhook_secret):
            logger.warning("rejected webhook: bad or missing X-Hub-Signature")
            return JSONResponse({"error": "invalid signature"}, status_code=401)

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "invalid JSON"}, status_code=400)

        event = parse_webhook(payload)
        if event is None:
            # Acknowledge: Zammad would otherwise retry a payload we will never
            # understand.
            logger.info("ignoring webhook with no usable ticket payload")
            return JSONResponse({"status": "ignored"}, status_code=202)

        try:
            result = await triager.handle(event)
        except ZammadError as exc:
            logger.error("ticket %s: Zammad write failed: %s", event.ticket_id, exc)
            return JSONResponse(
                {"status": "error", "ticket_id": event.ticket_id, "error": str(exc)},
                status_code=200,
            )

        return JSONResponse(
            {
                "status": "applied" if result.applied else "skipped",
                "ticket_id": result.ticket_id,
                "group": result.classification.group,
                "priority": result.classification.priority,
                "tags": list(result.classification.tags),
                "confidence": round(result.classification.confidence, 3),
                "source": result.classification.source,
                "reason": result.classification.reason,
                "skipped_reason": result.skipped_reason,
            }
        )

    return app


app = create_app()
