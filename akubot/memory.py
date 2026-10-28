from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable


SessionKey = tuple[int, int, int]


@dataclass
class Session:
    updated: float
    turns: list[dict[str, str]] = field(default_factory=list)


class Memory:
    def __init__(self, ttl: int = 900, max_sessions: int = 256, max_turns: int = 2, clock: Callable[[], float] = time.monotonic):
        self.ttl, self.max_sessions, self.max_turns, self.clock = ttl, max_sessions, max_turns, clock
        self.sessions: OrderedDict[SessionKey, Session] = OrderedDict()

    def purge(self) -> None:
        now = self.clock()
        for key in list(self.sessions):
            if now - self.sessions[key].updated >= self.ttl:
                del self.sessions[key]

    def get(self, key: SessionKey) -> list[dict[str, str]]:
        self.purge()
        session = self.sessions.get(key)
        return [dict(turn) for turn in session.turns[-2 * self.max_turns:]] if session and self.max_turns > 0 else []

    def append(self, key: SessionKey, prompt: str, answer: str) -> None:
        self.purge()
        if self.max_turns <= 0:
            self.forget(key)
            return
        session = self.sessions.pop(key, Session(self.clock()))
        session.updated = self.clock()
        session.turns.extend([{"role": "user", "content": prompt[:512]}, {"role": "assistant", "content": answer[:512]}])
        session.turns = session.turns[-2 * self.max_turns:]
        self.sessions[key] = session
        while len(self.sessions) > self.max_sessions:
            self.sessions.popitem(last=False)

    def forget(self, key: SessionKey) -> None:
        self.sessions.pop(key, None)

    def forget_guild(self, guild_id: int) -> None:
        for key in list(self.sessions):
            if key[0] == guild_id:
                del self.sessions[key]

