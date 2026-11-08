"""Starts, stops and reloads the chat transports from the saved configuration."""

from __future__ import annotations

import asyncio
import logging

from backend.app.core import database
from backend.app.services.chat_agent.agent import ChatAgent
from backend.app.services.chat_agent.config import ChatAgentConfig, load_config

logger = logging.getLogger(__name__)


class ChatAgentManager:
    def __init__(self):
        self.agent: ChatAgent | None = None
        self.adapters: dict[str, object] = {}
        self.errors: dict[str, str] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Start whatever the saved configuration enables. Never raises."""
        try:
            async with database.async_session() as db:
                cfg = await load_config(db)
        except Exception as e:  # noqa: BLE001 - a broken config must not stop the engine
            logger.warning("chat agent: could not load configuration: %s", e)
            return
        await self.apply(cfg)

    async def apply(self, cfg: ChatAgentConfig) -> None:
        async with self._lock:
            await self._stop_adapters()
            self.errors = {}
            if not cfg.enabled or not (cfg.slack_ready or cfg.discord_ready):
                self.agent = None
                return
            if self.agent is None:
                self.agent = ChatAgent(cfg)
            else:
                self.agent.config = cfg  # keep pending confirmations across a reload
            if cfg.slack_ready:
                await self._start_one("slack", cfg)
            if cfg.discord_ready:
                await self._start_one("discord", cfg)

    async def _start_one(self, platform: str, cfg: ChatAgentConfig) -> None:
        try:
            if platform == "slack":
                from backend.app.services.chat_agent.slack_adapter import SlackAdapter

                adapter = SlackAdapter(self.agent, cfg.slack_bot_token, cfg.slack_app_token)
            else:
                from backend.app.services.chat_agent.discord_adapter import DiscordAdapter

                adapter = DiscordAdapter(self.agent, cfg.discord_bot_token)
            await adapter.start()
            self.adapters[platform] = adapter
        except Exception as e:  # noqa: BLE001 - report the class only; messages can echo tokens
            self.errors[platform] = type(e).__name__
            logger.warning("chat agent: %s did not start: %s", platform, type(e).__name__)

    async def _stop_adapters(self) -> None:
        for platform, adapter in list(self.adapters.items()):
            try:
                await adapter.stop()
            except Exception as e:  # noqa: BLE001
                logger.debug("chat agent: stopping %s failed: %s", platform, e)
        self.adapters = {}

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_adapters()
            self.agent = None

    def status(self) -> dict:
        out = {}
        for platform in ("slack", "discord"):
            adapter = self.adapters.get(platform)
            err = self.errors.get(platform) or (getattr(adapter, "last_error", None) if adapter else None)
            out[platform] = {"running": adapter is not None and not err, "error": err}
        return out


chat_agent_manager = ChatAgentManager()
