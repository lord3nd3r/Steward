"""Built-in tools and the default registry."""

from steward.core.tools.files import ListDirectoryTool, ReadFileTool, RestoreFileTool, WriteFileTool
from steward.core.tools.memory import ForgetNoteTool, SaveNoteTool, SearchNotesTool
from steward.core.tools.registry import ToolRegistry
from steward.core.tools.shell import RunCommandTool

__all__ = ["ToolRegistry", "default_registry"]


def default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    for tool in (
        RunCommandTool(),
        ReadFileTool(),
        WriteFileTool(),
        RestoreFileTool(),
        ListDirectoryTool(),
        SaveNoteTool(),
        SearchNotesTool(),
        ForgetNoteTool(),
    ):
        registry.register(tool)
    registry.load_entry_points()
    return registry
