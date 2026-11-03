"""Run a shell command on a saved machine (this computer or SSH)."""

from __future__ import annotations

from typing import Any

from steward.core.approval import classify_command
from steward.core.errors import StewardError
from steward.core.execution import format_command_result, run_local_command, run_ssh_command
from steward.core.machines import resolve_machine
from steward.core.models import CommandAssessment
from steward.core.tools.base import Tool, ToolContext


class RunCommandTool(Tool):
    name = "run_command"
    description = (
        "Run bash on a saved machine. Pipes and quotes are allowed. "
        "If the machine line does not say root, include sudo or doas on package and system commands, "
        "with non-interactive flags. Do not put secrets in the command."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The bash command to run. This exact string is what the user is asked to approve.",
            },
            "machine": {
                "type": "string",
                "description": "Saved machine name. Omit only when one machine is configured.",
            },
            "cwd": {
                "type": "string",
                "description": "Working directory. Optional. Use an absolute path on a remote machine.",
            },
            "timeout": {
                "type": "integer",
                "description": "Seconds before the command is stopped. Optional.",
            },
        },
        "required": ["command"],
    }
    approval_class = "mutate"

    def classify(self, arguments: dict[str, Any]) -> CommandAssessment:
        command = arguments.get("command")
        if not isinstance(command, str):
            return CommandAssessment("mutating", False, ("missing command",))
        return classify_command(command)

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            return "Error: command is required."
        timeout = _timeout(arguments.get("timeout"), context.settings.command_timeout)
        cwd = arguments.get("cwd")
        cwd_text = str(cwd).strip() if isinstance(cwd, str) and cwd.strip() else None
        try:
            machine = resolve_machine(context.store, arguments.get("machine"))
            if machine.is_local:
                if cwd_text:
                    from pathlib import Path

                    expanded = Path(cwd_text).expanduser()
                    if not expanded.is_dir():
                        return f"Error: working directory does not exist: {expanded}"
                    cwd_text = str(expanded)
                result = run_local_command(
                    command,
                    timeout=timeout,
                    cwd=cwd_text,
                    sudo_password=context.sudo_password,
                    cancel=context.cancel,
                )
            else:
                result = run_ssh_command(
                    machine,
                    context.store,
                    command,
                    timeout=timeout,
                    cwd=cwd_text,
                    sudo_password=context.sudo_password,
                    cancel=context.cancel,
                )
        except StewardError as exc:
            return f"Error: {exc}"
        return format_command_result(result, command=command)


def _timeout(value: object, default: int) -> int:
    if value is None or value == "":
        return max(1, min(int(default), 900))
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return max(1, min(int(default), 900))
    return max(1, min(number, 900))
