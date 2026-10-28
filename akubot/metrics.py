from __future__ import annotations

from collections import Counter, deque
from datetime import datetime, timezone
import time
import math


class Metrics:
    """Bounded timing/counter metadata. Never contains messages or identities."""

    def __init__(self, clock=time.monotonic):
        self.clock, self.started = clock, clock()
        self.sequence = 0
        self.running: dict[int, dict] = {}
        self.history: deque[dict] = deque(maxlen=100)
        self.counts: Counter[str] = Counter()

    def begin(self, source: str = "discord") -> int:
        self.sequence += 1
        self.running[self.sequence] = {"started": self.clock(), "at": datetime.now(timezone.utc).isoformat(), "source": source, "state": "queued"}
        return self.sequence

    def active(self, identifier: int) -> float:
        event = self.running[identifier]
        event["state"] = "running"
        event["queue_seconds"] = self.clock() - event["started"]
        return event["queue_seconds"]

    def finish(self, identifier: int, outcome: str, model: dict | None = None) -> None:
        event = self.running.pop(identifier)
        data = {"id": identifier, "at": event["at"], "source": event["source"], "outcome": outcome, "queue_seconds": round(event.get("queue_seconds", 0), 4), "total_seconds": round(self.clock() - event["started"], 4)}
        for key in ("model_seconds", "load_seconds", "generation_seconds", "input_tokens", "output_tokens", "tokens_per_second"):
            value = (model or {}).get(key)
            if type(value) in {int, float} and math.isfinite(value) and value >= 0:
                data[key] = round(value, 4)
        self.history.appendleft(data)
        self.counts[outcome if event["source"] == "discord" else event["source"] + "_" + outcome] += 1

    def snapshot(self) -> dict:
        successful = [event for event in self.history if event["outcome"] == "reply" and event["source"] == "discord"]
        ordered = sorted(event["total_seconds"] for event in successful)
        return {
            "uptime_seconds": round(self.clock() - self.started),
            "counts": dict(self.counts),
            "active": sum(event["state"] == "running" for event in self.running.values()),
            "queued": sum(event["state"] == "queued" for event in self.running.values()),
            "last_reply_seconds": successful[0]["total_seconds"] if successful else None,
            "typical_reply_seconds": ordered[len(ordered) // 2] if ordered else None,
            "recent": list(self.history),
        }
