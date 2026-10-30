from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import math
import re

from .config import ConfigurationError, Persona
from .storage import Store
from .prompts import PromptSettings


@dataclass(frozen=True)
class Tuning:
    model: str
    temperature: float = 0.8
    max_tokens: int = 128
    context_tokens: int = 4096
    keep_alive: str = "30m"
    cooldown_seconds: float = 2
    requests_per_minute: int = 30
    memory_turns: int = 2
    memory_minutes: int = 15
    preload: bool = True

    @classmethod
    def parse(cls, data: dict) -> "Tuning":
        if not isinstance(data, dict) or set(data) != {field.name for field in fields(cls)}:
            raise ValueError("Supply all tuning fields and no extra fields.")
        if not isinstance(data["model"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,149}", data["model"]) or "cloud" in data["model"].lower():
            raise ValueError("Choose an installed local model.")
        for key, low, high in (("temperature", 0, 1.5), ("cooldown_seconds", 0, 30)):
            value = data[key]
            if type(value) not in {int, float} or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{key} is outside its allowed range.")
        for key, low, high in (("max_tokens", 32, 512), ("context_tokens", 2048, 8192), ("requests_per_minute", 1, 120), ("memory_turns", 0, 6), ("memory_minutes", 1, 60)):
            if type(data[key]) is not int or not low <= data[key] <= high:
                raise ValueError(f"{key} is outside its allowed range.")
        if not isinstance(data["keep_alive"], str) or data["keep_alive"] not in {"5m", "30m", "2h", "-1"} or type(data["preload"]) is not bool:
            raise ValueError("Choose one of the supported keep-ready options.")
        return cls(**data)


class RevisionConflict(ValueError):
    pass


class Control:
    def __init__(self, store: Store, model: str, persona: Persona):
        self.store = store
        saved = store.read_control()
        try:
            self.tuning = Tuning.parse(saved["tuning"]) if saved else Tuning(model)
            self.persona = Persona.from_dict(saved["persona"]) if saved else persona
            self.prompts = PromptSettings.from_dict(saved["prompts"]) if saved and "prompts" in saved else PromptSettings()
            self.revision = int(saved["revision"]) if saved else 0
        except (ValueError, KeyError, TypeError):
            raise ConfigurationError("Saved dashboard settings are invalid. Restore the private database backup.") from None

    def document(self) -> dict:
        return {"revision": self.revision, "tuning": asdict(self.tuning), "persona": asdict(self.persona), "prompts": asdict(self.prompts)}

    def _commit(self, revision: int, tuning: Tuning, persona: Persona, prompts: PromptSettings | None = None) -> None:
        if type(revision) is not int or revision != self.revision:
            raise RevisionConflict("Settings changed in another tab. Refresh before saving again.")
        prompts = self.prompts if prompts is None else prompts
        data = {"revision": self.revision + 1, "tuning": asdict(tuning), "persona": asdict(persona), "prompts": asdict(prompts)}
        self.store.write_control(data)
        self.tuning, self.persona, self.prompts, self.revision = tuning, persona, prompts, self.revision + 1

    def update_tuning(self, revision: int, data: dict) -> None:
        self._commit(revision, Tuning.parse(data), self.persona)

    def update_persona(self, revision: int, data: dict, prompts: dict | None = None) -> None:
        parsed = self.prompts if prompts is None else PromptSettings.from_dict(prompts)
        self._commit(revision, self.tuning, Persona.from_dict(data), parsed)
