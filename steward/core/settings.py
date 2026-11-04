"""TOML settings under the XDG config directory."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass

from steward.core.errors import StewardError
from steward.core.paths import config_path, ensure_private_dir

_HEADER = """\
# Steward settings.
# URLs are shared by every agent. Each agent picks its own model.
# Environment overrides (not written back): STEWARD_OLLAMA_URL, STEWARD_LLAMA_CPP_URL.
"""


@dataclass
class Settings:
    ollama_base_url: str = "http://127.0.0.1:11434"
    llama_cpp_base_url: str = "http://127.0.0.1:8080"
    max_steps: int = 12
    history_limit: int = 40
    request_timeout: float = 180.0
    command_timeout: int = 180
    temperature: float = 0.3
    # Reserved for a future Ollama embedding index. "keyword" is the only
    # default; "ollama" uses a local embedding model when configured.
    memory_backend: str = "keyword"
    memory_embedding_model: str = "nomic-embed-text"

    @classmethod
    def load(cls) -> Settings:
        path = config_path()
        if not path.exists():
            settings = cls()
            settings.save()
            return settings._with_env()
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise StewardError(f"Could not read {path}: {exc}") from exc
        backends = data.get("backends") or {}
        defaults = data.get("defaults") or {}
        memory = data.get("memory") or {}
        settings = cls(
            ollama_base_url=str(backends.get("ollama_base_url", cls.ollama_base_url)),
            llama_cpp_base_url=str(backends.get("llama_cpp_base_url", cls.llama_cpp_base_url)),
            max_steps=_as_int(defaults.get("max_steps", cls.max_steps), "max_steps", 1, 40),
            history_limit=_as_int(defaults.get("history_limit", cls.history_limit), "history_limit", 4, 200),
            request_timeout=_as_float(
                defaults.get("request_timeout", cls.request_timeout), "request_timeout", 5, 3600
            ),
            command_timeout=_as_int(
                defaults.get("command_timeout", cls.command_timeout), "command_timeout", 1, 900
            ),
            temperature=_as_float(defaults.get("temperature", cls.temperature), "temperature", 0, 2),
            memory_backend=str(memory.get("backend", cls.memory_backend) or "keyword"),
            memory_embedding_model=str(
                memory.get("embedding_model", cls.memory_embedding_model) or "nomic-embed-text"
            ),
        )
        return settings._with_env()

    def _with_env(self) -> Settings:
        ollama = os.environ.get("STEWARD_OLLAMA_URL")
        llama = os.environ.get("STEWARD_LLAMA_CPP_URL")
        if ollama:
            self.ollama_base_url = ollama
        if llama:
            self.llama_cpp_base_url = llama
        return self

    def save(self) -> None:
        path = config_path()
        ensure_private_dir(path.parent)
        if path.exists():
            path.chmod(0o600)
        body = _HEADER + (
            "\n[backends]\n"
            f"ollama_base_url = {_toml_string(self.ollama_base_url)}\n"
            f"llama_cpp_base_url = {_toml_string(self.llama_cpp_base_url)}\n"
            "\n[defaults]\n"
            f"max_steps = {int(self.max_steps)}\n"
            f"history_limit = {int(self.history_limit)}\n"
            f"request_timeout = {float(self.request_timeout)}\n"
            f"command_timeout = {int(self.command_timeout)}\n"
            f"temperature = {float(self.temperature)}\n"
            "\n[memory]\n"
            f"backend = {_toml_string(self.memory_backend)}\n"
            f"embedding_model = {_toml_string(self.memory_embedding_model)}\n"
        )
        path.write_text(body, encoding="utf-8")
        path.chmod(0o600)


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _as_int(value: object, name: str, low: int, high: int) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise StewardError(f"Setting {name} must be an integer.") from exc
    return max(low, min(high, number))


def _as_float(value: object, name: str, low: float, high: float) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise StewardError(f"Setting {name} must be a number.") from exc
    return max(low, min(high, number))
