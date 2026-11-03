"""Tool registry, including the ``steward.tools`` entry-point group."""

from __future__ import annotations

import logging
import os
from typing import Iterable

from steward.core.tools.base import Tool

log = logging.getLogger(__name__)


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("Tool is missing a name.")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self, *, include_machine_tools: bool = True) -> list[dict]:
        from steward.core.messages import MACHINE_TOOLS

        names = self.names()
        if not include_machine_tools:
            names = [name for name in names if name not in MACHINE_TOOLS]
        return [self._tools[name].schema() for name in names]

    def load_entry_points(self) -> None:
        if os.environ.get("STEWARD_DISABLE_PLUGINS") == "1":
            return
        try:
            from importlib.metadata import entry_points
        except ImportError:  # pragma: no cover
            return
        try:
            found = entry_points(group="steward.tools")
        except Exception as exc:  # pragma: no cover
            log.warning("Could not read steward.tools entry points: %s", exc)
            return
        for item in found:
            try:
                loaded = item.load()
            except Exception as exc:
                log.warning("Skipping tool plugin %s: %s", item.name, exc)
                continue
            self._absorb(loaded, item.name)

    def _absorb(self, loaded: object, label: str) -> None:
        if isinstance(loaded, Tool):
            self.register(loaded)
            return
        if isinstance(loaded, type) and issubclass(loaded, Tool):
            self.register(loaded())
            return
        if callable(loaded):
            produced = loaded()
            if isinstance(produced, Tool):
                self.register(produced)
                return
            if isinstance(produced, Iterable) and not isinstance(produced, (str, bytes)):
                for tool in produced:
                    if isinstance(tool, Tool):
                        self.register(tool)
                return
        log.warning("Plugin %s did not produce a Tool.", label)
