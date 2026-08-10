"""A small async Zammad REST client — only the four calls triage needs.

Groups and priorities are resolved to IDs rather than passed by name. Zammad
accepts names on some endpoints and IDs on others, and silently ignoring an
unknown name is exactly the kind of failure that looks like "the AI didn't do
anything". Resolving up front means an unknown queue is a loud error.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class ZammadError(RuntimeError):
    """A Zammad API call failed."""


class ZammadClient:
    def __init__(self, *, base_url: str, auth_header: str, timeout: float = 30.0) -> None:
        if not auth_header:
            raise ValueError(
                "No Zammad credentials: set ZAMMAD_TOKEN, or ZAMMAD_USER + ZAMMAD_PASSWORD."
            )
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={
                "Authorization": auth_header,
                "Content-Type": "application/json",
            },
        )
        self._groups: dict[str, int] = {}
        self._priorities: dict[str, int] = {}

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

    async def _load_lookup(self, path: str) -> dict[str, int]:
        rows = await self._request("GET", path)
        table: dict[str, int] = {}
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("name") and row.get("id"):
                    table[str(row["name"]).casefold()] = int(row["id"])
        return table

    async def group_id(self, name: str) -> int:
        if not self._groups:
            self._groups = await self._load_lookup("/api/v1/groups")
        try:
            return self._groups[name.casefold()]
        except KeyError:
            known = ", ".join(sorted(self._groups)) or "(none)"
            raise ZammadError(
                f"Zammad has no group named {name!r}. Known groups: {known}"
            ) from None

    async def priority_id(self, name: str) -> int:
        if not self._priorities:
            self._priorities = await self._load_lookup("/api/v1/ticket_priorities")
        try:
            return self._priorities[name.casefold()]
        except KeyError:
            known = ", ".join(sorted(self._priorities)) or "(none)"
            raise ZammadError(
                f"Zammad has no priority named {name!r}. Known priorities: {known}"
            ) from None

    async def update_ticket(
        self, ticket_id: int, *, group: str | None = None, priority: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if group:
            payload["group_id"] = await self.group_id(group)
        if priority:
            payload["priority_id"] = await self.priority_id(priority)
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
