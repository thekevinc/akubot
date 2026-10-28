from __future__ import annotations

import asyncio
import contextlib
import logging
import re

import aiohttp
import discord
from discord import app_commands

from .config import Persona, Settings
from .model import Ollama
from .service import Notice, Request, Responder
from .storage import Store

logger = logging.getLogger("akubot")


def in_scope(settings: Settings, guild_id: int | None, channel_id: int | None, owner_control: bool = False) -> bool:
    return guild_id in settings.guild_ids and (
        owner_control or not settings.channel_ids or channel_id in settings.channel_ids
    )


class AkuClient(discord.Client):
    def __init__(self, settings: Settings, store: Store, persona: Persona):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        # Mentioned messages are readable without broad Message Content access.
        super().__init__(
            intents=intents, max_messages=None,
            member_cache_flags=discord.MemberCacheFlags.none(),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.settings, self.store, self.persona = settings, store, persona
        self.model_session: aiohttp.ClientSession | None = None
        self.responder: Responder | None = None
        self.cleanup_task: asyncio.Task | None = None
        self.tree = app_commands.CommandTree(self)
        self._register_commands()

    def _register_commands(self) -> None:
        @self.tree.command(name="aku_forget", description="Clear your temporary conversation with Aku in this channel")
        async def forget(interaction: discord.Interaction):
            if not await self._authorize(interaction):
                return
            assert self.responder is not None
            self.responder.forget((interaction.guild_id, interaction.channel_id, interaction.user.id))
            await interaction.response.send_message("Your temporary conversation in this channel has been cleared.", ephemeral=True)

        @self.tree.command(name="aku_pause", description="Owner only: pause Aku and clear temporary memory in this server")
        async def pause(interaction: discord.Interaction):
            if not await self._authorize(interaction, owner=True):
                return
            assert self.responder is not None
            self.responder.pause(interaction.guild_id, True)
            await interaction.response.send_message("Aku is paused in this server. Temporary conversations were cleared.", ephemeral=True)

        @self.tree.command(name="aku_resume", description="Owner only: resume Aku in this server")
        async def resume(interaction: discord.Interaction):
            if not await self._authorize(interaction, owner=True):
                return
            assert self.responder is not None
            self.responder.pause(interaction.guild_id, False)
            await interaction.response.send_message("Aku can respond to mentions in this server again.", ephemeral=True)

        @self.tree.command(name="aku_status", description="Owner only: show Aku's local model and personality status")
        async def status(interaction: discord.Interaction):
            if not await self._authorize(interaction, owner=True):
                return
            state = "paused" if self.store.is_paused(interaction.guild_id) else "responding to mentions"
            await interaction.response.send_message(
                f"Aku is {state}. Local model: `{self.settings.model}`. "
                f"Reviewed style examples: {self.store.count()}. "
                "Conversation memory stays in RAM for up to 15 minutes, with expired entries cleared every minute.",
                ephemeral=True,
            )

        self.tree.on_error = self._command_error

    async def _authorize(self, interaction: discord.Interaction, owner: bool = False) -> bool:
        allowed = in_scope(self.settings, interaction.guild_id, interaction.channel_id, owner_control=owner)
        allowed = allowed and (not owner or interaction.user.id == self.settings.owner_id)
        if not allowed:
            await interaction.response.send_message("This command is not available to you here.", ephemeral=True)
        return allowed

    async def setup_hook(self) -> None:
        self.model_session = aiohttp.ClientSession(trust_env=False, connector=aiohttp.TCPConnector(limit=4))
        self.responder = Responder(self.store, Ollama(self.model_session, self.settings.model_url, self.settings.model), self.persona)
        # Server-scoped registration avoids enabling commands in other servers or DMs.
        for guild_id in self.settings.guild_ids:
            guild = discord.Object(id=guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        self.cleanup_task = asyncio.create_task(self._purge_memory())

    async def _purge_memory(self) -> None:
        while True:
            await asyncio.sleep(60)
            assert self.responder is not None
            self.responder.memory.purge()

    async def on_ready(self) -> None:
        logger.info("Connected. Mention replies configured for %d server(s).", len(self.settings.guild_ids))
        if not self.store.count():
            logger.info("No style examples loaded yet. Edit the persona or import reviewed examples to personalize Aku.")

    async def on_message(self, message: discord.Message) -> None:
        if (
            self.user is None or self.responder is None
            or message.author.bot or message.webhook_id is not None
            or not in_scope(self.settings, message.guild.id if message.guild else None, message.channel.id)
        ):
            return
        mention = re.compile(rf"<@!?{self.user.id}>")
        # Require an explicit mention in the text, including on follow-up replies.
        if not mention.search(message.content):
            return
        request = Request(message.guild.id, message.channel.id, message.author.id, mention.sub("", message.content).strip())
        try:
            async with message.channel.typing():
                answer = await asyncio.wait_for(self.responder.reply(request), timeout=240)
            await self._send(message, answer)
        except Notice as error:
            await self._send(message, str(error))
        except asyncio.TimeoutError:
            await self._send(message, "My local model took too long. Try again shortly.")
        except discord.HTTPException:
            logger.warning("Discord could not accept a reply; no message content logged.")
        except Exception:
            logger.error("A reply failed; no message content or exception details logged.")
            await self._send(message, "I couldn't answer that message. The owner can check my local setup.")

    async def _send(self, message: discord.Message, content: str) -> None:
        try:
            await message.reply(content, mention_author=False, allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)
        except discord.HTTPException:
            logger.warning("Discord could not accept a reply; no message content logged.")

    async def _command_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        logger.error("A control command failed; no interaction content logged.")
        if not interaction.response.is_done():
            await interaction.response.send_message("The command could not complete. Ask the owner to check the local setup.", ephemeral=True)

    async def on_error(self, event_method: str, *args, **kwargs) -> None:
        # discord.py's default handler would print a traceback with event context.
        logger.error("A Discord event failed; no event content logged.")

    async def close(self) -> None:
        if self.cleanup_task is not None:
            self.cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.cleanup_task
        if self.model_session is not None:
            await self.model_session.close()
        await super().close()

