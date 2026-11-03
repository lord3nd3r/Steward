"""Tool interface. Register new tools on a ToolRegistry; the agent loop does the rest."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from steward.core.models import CommandAssessment
from steward.core.settings import Settings
from steward.core.store import Store


@dataclass
class ToolContext:
    agent_id: str
    store: Store
    settings: Settings
    sudo_password: str | None = None
    cancel: threading.Event | None = None


class Tool:
    """A single action the model can request.

    ``approval_class``:
    - ``safe``: never ask (memory search and save)
    - ``read``: ask only in ask-every-time mode
    - ``mutate``: ask unless the agent is full-auto

    ``classify`` can upgrade a call to destructive, which always asks.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    approval_class: str = "mutate"

    def classify(self, arguments: dict[str, Any]) -> CommandAssessment:
        level = "readonly" if self.approval_class in {"safe", "read"} else "mutating"
        return CommandAssessment(level=level, needs_sudo=False, reasons=())

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        raise NotImplementedError

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
