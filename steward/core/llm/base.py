"""Neutral chat types and the backend interface."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from steward.core.models import Agent, ToolCall
from steward.core.settings import Settings


@dataclass
class ChatMessage:
    role: str
    content: str = ""
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    used_text_protocol: bool = False


class LLMBackend(Protocol):
    def complete(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None,
        on_token: Callable[[str], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> LLMResponse: ...

    def list_models(self) -> list[str]: ...


def build_backend(settings: Settings, agent: Agent) -> LLMBackend:
    from steward.core.errors import StewardError
    from steward.core.llm.llamacpp import LlamaCppBackend
    from steward.core.llm.ollama import OllamaBackend

    timeout = settings.request_timeout
    if agent.backend == "ollama":
        return OllamaBackend(settings.ollama_base_url, agent.model, timeout=timeout, temperature=settings.temperature)
    if agent.backend == "llama_cpp":
        return LlamaCppBackend(
            settings.llama_cpp_base_url, agent.model, timeout=timeout, temperature=settings.temperature
        )
    raise StewardError(f"Unknown backend '{agent.backend}'. Use ollama or llama_cpp.")


def list_backend_models(settings: Settings, backend: str) -> list[str]:
    probe = Agent(
        id="",
        name="probe",
        persona="",
        backend=backend,
        model="",
        approval_mode="ask",
        tool_protocol="auto",
        max_steps=1,
        created_at="",
        updated_at="",
    )
    return build_backend(settings, probe).list_models()
