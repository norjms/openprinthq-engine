"""Slack transport: Socket Mode, so the engine needs no public URL.

The app connects out to Slack with an app-level token (xapp-, scope
``connections:write``) and posts with the bot token (xoxb-). It answers:

- ``app_mention`` events in channels (someone writes @bot ...),
- ``message`` events in direct messages (``channel_type`` "im"), when DMs are allowed,
- ``block_actions`` from the Confirm / Cancel buttons it posts.

Slack setup is in docs/chat-agent.md.
"""

from __future__ import annotations

import logging
import re

from backend.app.services.chat_agent.agent import ChatAgent, IncomingMessage, Reply

logger = logging.getLogger(__name__)

ACTION_CONFIRM = "ophq_chat_confirm"
ACTION_DECLINE = "ophq_chat_decline"
_MENTION = re.compile(r"<@[A-Z0-9]+>")


def reply_payload(channel: str, reply: Reply, thread_ts: str | None = None) -> dict:
    """chat.postMessage arguments for one reply (buttons when it needs confirming)."""
    payload: dict = {"channel": channel, "text": reply.text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    if reply.confirmation_id:
        payload["blocks"] = [
            {"type": "section", "text": {"type": "plain_text", "text": reply.text[:2900]}},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "style": "danger",
                        "text": {"type": "plain_text", "text": "Confirm"},
                        "action_id": ACTION_CONFIRM,
                        "value": reply.confirmation_id,
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Cancel"},
                        "action_id": ACTION_DECLINE,
                        "value": reply.confirmation_id,
                    },
                ],
            },
        ]
    return payload


class SlackAdapter:
    platform = "slack"

    def __init__(self, agent: ChatAgent, bot_token: str, app_token: str):
        self.agent = agent
        self._bot_token = bot_token
        self._app_token = app_token
        self.client = None  # SocketModeClient once started
        self.web = None  # AsyncWebClient
        self.bot_user_id: str | None = None
        self.last_error: str | None = None

    async def start(self) -> None:
        from slack_sdk.socket_mode.aiohttp import SocketModeClient
        from slack_sdk.web.async_client import AsyncWebClient

        self.web = AsyncWebClient(token=self._bot_token)
        auth = await self.web.auth_test()
        self.bot_user_id = auth.get("user_id")
        self.client = SocketModeClient(app_token=self._app_token, web_client=self.web)
        self.client.socket_mode_request_listeners.append(self._on_request)
        await self.client.connect()
        logger.info("chat agent: Slack connected (Socket Mode)")

    async def stop(self) -> None:
        if self.client is not None:
            try:
                await self.client.close()
            except Exception as e:  # noqa: BLE001 - shutting down anyway
                logger.debug("chat agent: Slack close failed: %s", e)
        self.client = None

    async def _on_request(self, client, req) -> None:
        from slack_sdk.socket_mode.response import SocketModeResponse

        # Acknowledge first: Slack retries anything not acked within 3 seconds.
        await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        try:
            await self.handle_request(req.type, req.payload or {})
        except Exception:  # noqa: BLE001 - one bad event must not stop the bot
            logger.exception("chat agent: Slack event failed")

    async def handle_request(self, req_type: str, payload: dict) -> None:
        if req_type == "events_api":
            await self._handle_event(payload)
        elif req_type == "interactive":
            await self._handle_interactive(payload)

    async def _handle_event(self, payload: dict) -> None:
        event = payload.get("event") or {}
        etype = event.get("type")
        if event.get("bot_id") or event.get("subtype"):
            return  # bots (including this one), edits, joins
        if etype == "app_mention":
            is_dm = False
        elif etype == "message" and event.get("channel_type") == "im":
            is_dm = True
        else:
            return
        user = event.get("user")
        if not user or user == self.bot_user_id:
            return
        text = _MENTION.sub(" ", event.get("text") or "").strip()
        msg = IncomingMessage(
            platform="slack",
            user_id=user,
            channel_id=event.get("channel") or "",
            text=text,
            team_id=payload.get("team_id") or event.get("team"),
            is_dm=is_dm,
        )
        replies = await self.agent.handle_message(msg)
        thread_ts = None if is_dm else (event.get("thread_ts") or event.get("ts"))
        for reply in replies or []:
            await self.web.chat_postMessage(**reply_payload(msg.channel_id, reply, thread_ts))

    async def _handle_interactive(self, payload: dict) -> None:
        if payload.get("type") != "block_actions":
            return
        actions = payload.get("actions") or []
        if not actions or actions[0].get("action_id") not in (ACTION_CONFIRM, ACTION_DECLINE):
            return
        action = actions[0]
        channel = (payload.get("channel") or {}).get("id") or (payload.get("container") or {}).get("channel_id")
        user = (payload.get("user") or {}).get("id")
        team = (payload.get("team") or {}).get("id") or (payload.get("user") or {}).get("team_id")
        is_dm = str(channel or "").startswith("D")
        reply = await self.agent.handle_button(
            "slack",
            user,
            channel,
            action.get("value") or "",
            action.get("action_id") == ACTION_CONFIRM,
            team_id=team,
            is_dm=is_dm,
        )
        if reply is None:
            return
        message = payload.get("message") or {}
        thread_ts = message.get("thread_ts")
        if reply.private:
            # Tell just the person who pressed, leave the buttons for the asker.
            await self.web.chat_postEphemeral(channel=channel, user=user, text=reply.text)
            return
        if message.get("ts"):
            # Replace the question so the buttons cannot be pressed twice.
            await self.web.chat_update(channel=channel, ts=message["ts"], text=reply.text, blocks=[])
        else:
            await self.web.chat_postMessage(**reply_payload(channel, reply, thread_ts))
