from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from dataclasses import dataclass

from .config import Persona
from .control import Control
from .memory import Memory, SessionKey
from .metrics import Metrics
from .model import ModelError, system_prompt
from .privacy import clean_input, clean_output
from .storage import Store

logger = logging.getLogger("akubot")


class Notice(RuntimeError):
    def __init__(self, message: str, code: str = "notice"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Request:
    guild_id: int
    channel_id: int
    user_id: int
    text: str
    source: str = "discord"

    @property
    def key(self) -> SessionKey:
        return self.guild_id, self.channel_id, self.user_id


class Gate:
    def __init__(self, clock=time.monotonic, control: Control | None = None):
        self.clock, self.control = clock, control
        self.pending = 0
        self.users: dict[int, float] = {}
        self.guilds: dict[int, deque[float]] = {}

    def enter(self, request: Request) -> None:
        now = self.clock()
        cooldown = self.control.tuning.cooldown_seconds if self.control else 2
        limit = self.control.tuning.requests_per_minute if self.control else 30
        self.users = {user: last for user, last in self.users.items() if now - last < 30}
        recent = self.guilds.setdefault(request.guild_id, deque())
        while recent and now - recent[0] >= 60:
            recent.popleft()
        if request.user_id in self.users and now - self.users[request.user_id] < cooldown:
            seconds = math.ceil(cooldown - (now - self.users[request.user_id]))
            raise Notice(f"You can ping me again in {seconds} second{'s' if seconds != 1 else ''}.", "cooldown")
        if len(recent) >= limit:
            raise Notice("I've reached this server's limit for the minute. Try again shortly.", "server_limit")
        if self.pending >= 4:
            raise Notice("I'm handling a few replies already. Try again shortly.", "busy")
        self.users[request.user_id] = now
        recent.append(now)
        self.pending += 1

    def leave(self) -> None:
        self.pending -= 1


class Responder:
    def __init__(self, store: Store, model, persona: Persona, memory: Memory | None = None, control: Control | None = None, metrics: Metrics | None = None):
        self.store, self.model, self.persona, self.control = store, model, persona, control
        self.memory = memory or Memory()
        self.metrics = metrics or Metrics()
        self.gate = Gate(control=control)
        self.slot = asyncio.Semaphore(1)
        self.revisions: dict[SessionKey, int] = {}
        self.inflight: dict[SessionKey, int] = {}
        self.guild_revisions: dict[int, int] = {}
        self.apply_tuning()

    def apply_tuning(self) -> None:
        if self.control:
            self.memory.ttl = self.control.tuning.memory_minutes * 60
            self.memory.max_turns = self.control.tuning.memory_turns
            if self.memory.max_turns == 0:
                self.memory.sessions.clear()
            self.memory.purge()

    def forget(self, key: SessionKey) -> None:
        self.memory.forget(key)
        if key in self.inflight:
            self.revisions[key] = self.revisions.get(key, 0) + 1

    def clear_guild(self, guild_id: int) -> None:
        self.guild_revisions[guild_id] = self.guild_revisions.get(guild_id, 0) + 1
        self.memory.forget_guild(guild_id)

    def pause(self, guild_id: int, paused: bool) -> None:
        self.store.set_paused(guild_id, paused)
        self.clear_guild(guild_id)

    async def reply(self, request: Request) -> str:
        identifier = self.metrics.begin(request.source)
        outcome, reserved, registered, timing = "error", False, False, None
        try:
            if self.store.is_paused(request.guild_id):
                raise Notice("I'm paused in this server.", "paused")
            prompt = clean_input(request.text)
            if not prompt:
                raise Notice("Mention me with a message, like `@aku what do you think?`.", "empty")
            if len(prompt) > 1500:
                raise Notice("Keep the message under 1,500 characters so I can answer locally.", "too_long")
            self.gate.enter(request)
            reserved = True
            self.inflight[request.key] = self.inflight.get(request.key, 0) + 1
            registered = True
            revision = (self.revisions.get(request.key, 0), self.guild_revisions.get(request.guild_id, 0))
            async with self.slot:
                self.metrics.active(identifier)
                if self.store.is_paused(request.guild_id):
                    raise Notice("I'm paused in this server.", "paused")
                current = (self.revisions.get(request.key, 0), self.guild_revisions.get(request.guild_id, 0))
                if current != revision:
                    raise Notice("That reply was cancelled because the conversation was cleared or paused.", "cancelled")
                persona = self.control.persona if self.control else self.persona
                messages = [{"role": "system", "content": system_prompt(persona, self.store.examples(prompt))}]
                if request.source == "discord":
                    messages.extend(self.memory.get(request.key))
                messages.append({"role": "user", "content": prompt})
                try:
                    answer = clean_output(await self.model.generate(messages))
                    timing = getattr(self.model, "last_metrics", None)
                except ModelError:
                    logger.warning("Local model unavailable; no request content logged.")
                    raise Notice("My local model isn't ready. The owner can run the setup check.", "model_error") from None
                if not answer:
                    raise Notice("I couldn't produce a reply. Try asking another way.", "empty_reply")
                current = (self.revisions.get(request.key, 0), self.guild_revisions.get(request.guild_id, 0))
                if current != revision or self.store.is_paused(request.guild_id):
                    raise Notice("That reply was cancelled because the conversation was cleared or paused.", "cancelled")
                if request.source == "discord":
                    self.memory.append(request.key, prompt, answer)
                outcome = "reply"
                return answer
        except Notice as error:
            outcome = error.code
            raise
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            if reserved:
                self.gate.leave()
            if registered:
                self.inflight[request.key] -= 1
                if not self.inflight[request.key]:
                    del self.inflight[request.key]
                    self.revisions.pop(request.key, None)
            self.metrics.finish(identifier, outcome, timing)
