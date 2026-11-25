"""Chat agent configuration.

Stored as rows in the settings table under ``chat_agent_*`` keys. Bot tokens and the
LLM API key are encrypted at rest with the engine's secret key (core/encryption.py),
never returned by the API and never logged. Each token can instead come from the
environment, which wins over the stored value:

- ``OPHQ_CHAT_SLACK_BOT_TOKEN`` (xoxb-...), ``OPHQ_CHAT_SLACK_APP_TOKEN`` (xapp-...)
- ``OPHQ_CHAT_DISCORD_BOT_TOKEN``
- ``OPHQ_CHAT_LLM_API_KEY``

The feature is off unless ``chat_agent_enabled`` is on and at least one platform is
enabled with its tokens and at least one allowed channel.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from sqlalchemy import select

from backend.app.models.settings import Settings

logger = logging.getLogger(__name__)

PREFIX = "chat_agent_"

SECRET_FIELDS = ("slack_bot_token", "slack_app_token", "discord_bot_token", "llm_api_key")
SECRET_ENV = {
    "slack_bot_token": "OPHQ_CHAT_SLACK_BOT_TOKEN",
    "slack_app_token": "OPHQ_CHAT_SLACK_APP_TOKEN",
    "discord_bot_token": "OPHQ_CHAT_DISCORD_BOT_TOKEN",
    "llm_api_key": "OPHQ_CHAT_LLM_API_KEY",
}
# Settings keys that hold secrets; backups leave them out.
SECRET_SETTING_KEYS = frozenset(PREFIX + f for f in SECRET_FIELDS)

BOOL_FIELDS = ("enabled", "slack_enabled", "discord_enabled", "allow_dms", "llm_enabled")
LIST_FIELDS = (
    "slack_allowed_channels",
    "slack_allowed_workspaces",
    "discord_allowed_channels",
    "discord_allowed_guilds",
)
STR_FIELDS = ("llm_base_url", "llm_model")
INT_FIELDS = ("confirm_timeout_seconds",)

CONFIRM_TIMEOUT_DEFAULT = 120
CONFIRM_TIMEOUT_MIN = 30
CONFIRM_TIMEOUT_MAX = 900


def _split_list(value: str | None) -> list[str]:
    if not value:
        return []
    out: list[str] = []
    for part in value.replace("\n", ",").split(","):
        part = part.strip()
        if part and part not in out:
            out.append(part[:64])
    return out


@dataclass
class ChatAgentConfig:
    enabled: bool = False
    slack_enabled: bool = False
    discord_enabled: bool = False
    allow_dms: bool = False
    llm_enabled: bool = False
    slack_allowed_channels: list[str] = field(default_factory=list)
    slack_allowed_workspaces: list[str] = field(default_factory=list)
    discord_allowed_channels: list[str] = field(default_factory=list)
    discord_allowed_guilds: list[str] = field(default_factory=list)
    llm_base_url: str = ""
    llm_model: str = ""
    confirm_timeout_seconds: int = CONFIRM_TIMEOUT_DEFAULT
    slack_bot_token: str = field(default="", repr=False)
    slack_app_token: str = field(default="", repr=False)
    discord_bot_token: str = field(default="", repr=False)
    llm_api_key: str = field(default="", repr=False)
    # Which secrets come from the environment (so the UI can say so).
    from_env: set[str] = field(default_factory=set)

    @property
    def slack_ready(self) -> bool:
        return (
            self.enabled
            and self.slack_enabled
            and bool(self.slack_bot_token)
            and bool(self.slack_app_token)
            and bool(self.slack_allowed_channels or self.allow_dms)
        )

    @property
    def discord_ready(self) -> bool:
        return (
            self.enabled
            and self.discord_enabled
            and bool(self.discord_bot_token)
            and bool(self.discord_allowed_channels or self.allow_dms)
        )

    @property
    def llm_ready(self) -> bool:
        return self.llm_enabled and bool(self.llm_base_url) and bool(self.llm_model)

    def allowed_channels(self, platform: str) -> list[str]:
        return self.slack_allowed_channels if platform == "slack" else self.discord_allowed_channels

    def allowed_teams(self, platform: str) -> list[str]:
        return self.slack_allowed_workspaces if platform == "slack" else self.discord_allowed_guilds

    def public_dict(self) -> dict:
        """Everything except secret values; secrets become ``<name>_set`` flags."""
        out = {
            "enabled": self.enabled,
            "slack_enabled": self.slack_enabled,
            "discord_enabled": self.discord_enabled,
            "allow_dms": self.allow_dms,
            "llm_enabled": self.llm_enabled,
            "slack_allowed_channels": list(self.slack_allowed_channels),
            "slack_allowed_workspaces": list(self.slack_allowed_workspaces),
            "discord_allowed_channels": list(self.discord_allowed_channels),
            "discord_allowed_guilds": list(self.discord_allowed_guilds),
            "llm_base_url": self.llm_base_url,
            "llm_model": self.llm_model,
            "confirm_timeout_seconds": self.confirm_timeout_seconds,
        }
        for name in SECRET_FIELDS:
            out[f"{name}_set"] = bool(getattr(self, name))
            out[f"{name}_from_env"] = name in self.from_env
        return out


def _decrypt(value: str) -> str:
    from backend.app.core.encryption import mfa_decrypt

    try:
        return mfa_decrypt(value)
    except Exception:  # noqa: BLE001 - an unreadable secret is treated as unset
        logger.warning("chat agent: a stored token could not be decrypted; treating it as unset")
        return ""


async def load_config(db) -> ChatAgentConfig:
    rows = (await db.execute(select(Settings).where(Settings.key.startswith(PREFIX)))).scalars().all()
    raw = {r.key[len(PREFIX) :]: r.value for r in rows}
    cfg = ChatAgentConfig()
    for name in BOOL_FIELDS:
        if name in raw:
            setattr(cfg, name, str(raw[name]).lower() in ("1", "true", "yes", "on"))
    for name in LIST_FIELDS:
        setattr(cfg, name, _split_list(raw.get(name)))
    for name in STR_FIELDS:
        setattr(cfg, name, (raw.get(name) or "").strip())
    try:
        t = int(raw.get("confirm_timeout_seconds") or CONFIRM_TIMEOUT_DEFAULT)
    except ValueError:
        t = CONFIRM_TIMEOUT_DEFAULT
    cfg.confirm_timeout_seconds = max(CONFIRM_TIMEOUT_MIN, min(CONFIRM_TIMEOUT_MAX, t))
    for name in SECRET_FIELDS:
        env = os.environ.get(SECRET_ENV[name], "").strip()
        if env:
            setattr(cfg, name, env)
            cfg.from_env.add(name)
        elif raw.get(name):
            setattr(cfg, name, _decrypt(raw[name]))
    return cfg


async def _set(db, key: str, value: str) -> None:
    row = (await db.execute(select(Settings).where(Settings.key == key))).scalar_one_or_none()
    if row is None:
        db.add(Settings(key=key, value=value))
    else:
        row.value = value


async def _delete(db, key: str) -> None:
    row = (await db.execute(select(Settings).where(Settings.key == key))).scalar_one_or_none()
    if row is not None:
        await db.delete(row)


async def save_config(db, changes: dict) -> None:
    """Apply a partial update. Secrets: a non-empty string sets, "" clears, None keeps."""
    from backend.app.core.encryption import mfa_encrypt

    for name, value in changes.items():
        key = PREFIX + name
        if name in BOOL_FIELDS:
            if value is not None:
                await _set(db, key, "true" if value else "false")
        elif name in LIST_FIELDS:
            if value is not None:
                items = value if isinstance(value, list) else _split_list(str(value))
                await _set(db, key, ",".join(_split_list(",".join(str(i) for i in items))))
        elif name in STR_FIELDS:
            if value is not None:
                await _set(db, key, str(value).strip()[:500])
        elif name in INT_FIELDS:
            if value is not None:
                await _set(db, key, str(max(CONFIRM_TIMEOUT_MIN, min(CONFIRM_TIMEOUT_MAX, int(value)))))
        elif name in SECRET_FIELDS:
            if value is None:
                continue
            value = str(value).strip()
            if value:
                await _set(db, key, mfa_encrypt(value))
            else:
                await _delete(db, key)
    await db.commit()
