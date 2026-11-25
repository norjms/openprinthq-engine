"""Discord transport: a gateway bot (discord.py), no public URL needed.

The bot answers messages that mention it in allowed channels, and direct messages
when DMs are allowed. Discord delivers the text of those two kinds of message
without the privileged Message Content intent, so the bot asks only for the
guilds, guild messages and direct messages intents. Confirmations use buttons
(message components), which also arrive over the gateway.

Discord setup is in docs/chat-agent.md. Use a Discord application of its own: one
bot token cannot hold two gateway sessions.
"""

from __future__ import annotations

import asyncio
import logging
import re

from backend.app.services.chat_agent.agent import ChatAgent, IncomingMessage, Reply

logger = logging.getLogger(__name__)

_MENTION = re.compile(r"<@!?\d+>")
MAX_LEN = 1900  # Discord's limit is 2000 characters per message


class DiscordAdapter:
    platform = "discord"

    def __init__(self, agent: ChatAgent, bot_token: str):
        self.agent = agent
        self._token = bot_token
        self.client = None
        self._task: asyncio.Task | None = None
        self.last_error: str | None = None

    async def start(self) -> None:
        import discord

        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.dm_messages = True
        self.client = discord.Client(intents=intents)
        adapter = self

        @self.client.event
        async def on_message(message):  # noqa: ANN001 - discord.py callback
            try:
                await adapter.handle_message(message)
            except Exception:  # noqa: BLE001 - one bad message must not stop the bot
                logger.exception("chat agent: Discord message failed")

        @self.client.event
        async def on_ready():
            logger.info("chat agent: Discord connected as %s", adapter.client.user)

        self._task = asyncio.create_task(self._run(), name="chat-agent-discord")

    async def _run(self) -> None:
        try:
            await self.client.start(self._token)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - surfaced in the status API, token never logged
            self.last_error = type(e).__name__
            logger.warning("chat agent: Discord stopped: %s", type(e).__name__)

    async def stop(self) -> None:
        if self.client is not None:
            try:
                await self.client.close()
            except Exception as e:  # noqa: BLE001 - shutting down anyway
                logger.debug("chat agent: Discord close failed: %s", e)
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.client = None
        self._task = None

    def _bot_user(self):
        return getattr(self.client, "user", None)

    async def handle_message(self, message) -> None:  # noqa: ANN001 - discord.Message or a stand-in
        author = message.author
        if getattr(author, "bot", False):
            return
        me = self._bot_user()
        guild = getattr(message, "guild", None)
        is_dm = guild is None
        if not is_dm and (me is None or me not in (message.mentions or [])):
            return
        text = _MENTION.sub(" ", message.content or "").strip()
        msg = IncomingMessage(
            platform="discord",
            user_id=str(author.id),
            channel_id=str(message.channel.id),
            text=text,
            team_id=None if is_dm else str(guild.id),
            is_dm=is_dm,
            user_name=getattr(author, "name", None),
        )
        replies = await self.agent.handle_message(msg)
        for reply in replies or []:
            view = self._confirm_view(reply) if reply.confirmation_id else None
            kwargs = {"view": view} if view is not None else {}
            await message.channel.send(reply.text[:MAX_LEN], **kwargs)

    def _confirm_view(self, reply: Reply):
        import discord

        adapter = self
        timeout = float(self.agent.config.confirm_timeout_seconds)
        conf_id = reply.confirmation_id

        class ConfirmView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=timeout)

            @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
            async def confirm(self, interaction, button):  # noqa: ANN001
                await adapter.handle_button(interaction, conf_id, True)

            @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
            async def decline(self, interaction, button):  # noqa: ANN001
                await adapter.handle_button(interaction, conf_id, False)

        return ConfirmView()

    async def handle_button(self, interaction, confirmation_id: str, accept: bool) -> None:  # noqa: ANN001
        guild = getattr(interaction, "guild", None)
        reply = await self.agent.handle_button(
            "discord",
            str(interaction.user.id),
            str(interaction.channel_id),
            confirmation_id,
            accept,
            team_id=None if guild is None else str(guild.id),
            is_dm=guild is None,
        )
        if reply is None:
            await interaction.response.defer()
            return
        if reply.private:
            # Only the presser sees this; the buttons stay for the person who asked.
            await interaction.response.send_message(reply.text, ephemeral=True)
            return
        await interaction.response.edit_message(content=reply.text[:MAX_LEN], view=None)
