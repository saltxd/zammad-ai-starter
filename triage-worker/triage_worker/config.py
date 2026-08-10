"""Environment-driven configuration.

One dataclass, built once at startup. Nothing else in the worker reads
``os.environ`` — which means the tests can build a Settings object directly
instead of monkeypatching the environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_GROUPS = ["Users"]
DEFAULT_PRIORITIES = ["1 low", "2 normal", "3 high"]
DEFAULT_TAGS = [
    "access",
    "billing",
    "bug",
    "hardware",
    "how-to",
    "needs-review",
    "network",
    "security",
]


def _split(value: str | None, fallback: list[str]) -> list[str]:
    if not value:
        return list(fallback)
    items = [part.strip() for part in value.split(",")]
    items = [item for item in items if item]
    return items or list(fallback)


def _flag(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # --- Zammad ---
    zammad_url: str = "http://zammad-nginx:8080"
    zammad_token: str = ""
    zammad_user: str = ""
    zammad_password: str = ""
    webhook_secret: str = ""

    # --- providers ---
    provider_order: list[str] = field(default_factory=lambda: ["anthropic", "ollama"])
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-opus-5"
    anthropic_effort: str = "low"
    anthropic_max_tokens: int = 4096
    ollama_url: str = "http://host.docker.internal:11434"
    ollama_model: str = "llama3.1"

    # --- taxonomy ---
    groups: list[str] = field(default_factory=lambda: list(DEFAULT_GROUPS))
    priorities: list[str] = field(default_factory=lambda: list(DEFAULT_PRIORITIES))
    tags: list[str] = field(default_factory=lambda: list(DEFAULT_TAGS))
    default_group: str = "Users"
    default_priority: str = "2 normal"
    max_tags: int = 5

    # --- behaviour ---
    confidence_threshold: float = 0.6
    dry_run: bool = False
    apply_tags: bool = True
    add_note: bool = True
    request_timeout: float = 60.0
    max_body_chars: int = 6000

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> Settings:
        env = os.environ if environ is None else environ

        groups = _split(env.get("TRIAGE_GROUPS"), DEFAULT_GROUPS)
        priorities = _split(env.get("TRIAGE_PRIORITIES"), DEFAULT_PRIORITIES)
        tags = _split(env.get("TRIAGE_TAGS"), DEFAULT_TAGS)

        default_group = env.get("TRIAGE_DEFAULT_GROUP") or groups[0]
        if default_group not in groups:
            groups = [*groups, default_group]

        default_priority = env.get("TRIAGE_DEFAULT_PRIORITY") or (
            priorities[len(priorities) // 2] if priorities else "2 normal"
        )
        if default_priority not in priorities:
            priorities = [*priorities, default_priority]

        return cls(
            zammad_url=(env.get("ZAMMAD_URL") or cls.zammad_url).rstrip("/"),
            zammad_token=env.get("ZAMMAD_TOKEN", ""),
            zammad_user=env.get("ZAMMAD_USER", ""),
            zammad_password=env.get("ZAMMAD_PASSWORD", ""),
            webhook_secret=env.get("ZAMMAD_WEBHOOK_SECRET", ""),
            provider_order=_split(env.get("AI_PROVIDER"), ["anthropic", "ollama"]),
            anthropic_api_key=env.get("ANTHROPIC_API_KEY", ""),
            anthropic_model=env.get("ANTHROPIC_MODEL") or cls.anthropic_model,
            anthropic_effort=env.get("ANTHROPIC_EFFORT") or cls.anthropic_effort,
            anthropic_max_tokens=int(env.get("ANTHROPIC_MAX_TOKENS") or cls.anthropic_max_tokens),
            ollama_url=(env.get("OLLAMA_URL") or cls.ollama_url).rstrip("/"),
            ollama_model=env.get("OLLAMA_MODEL") or cls.ollama_model,
            groups=groups,
            priorities=priorities,
            tags=tags,
            default_group=default_group,
            default_priority=default_priority,
            max_tags=int(env.get("TRIAGE_MAX_TAGS") or cls.max_tags),
            confidence_threshold=float(
                env.get("TRIAGE_CONFIDENCE_THRESHOLD") or cls.confidence_threshold
            ),
            dry_run=_flag(env.get("TRIAGE_DRY_RUN"), cls.dry_run),
            apply_tags=_flag(env.get("TRIAGE_APPLY_TAGS"), cls.apply_tags),
            add_note=_flag(env.get("TRIAGE_ADD_NOTE"), cls.add_note),
            request_timeout=float(env.get("REQUEST_TIMEOUT") or cls.request_timeout),
            max_body_chars=int(env.get("TRIAGE_MAX_BODY_CHARS") or cls.max_body_chars),
        )

    @property
    def zammad_auth_header(self) -> str:
        """``Authorization`` value for the Zammad API.

        Token auth is preferred; HTTP Basic exists because it is the only thing
        available before you have minted a token (see ``scripts/smoke.sh``).
        """
        if self.zammad_token:
            return f"Token token={self.zammad_token}"
        if self.zammad_user and self.zammad_password:
            import base64

            raw = f"{self.zammad_user}:{self.zammad_password}".encode()
            return f"Basic {base64.b64encode(raw).decode()}"
        return ""
