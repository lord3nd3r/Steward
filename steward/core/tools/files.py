"""Read, write, and list files on a saved machine."""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Any

from steward.core.approval import path_is_sensitive
from steward.core.errors import StewardError
from steward.core.execution import list_remote_directory, read_remote_bytes, write_remote_bytes
from steward.core.machines import resolve_machine
from steward.core.models import CommandAssessment
from steward.core.tools.base import Tool, ToolContext

_MAX_READ = 200_000
_MAX_WRITE = 1_000_000
_MAX_ENTRIES = 400


class ReadFileTool(Tool):
    name = "read_file"
    description = "Read a text file on a saved machine. Binary files are reported but not dumped."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path. ~ is the home directory of the target machine."},
            "machine": {"type": "string", "description": "Saved machine name. Omit only when one machine is configured."},
        },
        "required": ["path"],
    }
    approval_class = "read"

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        path = _path_arg(arguments)
        if path is None:
            return "Error: path is required."
        try:
            machine = resolve_machine(context.store, arguments.get("machine"))
            if machine.is_local:
                return _read_local(path)
            data = read_remote_bytes(machine, context.store, path, _MAX_READ + 1)
        except StewardError as exc:
            return f"Error: {exc}"
        except OSError as exc:
            return _format_os_error(exc)
        return _render_bytes(data, path)


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "Write a text file on a saved machine, replacing the file if it exists. "
        "Parent directories are created. This runs as the login user, not as root."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string", "description": "The full new contents of the file."},
            "machine": {"type": "string", "description": "Saved machine name. Omit only when one machine is configured."},
        },
        "required": ["path", "content"],
    }
    approval_class = "mutate"

    def classify(self, arguments: dict[str, Any]) -> CommandAssessment:
        path = str(arguments.get("path") or "")
        remote = bool(str(arguments.get("machine") or "").strip())
        if path_is_sensitive(path, remote=remote):
            return CommandAssessment("destructive", False, ("sensitive path",))
        return CommandAssessment("mutating", False, ())

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        path = _path_arg(arguments)
        if path is None:
            return "Error: path is required."
        if "content" not in arguments or not isinstance(arguments.get("content"), str):
            return "Error: content must be a string."
        content = arguments["content"]
        if len(content.encode("utf-8")) > _MAX_WRITE:
            return f"Error: refusing to write more than {_MAX_WRITE} bytes."
        try:
            machine = resolve_machine(context.store, arguments.get("machine"))
            if machine.is_local:
                target = Path(path).expanduser()
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                return f"Wrote {len(content)} characters to {target}."
            written = write_remote_bytes(
                machine, context.store, path, content.encode("utf-8"), True
            )
        except StewardError as exc:
            return f"Error: {exc}"
        except OSError as exc:
            return _format_os_error(exc)
        return f"Wrote {len(content)} characters to {written} on {machine.name}."


class ListDirectoryTool(Tool):
    name = "list_directory"
    description = "List files and directories at a path on a saved machine."
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory path. Defaults to the home directory."},
            "machine": {"type": "string", "description": "Saved machine name. Omit only when one machine is configured."},
        },
        "required": ["path"],
    }
    approval_class = "read"

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        path = _path_arg(arguments) or "~"
        try:
            machine = resolve_machine(context.store, arguments.get("machine"))
            if machine.is_local:
                return _list_local(path)
            entries = list_remote_directory(machine, context.store, path)
        except StewardError as exc:
            return f"Error: {exc}"
        except OSError as exc:
            return _format_os_error(exc)
        return _render_entries(path, entries)


def _path_arg(arguments: dict[str, Any]) -> str | None:
    path = arguments.get("path")
    if not isinstance(path, str) or not path.strip():
        return None
    return path.strip()


def _read_local(path: str) -> str:
    target = Path(path).expanduser()
    if target.is_dir():
        return f"Error: {target} is a directory. Use list_directory."
    try:
        data = target.read_bytes()
    except OSError as exc:
        return _format_os_error(exc)
    if len(data) > _MAX_READ:
        data = data[: _MAX_READ + 1]
    return _render_bytes(data, str(target))


def _list_local(path: str) -> str:
    target = Path(path).expanduser()
    if not target.exists():
        return f"Error: no such directory: {target}"
    if not target.is_dir():
        return f"Error: {target} is not a directory."
    entries = []
    with os.scandir(target) as scan:
        for item in scan:
            try:
                kind = "dir" if item.is_dir(follow_symlinks=False) else "file"
                size = item.stat(follow_symlinks=False).st_size
            except OSError:
                kind, size = "file", 0
            entries.append((item.name, kind, size))
    return _render_entries(str(target), entries)


def _format_os_error(exc: OSError) -> str:
    text = f"Error: {exc}"
    if exc.errno in (errno.EACCES, errno.EPERM):
        return (
            text
            + " Hint: permission denied. File tools cannot sudo. "
            + "Retry with run_command using sudo if this needs root. Steward will ask for approval."
        )
    return text


def _render_bytes(data: bytes, label: str) -> str:
    truncated = len(data) > _MAX_READ
    if truncated:
        data = data[:_MAX_READ]
    if b"\x00" in data[:1024]:
        return f"(binary file {label}, not shown)"
    text = data.decode("utf-8", errors="replace")
    if truncated:
        text += f"\n...[truncated, showing first {_MAX_READ} bytes]"
    return text if text else f"(empty file {label})"


def _render_entries(label: str, entries: list[tuple[str, str, int]]) -> str:
    entries = sorted(entries, key=lambda item: (item[1] != "dir", item[0].lower()))
    extra = ""
    if len(entries) > _MAX_ENTRIES:
        extra = f"\n...[{len(entries) - _MAX_ENTRIES} more entries not shown]"
        entries = entries[:_MAX_ENTRIES]
    lines = [f"{kind:4} {size:10}  {name}" for name, kind, size in entries]
    body = "\n".join(lines) if lines else "(empty directory)"
    return f"{label}\n{body}{extra}"
