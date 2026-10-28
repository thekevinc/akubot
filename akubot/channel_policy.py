from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path


class ChannelPolicy:
    """Fail closed unless recent Discord metadata explicitly allows the channel."""

    def __init__(self, data: dict, now: datetime | None = None):
        try:
            if data.get("version") != 1 or data.get("source") != "discord-api":
                raise ValueError
            verified = datetime.fromisoformat(data["verified_at"])
            if verified.tzinfo is None:
                raise ValueError
            age = ((now or datetime.now(timezone.utc)) - verified).total_seconds()
            if not -300 <= age <= 86_400:
                raise ValueError
            self.channels = data["channels"]
            if not isinstance(self.channels, dict):
                raise ValueError
        except (AttributeError, KeyError, TypeError, ValueError):
            raise ValueError("Channel metadata is missing or expired. Refresh it before importing messages.") from None

    @classmethod
    def load(cls, path: Path) -> "ChannelPolicy":
        try:
            return cls(json.loads(path.read_text(encoding="utf-8")))
        except OSError:
            raise ValueError("Channel metadata is missing. Run scripts/connect_discord.py --refresh first.") from None

    def allows(self, channel_id: str) -> bool:
        entry = self.channels.get(channel_id)
        return isinstance(entry, dict) and entry.get("nsfw") is False


def snapshot(guild_id: str, channels: list[dict]) -> dict:
    by_id = {str(channel.get("id")): channel for channel in channels if isinstance(channel, dict)}

    def blocked(channel: dict, visited: set[str]) -> bool:
        channel_id = str(channel.get("id"))
        if channel_id in visited:
            return True
        visited.add(channel_id)
        if channel.get("nsfw") is True or "nsfw" in str(channel.get("name", "")).casefold():
            return True
        parent = by_id.get(str(channel.get("parent_id")))
        return blocked(parent, visited) if parent else False

    result = {}
    for channel_id, channel in by_id.items():
        if not channel_id.isdecimal():
            continue
        # Unsupported/deleted channels and threads without verified parent data
        # stay unknown. Categories are only used to propagate exclusion.
        supported = channel.get("type") in {0, 2, 5, 13, 15, 16}
        value = True if blocked(channel, set()) else (False if supported and channel.get("nsfw") is False else None)
        result[channel_id] = {"guild_id": guild_id, "nsfw": value}
    return {"version": 1, "source": "discord-api", "verified_at": datetime.now(timezone.utc).isoformat(), "channels": result}

