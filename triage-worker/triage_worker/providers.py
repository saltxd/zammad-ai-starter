"""Model providers, plus the fallback chain that ties them together.

Two providers ship here — Anthropic's Claude API and a local Ollama server —
behind one three-line protocol. Adding a third is a class with a ``complete``
method; nothing else in the worker changes.

The chain exists because a triage worker that stops working when one API has a
bad afternoon is worse than useless: tickets silently stop being routed. Every
provider is tried in order, and :mod:`main` falls back to keyword rules if they
all fail.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """A provider could not produce a usable response."""


class AllProvidersFailed(ProviderError):
    """Every configured provider failed."""


class Provider(Protocol):
    name: str

    async def complete(self, system: str, user: str, schema: dict[str, Any]) -> str:
        """Return the model's raw text response."""
        ...

    async def aclose(self) -> None: ...


class AnthropicProvider:
    """Claude via the official SDK, with the response shape constrained.

    ``output_config.format`` makes the API enforce the classification schema,
    so the parser is a safety net rather than a repair shop. ``effort`` is set
    low: triage is a short classification, not a reasoning problem, and low
    effort is where the cost/latency win is.
    """

    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        effort: str = "low",
        max_tokens: int = 4096,
        timeout: float = 60.0,
    ) -> None:
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=api_key, timeout=timeout)
        self._model = model
        self._effort = effort
        self._max_tokens = max_tokens

    async def complete(self, system: str, user: str, schema: dict[str, Any]) -> str:
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            output_config={
                "effort": self._effort,
                "format": {"type": "json_schema", "schema": schema},
            },
            messages=[{"role": "user", "content": user}],
        )

        # Check stop_reason before reading content: a refusal returns HTTP 200
        # with empty or partial content, and indexing content[0] would blow up.
        if response.stop_reason == "refusal":
            raise ProviderError("anthropic declined to classify this ticket")
        if response.stop_reason == "max_tokens":
            raise ProviderError("anthropic response hit max_tokens; raise ANTHROPIC_MAX_TOKENS")

        text = "".join(block.text for block in response.content if block.type == "text")
        if not text.strip():
            raise ProviderError("anthropic returned no text content")
        return text

    async def aclose(self) -> None:
        await self._client.close()


class OllamaProvider:
    """A local model over Ollama's chat API.

    Here so the whole pipeline can run with no API key and no data leaving the
    building — which is the reason a lot of teams self-host a helpdesk at all.
    """

    name = "ollama"

    def __init__(self, *, url: str, model: str, timeout: float = 120.0) -> None:
        self._client = httpx.AsyncClient(base_url=url, timeout=timeout)
        self._model = model

    async def complete(self, system: str, user: str, schema: dict[str, Any]) -> str:
        try:
            response = await self._client.post(
                "/api/chat",
                json={
                    "model": self._model,
                    "stream": False,
                    # Ollama >= 0.5 accepts a JSON Schema here; older versions
                    # only understand the string "json" and ignore the rest.
                    "format": schema,
                    "options": {"temperature": 0},
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderError(f"ollama request failed: {exc}") from exc

        payload = response.json()
        text = (payload.get("message") or {}).get("content") or ""
        if not text.strip():
            raise ProviderError("ollama returned no content")
        return text

    async def aclose(self) -> None:
        await self._client.aclose()


class ProviderChain:
    """Try each provider in order; the first usable response wins."""

    def __init__(self, providers: list[Provider]) -> None:
        self.providers = providers

    @property
    def names(self) -> list[str]:
        return [provider.name for provider in self.providers]

    async def complete(self, system: str, user: str, schema: dict[str, Any]) -> str:
        errors: list[str] = []
        for provider in self.providers:
            try:
                return await provider.complete(system, user, schema)
            except Exception as exc:  # noqa: BLE001 - any failure means "try the next one"
                logger.warning("provider %s failed: %s", provider.name, exc)
                errors.append(f"{provider.name}: {exc}")
        raise AllProvidersFailed("; ".join(errors) or "no providers configured")

    async def aclose(self) -> None:
        for provider in self.providers:
            try:
                await provider.aclose()
            except Exception:  # noqa: BLE001 - shutdown is best-effort
                logger.debug("error closing provider %s", provider.name, exc_info=True)


def build_chain(settings: Any) -> ProviderChain:
    """Instantiate the providers named in ``AI_PROVIDER``, skipping unconfigured ones."""
    providers: list[Provider] = []
    for name in settings.provider_order:
        key = name.strip().lower()
        if key == "anthropic":
            if not settings.anthropic_api_key:
                logger.info("skipping anthropic provider: ANTHROPIC_API_KEY is not set")
                continue
            providers.append(
                AnthropicProvider(
                    api_key=settings.anthropic_api_key,
                    model=settings.anthropic_model,
                    effort=settings.anthropic_effort,
                    max_tokens=settings.anthropic_max_tokens,
                    timeout=settings.request_timeout,
                )
            )
        elif key == "ollama":
            if not settings.ollama_url:
                continue
            providers.append(
                OllamaProvider(
                    url=settings.ollama_url,
                    model=settings.ollama_model,
                    timeout=settings.request_timeout,
                )
            )
        elif key:
            logger.warning("unknown provider %r in AI_PROVIDER; ignoring", name)
    return ProviderChain(providers)
