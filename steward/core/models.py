"""Datatypes shared by the store, the agent loop, and both front ends."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


BACKENDS = ("ollama", "llama_cpp")
APPROVAL_MODES = ("ask", "auto_readonly", "full_auto")
TOOL_PROTOCOLS = ("auto", "native", "text")

APPROVAL_LABELS = {
    "ask": "Ask every time",
    "auto_readonly": "Auto-approve reads",
    "full_auto": "Full auto",
}

BACKEND_LABELS = {
    "ollama": "Ollama",
    "llama_cpp": "llama.cpp",
}

@dataclass
class Agent:
    id: str
    name: str
    persona: str
    backend: str
    model: str
    approval_mode: str
    tool_protocol: str
    max_steps: int
    created_at: str
    updated_at: str


@dataclass
class Machine:
    name: str
    host: str = ""
    user: str = ""
    port: int = 22
    identity_file: str | None = None
    sudo_notes: str = ""
    use_agent: bool = True
    host_key_fingerprint: str = ""
    is_local: bool = False


@dataclass
class MachineFacts:
    """Cached read-only facts about a saved machine.

    Free-text fields from the host are not copied into the system prompt.
    ``fact_line`` keeps only short allowlisted tokens.
    """

    machine: str
    user_name: str = ""
    is_root: bool = False
    os_id: str = ""
    os_version: str = ""
    pkg: str = ""
    sudo_state: str = ""  # root | sudo | doas | none
    extras: str = ""  # comma-separated: flatpak, snap
    error: str = ""
    gathered_at: str = ""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class StoredMessage:
    id: int
    agent_id: str
    role: str
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    created_at: str = ""


@dataclass
class Note:
    id: int
    agent_id: str
    title: str
    content: str
    tags: str
    created_at: str
    updated_at: str


@dataclass
class ToolEventRecord:
    id: int
    agent_id: str
    tool_name: str
    arguments: dict[str, Any]
    result: str
    machine: str
    approved: bool
    destructive: bool
    created_at: str


@dataclass
class FileBackup:
    id: str
    agent_id: str | None
    machine: str
    path: str
    existed: bool
    content: bytes | None
    created_at: str


@dataclass(frozen=True)
class CommandAssessment:
    """How risky a shell command is. Destructive commands always need a person."""

    level: str  # readonly | mutating | destructive
    needs_sudo: bool = False
    reasons: tuple[str, ...] = ()
    requires_review: bool = False

    @property
    def destructive(self) -> bool:
        return self.level == "destructive"


@dataclass
class ApprovalRequest:
    tool_name: str
    arguments: dict[str, Any]
    machine: str
    summary: str
    reason: str
    destructive: bool
    needs_sudo: bool
    password_only: bool = False


@dataclass
class ApprovalDecision:
    approved: bool
    sudo_password: str | None = None


@dataclass
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str


@dataclass
class TurnResult:
    text: str
    steps: int
    cancelled: bool = False
    truncated: bool = False
