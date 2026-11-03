"""A backend that returns a script instead of calling a model server."""

from __future__ import annotations

from steward.core.llm.base import ChatMessage, LLMResponse


class ScriptedBackend:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.seen: list[list[ChatMessage]] = []
        self.tools_seen: list[object] = []

    def complete(self, messages, tools=None, on_token=None, cancel=None) -> LLMResponse:
        self.seen.append(list(messages))
        self.tools_seen.append(tools)
        if cancel is not None and cancel.is_set():
            from steward.core.errors import Cancelled

            raise Cancelled()
        response = self.responses.pop(0)
        if on_token and response.content and not response.tool_calls:
            on_token(response.content)
        elif on_token and response.content:
            on_token(response.content)
        return response

    def list_models(self) -> list[str]:
        return ["scripted"]
