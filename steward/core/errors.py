"""Exceptions raised by Steward."""


class StewardError(Exception):
    """A problem the user can act on. Message text is shown in the CLI and GUI."""


class ToolUnsupportedError(StewardError):
    """The model server rejected a request that included native tool definitions."""


class Cancelled(StewardError):
    """The user stopped the current turn. ``partial`` is any text already streamed."""

    def __init__(self, partial: str = "") -> None:
        super().__init__("Cancelled")
        self.partial = partial
