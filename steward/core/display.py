"""Human-readable summaries of tool calls, shared by the CLI and the GUI."""

from __future__ import annotations

import json
from typing import Any


def summarize_action(tool_name: str, arguments: dict[str, Any]) -> tuple[str, str]:
    """Return ``(machine, summary)`` for an approval dialog or activity row."""
    machine = str(arguments.get("machine") or "")
    if tool_name in {"run_command", "read_file", "write_file", "list_directory"}:
        machine = machine or "local"
    else:
        machine = machine or "memory"

    if tool_name == "run_command":
        command = str(arguments.get("command") or "")
        cwd = arguments.get("cwd")
        if cwd:
            return machine, f"(cwd {cwd})\n{command}"
        return machine, command
    if tool_name in {"read_file", "list_directory"}:
        return machine, str(arguments.get("path") or "")
    if tool_name == "write_file":
        content = str(arguments.get("content") or "")
        preview = content[:800]
        if len(content) > 800:
            preview += "\n…"
        return machine, f"{arguments.get('path') or ''}\n\n{preview}"
    if tool_name == "save_note":
        tags = arguments.get("tags") or ""
        suffix = f"\ntags: {tags}" if tags else ""
        return "memory", f"{arguments.get('title')}: {arguments.get('content')}{suffix}"
    if tool_name == "search_notes":
        return "memory", str(arguments.get("query") or "")
    if tool_name == "forget_note":
        return "memory", f"id={arguments.get('note_id') or ''} title={arguments.get('title') or ''}"
    try:
        rendered = json.dumps(arguments, indent=2, default=str)
    except TypeError:
        rendered = str(arguments)
    return machine, rendered[:1000]
