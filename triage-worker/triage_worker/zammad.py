"""A small async Zammad REST client — only the three calls triage needs.

Group and priority are sent **by name**. Zammad resolves them server-side and
rejects an unknown one with `422 No lookup value found for 'group': "..."`,
leaving the ticket untouched — so a typo in `TRIAGE_GROUPS` surfaces as a loud
error on the first ticket rather than as a silent no-op.

That matters for more than tidiness: resolving names to IDs client-side would
mean calling `GET /api/v1/groups`, which requires an admin-scoped token. Letting
Zammad do the lookup keeps the worker on `ticket.agent` — the least privilege
that can do the job — and saves two HTTP round trips per ticket.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class ZammadError(RuntimeError):
    """A Zammad API call failed."""


class ZammadClient:
    def __init__(
        self,
        *,
        base_url: str,
        auth_header: str,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not auth_header:
            raise ValueError(
                "No Zammad credentials: set ZAMMAD_TOKEN, or ZAMMAD_USER + ZAMMAD_PASSWORD."
            )
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": auth_header,
                "Content-Type": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, path, **kwargs)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ZammadError(
                f"{method} {path} -> {exc.response.status_code}: {exc.response.text[:300]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ZammadError(f"{method} {path} failed: {exc}") from exc
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    async def update_ticket(
        self, ticket_id: int, *, group: str | None = None, priority: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if group:
            payload["group"] = group
        if priority:
            payload["priority"] = priority
        if not payload:
            return {}
        await self._request("PUT", f"/api/v1/tickets/{ticket_id}", json=payload)
        return payload

    async def add_tags(self, ticket_id: int, tags: tuple[str, ...] | list[str]) -> None:
        for tag in tags:
            await self._request(
                "POST",
                "/api/v1/tags/add",
                params={"object": "Ticket", "o_id": ticket_id, "item": tag},
            )

    async def add_internal_note(self, ticket_id: int, body: str) -> None:
        """Leave the triage rationale on the ticket, visible to agents only."""
        await self._request(
            "POST",
            "/api/v1/ticket_articles",
            json={
                "ticket_id": ticket_id,
                "body": body,
                "type": "note",
                "internal": True,
                "content_type": "text/plain",
                "subject": "AI triage",
            },
        )

    async def ping(self) -> bool:
        """True if the configured credentials can reach the API."""
        try:
            await self._request("GET", "/api/v1/users/me")
            return True
        except ZammadError as exc:
            logger.warning("Zammad ping failed: %s", exc)
            return False
