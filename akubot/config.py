from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    pass


def ids(value: str, field: str, required: bool = False) -> frozenset[int]:
    try:
        result = frozenset(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError:
        raise ConfigurationError(f"{field} must contain numeric Discord IDs.") from None
    if any(item <= 0 for item in result) or (required and not result):
        raise ConfigurationError(f"{field} needs at least one positive Discord ID.")
    return result


def local_model_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "http"
            and parsed.hostname in {"ollama", "localhost", "127.0.0.1", "::1", "host.docker.internal"}
            and parsed.port is not None
            and not parsed.username
            and not parsed.password
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ConfigurationError("OLLAMA_URL must point to the local Ollama service with an explicit port.")
    return value.rstrip("/")


@dataclass(frozen=True)
class Persona:
    name: str
    description: str
    style_rules: tuple[str, ...]
    public_facts: tuple[str, ...]

    @classmethod
    def load(cls, path: Path) -> "Persona":
        try:
            if path.stat().st_size > 32_768:
                raise ValueError
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls.from_dict(data)
        except (OSError, ValueError, TypeError, KeyError):
            raise ConfigurationError("Check config/persona.json: expected a name, description, style_rules, and public_facts.") from None

    @classmethod
    def from_dict(cls, data: dict) -> "Persona":
        try:
            if not isinstance(data, dict) or set(data) != {"name", "description", "style_rules", "public_facts"}:
                raise ValueError
            for field in ("style_rules", "public_facts"):
                if not isinstance(data.get(field), list) or not all(
                    isinstance(item, str) and len(item) <= 1000 for item in data[field]
                ) or len(data[field]) > 30:
                    raise ValueError
            if not all(isinstance(data.get(key), str) and 0 < len(data[key]) <= 1000 for key in ("name", "description")):
                raise ValueError
            return cls(data["name"], data["description"], tuple(data["style_rules"]), tuple(data["public_facts"]))
        except (ValueError, TypeError, KeyError):
            raise ConfigurationError("Use a name, description, style rules, and public facts within the displayed limits.") from None


@dataclass(frozen=True)
class Settings:
    owner_id: int
    guild_ids: frozenset[int]
    channel_ids: frozenset[int]
    token_file: Path
    data_dir: Path
    persona_file: Path
    model_url: str
    model: str
    dashboard_enabled: bool = False
    dashboard_port: int = 8787

    @classmethod
    def from_env(cls) -> "Settings":
        owner = ids(os.getenv("DISCORD_OWNER_ID", ""), "DISCORD_OWNER_ID", required=True)
        if len(owner) != 1:
            raise ConfigurationError("DISCORD_OWNER_ID must contain exactly one ID.")
        model = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
        if not model or len(model) > 150 or "cloud" in model.lower() or any(char.isspace() for char in model):
            raise ConfigurationError("Choose a local Ollama model, such as llama3.2:3b.")
        try:
            dashboard_port = int(os.getenv("DASHBOARD_PORT", "8787"))
            if not 1024 <= dashboard_port <= 65535:
                raise ValueError
        except ValueError:
            raise ConfigurationError("DASHBOARD_PORT must be between 1024 and 65535.") from None
        return cls(
            next(iter(owner)),
            ids(os.getenv("ALLOWED_GUILD_IDS", ""), "ALLOWED_GUILD_IDS", required=True),
            ids(os.getenv("ALLOWED_CHANNEL_IDS", ""), "ALLOWED_CHANNEL_IDS"),
            Path(os.getenv("DISCORD_TOKEN_FILE", "secrets/discord_token.txt")),
            Path(os.getenv("DATA_DIR", "data")),
            Path(os.getenv("PERSONA_FILE", "config/persona.json")),
            local_model_url(os.getenv("OLLAMA_URL", "http://ollama:11434")),
            model,
            os.getenv("DASHBOARD_ENABLED", "0") == "1",
            dashboard_port,
        )

    def read_token(self) -> str:
        try:
            token = self.token_file.read_text(encoding="utf-8").strip()
        except OSError:
            raise ConfigurationError("The bot token file is missing or unreadable. Run scripts/setup.py.") from None
        if len(token) < 30 or any(char.isspace() for char in token) or token.lower().startswith("paste"):
            raise ConfigurationError("The bot token file does not contain a valid-looking token.")
        return token

