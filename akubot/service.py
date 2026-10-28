from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass

from .config import Persona
from .memory import Memory, SessionKey
from .model import ModelError, system_prompt
from .privacy import clean_input, clean_output
from .storage import Store

logger = logging.getLogger("akubot")


class Notice(RuntimeError):
    """A fixed, safe explanation that can be shown in Discord."""


@dataclass(frozen=True)
class Request:
    guild_id: int
    channel_id: int
    user_id: int
    text: str

    @property
    def key(self) -> SessionKey:
        return self.guild_id, self.channel_id, self.user_id


class Gate:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.pending = 0
        self.users: dict[int, float] = {}
        self.guilds: dict[int, deque[float]] = {}

    def enter(self, request: Request) -> None:
        now = self.clock()
        self.users = {user: last for user, last in self.users.items() if now - last < 15}
        recent = self.guilds.setdefault(request.guild_id, deque())
        while recent and now - recent[0] >= 60:
            recent.popleft()
        if request.user_id in self.users:
            raise Notice("Give me a few seconds before pinging me again.")
        if len(recent) >= 12:
            raise Notice("I've reached this server's limit for the minute. Try again shortly.")
        if self.pending >= 4:
            raise Notice("I'm handling a few replies already. Try again shortly.")
        self.users[request.user_id] = now
        recent.append(now)
        self.pending += 1

    def leave(self) -> None:
        self.pending -= 1


class Responder:
    def __init__(self, store: Store, model, persona: Persona, memory: Memory | None = None):
        self.store, self.model, self.persona = store, model, persona
        self.memory = memory or Memory()
        self.gate = Gate()
        self.slot = asyncio.Semaphore(1)
        # In-flight replies carry a revision so forget/pause also cancel stale work.
        self.revisions: dict[SessionKey, int] = {}
        self.inflight: dict[SessionKey, int] = {}
        self.guild_revisions: dict[int, int] = {}

    def forget(self, key: SessionKey) -> None:
        self.memory.forget(key)
        if key in self.inflight:
            self.revisions[key] = self.revisions.get(key, 0) + 1

    def pause(self, guild_id: int, paused: bool) -> None:
        self.store.set_paused(guild_id, paused)
        self.guild_revisions[guild_id] = self.guild_revisions.get(guild_id, 0) + 1
        self.memory.forget_guild(guild_id)

    async def reply(self, request: Request) -> str:
        if self.store.is_paused(request.guild_id):
            raise Notice("I'm paused in this server.")
        prompt = clean_input(request.text)
        if not prompt:
            raise Notice("Mention me with a message, like `@aku what do you think?`.")
        if len(prompt) > 1500:
            raise Notice("Keep the message under 1,500 characters so I can answer locally.")
        self.gate.enter(request)
        self.inflight[request.key] = self.inflight.get(request.key, 0) + 1
        revision = (self.revisions.get(request.key, 0), self.guild_revisions.get(request.guild_id, 0))
        try:
            async with self.slot:
                if self.store.is_paused(request.guild_id):
                    raise Notice("I'm paused in this server.")
                current = (self.revisions.get(request.key, 0), self.guild_revisions.get(request.guild_id, 0))
                if current != revision:
                    raise Notice("That reply was cancelled because the conversation was cleared or paused.")
                messages = [{"role": "system", "content": system_prompt(self.persona, self.store.examples(prompt))}]
                messages.extend(self.memory.get(request.key))
                messages.append({"role": "user", "content": prompt})
                try:
                    answer = clean_output(await self.model.generate(messages))
                except ModelError:
                    logger.warning("Local model unavailable; no request content logged.")
                    raise Notice("My local model isn't ready. The owner can run the setup check.") from None
                if not answer:
                    raise Notice("I couldn't produce a reply. Try asking another way.")
                current = (self.revisions.get(request.key, 0), self.guild_revisions.get(request.guild_id, 0))
                if current != revision or self.store.is_paused(request.guild_id):
                    raise Notice("That reply was cancelled because the conversation was cleared or paused.")
                self.memory.append(request.key, prompt, answer)
                return answer
        finally:
            self.gate.leave()
            self.inflight[request.key] -= 1
            if not self.inflight[request.key]:
                del self.inflight[request.key]
                self.revisions.pop(request.key, None)

