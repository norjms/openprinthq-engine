"""Chat agent transports with mocked Slack and Discord clients, and the manager."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.app.services.chat_agent.agent import IncomingMessage, Reply
from backend.app.services.chat_agent.config import ChatAgentConfig
from backend.app.services.chat_agent.discord_adapter import DiscordAdapter
from backend.app.services.chat_agent.manager import ChatAgentManager
from backend.app.services.chat_agent.slack_adapter import (
    ACTION_CONFIRM,
    ACTION_DECLINE,
    SlackAdapter,
    reply_payload,
)

pytestmark = pytest.mark.asyncio


def fake_agent(replies=None, button_reply=None):
    agent = MagicMock()
    agent.config = ChatAgentConfig(confirm_timeout_seconds=60)
    agent.handle_message = AsyncMock(return_value=replies)
    agent.handle_button = AsyncMock(return_value=button_reply)
    return agent


class TestSlack:
    def _adapter(self, agent):
        a = SlackAdapter(agent, "xoxb-x", "xapp-x")
        a.web = AsyncMock()
        a.bot_user_id = "UBOT"
        return a

    async def test_mention_in_channel(self):
        agent = fake_agent([Reply("all idle")])
        a = self._adapter(agent)
        await a.handle_request(
            "events_api",
            {
                "team_id": "T1",
                "event": {"type": "app_mention", "user": "U1", "text": "<@UBOT> status", "channel": "C1", "ts": "1.5"},
            },
        )
        m: IncomingMessage = agent.handle_message.call_args.args[0]
        assert (m.platform, m.user_id, m.channel_id, m.text, m.team_id, m.is_dm) == (
            "slack",
            "U1",
            "C1",
            "status",
            "T1",
            False,
        )
        a.web.chat_postMessage.assert_awaited_once_with(channel="C1", text="all idle", thread_ts="1.5")

    async def test_direct_message_and_ignored_events(self):
        agent = fake_agent([Reply("hi")])
        a = self._adapter(agent)
        await a.handle_request(
            "events_api",
            {
                "team_id": "T1",
                "event": {"type": "message", "channel_type": "im", "user": "U1", "text": "help", "channel": "D1"},
            },
        )
        m = agent.handle_message.call_args.args[0]
        assert m.is_dm and m.channel_id == "D1"
        a.web.chat_postMessage.assert_awaited_once_with(channel="D1", text="hi")
        agent.handle_message.reset_mock()
        for event in (
            {"type": "message", "channel_type": "channel", "user": "U1", "text": "status", "channel": "C1"},
            {"type": "app_mention", "bot_id": "B1", "user": "U2", "text": "status", "channel": "C1"},
            {"type": "message", "channel_type": "im", "subtype": "message_changed", "channel": "D1"},
            {"type": "app_mention", "user": "UBOT", "text": "x", "channel": "C1"},
        ):
            await a.handle_request("events_api", {"team_id": "T1", "event": event})
        agent.handle_message.assert_not_called()

    async def test_not_allowed_means_no_post(self):
        agent = fake_agent(None)
        a = self._adapter(agent)
        await a.handle_request(
            "events_api",
            {"team_id": "T9", "event": {"type": "app_mention", "user": "U1", "text": "x", "channel": "C9"}},
        )
        a.web.chat_postMessage.assert_not_called()

    async def test_confirmation_has_buttons(self):
        payload = reply_payload("C1", Reply("Please confirm: cancel", confirmation_id="abc"), "1.5")
        buttons = payload["blocks"][1]["elements"]
        assert [(b["action_id"], b["value"]) for b in buttons] == [(ACTION_CONFIRM, "abc"), (ACTION_DECLINE, "abc")]
        assert payload["thread_ts"] == "1.5"

    async def test_button_press_updates_message(self):
        agent = fake_agent(button_reply=Reply("Cancelled the print on X1C-1."))
        a = self._adapter(agent)
        await a.handle_request(
            "interactive",
            {
                "type": "block_actions",
                "user": {"id": "U1"},
                "team": {"id": "T1"},
                "channel": {"id": "C1"},
                "message": {"ts": "2.0"},
                "actions": [{"action_id": ACTION_CONFIRM, "value": "abc"}],
            },
        )
        agent.handle_button.assert_awaited_once_with("slack", "U1", "C1", "abc", True, team_id="T1", is_dm=False)
        a.web.chat_update.assert_awaited_once_with(
            channel="C1", ts="2.0", text="Cancelled the print on X1C-1.", blocks=[]
        )

    async def test_wrong_user_press_is_ephemeral(self):
        agent = fake_agent(button_reply=Reply("Only the person who asked can confirm or cancel this.", private=True))
        a = self._adapter(agent)
        await a.handle_request(
            "interactive",
            {
                "type": "block_actions",
                "user": {"id": "U2"},
                "team": {"id": "T1"},
                "channel": {"id": "C1"},
                "message": {"ts": "2.0"},
                "actions": [{"action_id": ACTION_DECLINE, "value": "abc"}],
            },
        )
        assert agent.handle_button.call_args.args[4] is False
        a.web.chat_postEphemeral.assert_awaited_once()
        a.web.chat_update.assert_not_called()

    async def test_socket_request_is_acked_first(self):
        agent = fake_agent([Reply("ok")])
        a = self._adapter(agent)
        client = SimpleNamespace(send_socket_mode_response=AsyncMock())
        req = SimpleNamespace(
            type="events_api",
            envelope_id="env-1",
            payload={
                "team_id": "T1",
                "event": {"type": "app_mention", "user": "U1", "text": "status", "channel": "C1"},
            },
        )
        await a._on_request(client, req)
        assert client.send_socket_mode_response.await_args.args[0].envelope_id == "env-1"
        a.web.chat_postMessage.assert_awaited_once()


class TestDiscord:
    def _adapter(self, agent):
        a = DiscordAdapter(agent, "token")
        self.bot = SimpleNamespace(id=1)
        a.client = SimpleNamespace(user=self.bot)
        return a

    def _message(self, content, mentions, guild=True, bot=False):
        return SimpleNamespace(
            author=SimpleNamespace(id=55, bot=bot, name="ann"),
            guild=SimpleNamespace(id=9) if guild else None,
            mentions=mentions,
            content=content,
            channel=SimpleNamespace(id=900, send=AsyncMock()),
        )

    async def test_mention_in_guild_channel(self):
        agent = fake_agent([Reply("all idle")])
        a = self._adapter(agent)
        message = self._message("<@1> status", [self.bot])
        await a.handle_message(message)
        m = agent.handle_message.call_args.args[0]
        assert (m.platform, m.user_id, m.channel_id, m.text, m.team_id, m.is_dm, m.user_name) == (
            "discord",
            "55",
            "900",
            "status",
            "9",
            False,
            "ann",
        )
        message.channel.send.assert_awaited_once_with("all idle")

    async def test_ignores_unmentioned_and_bots(self):
        agent = fake_agent([Reply("x")])
        a = self._adapter(agent)
        await a.handle_message(self._message("status", []))
        await a.handle_message(self._message("<@1> status", [self.bot], bot=True))
        agent.handle_message.assert_not_called()

    async def test_dm_needs_no_mention(self):
        agent = fake_agent([Reply("x")])
        a = self._adapter(agent)
        await a.handle_message(self._message("status", [], guild=False))
        m = agent.handle_message.call_args.args[0]
        assert m.is_dm and m.team_id is None

    async def test_confirmation_sends_buttons(self):
        agent = fake_agent([Reply("Please confirm", confirmation_id="abc")])
        a = self._adapter(agent)
        message = self._message("<@1> cancel x1c", [self.bot])
        await a.handle_message(message)
        view = message.channel.send.await_args.kwargs["view"]
        labels = sorted(item.label for item in view.children)
        assert labels == ["Cancel", "Confirm"]
        assert view.timeout == 60

    async def test_button_handling(self):
        agent = fake_agent(button_reply=Reply("Cancelled the print on X1C-1."))
        a = self._adapter(agent)
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=55),
            channel_id=900,
            guild=SimpleNamespace(id=9),
            response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock(), defer=AsyncMock()),
        )
        await a.handle_button(interaction, "abc", True)
        agent.handle_button.assert_awaited_once_with("discord", "55", "900", "abc", True, team_id="9", is_dm=False)
        interaction.response.edit_message.assert_awaited_once_with(content="Cancelled the print on X1C-1.", view=None)

        agent.handle_button.return_value = Reply("Only the person who asked can confirm or cancel this.", private=True)
        interaction.response.edit_message.reset_mock()
        await a.handle_button(interaction, "abc", False)
        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.await_args.kwargs == {"ephemeral": True}
        interaction.response.edit_message.assert_not_called()


class TestManager:
    async def test_nothing_starts_unless_configured(self):
        m = ChatAgentManager()
        await m.apply(ChatAgentConfig(enabled=True, slack_enabled=True))  # no tokens, no channels
        assert m.agent is None and m.adapters == {}
        await m.apply(
            ChatAgentConfig(
                enabled=False,
                slack_enabled=True,
                slack_bot_token="a",
                slack_app_token="b",
                slack_allowed_channels=["C1"],
            )
        )
        assert m.agent is None

    async def test_start_failure_reports_class_only(self, monkeypatch):
        from backend.app.services.chat_agent import slack_adapter

        async def boom(self):
            raise RuntimeError("invalid_auth xoxb-secret-token")

        monkeypatch.setattr(slack_adapter.SlackAdapter, "start", boom)
        m = ChatAgentManager()
        cfg = ChatAgentConfig(
            enabled=True,
            slack_enabled=True,
            slack_bot_token="xoxb-secret-token",
            slack_app_token="xapp-1",
            slack_allowed_channels=["C1"],
        )
        await m.apply(cfg)
        assert m.status()["slack"] == {"running": False, "error": "RuntimeError"}
        assert "secret" not in str(m.status())
        assert m.agent is not None

    async def test_start_and_stop(self, monkeypatch):
        from backend.app.services.chat_agent import discord_adapter

        started, stopped = [], []

        async def start(self):
            started.append(self)

        async def stop(self):
            stopped.append(self)

        monkeypatch.setattr(discord_adapter.DiscordAdapter, "start", start)
        monkeypatch.setattr(discord_adapter.DiscordAdapter, "stop", stop)
        m = ChatAgentManager()
        cfg = ChatAgentConfig(enabled=True, discord_enabled=True, discord_bot_token="t", discord_allowed_channels=["1"])
        await m.apply(cfg)
        assert m.status()["discord"] == {"running": True, "error": None}
        assert m.status()["slack"] == {"running": False, "error": None}
        agent = m.agent
        await m.apply(cfg)  # reload keeps the agent (and its open confirmations)
        assert m.agent is agent and len(started) == 2 and len(stopped) == 1
        await m.stop()
        assert len(stopped) == 2 and m.adapters == {}
