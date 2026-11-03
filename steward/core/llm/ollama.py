"""Ollama native /api/chat client, with streaming and tool calls."""

from __future__ import annotations

import json
import threading
from typing import Any, Callable

import httpx

from steward.core.errors import Cancelled, StewardError, ToolUnsupportedError
from steward.core.llm.base import ChatMessage, LLMResponse
from steward.core.models import ToolCall


class OllamaBackend:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout: float = 180,
        temperature: float = 0.3,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.transport = transport

    def list_models(self) -> list[str]:
        try:
            with self._client() as client:
                response = client.get("/api/tags")
                _raise_for_status(response)
                payload = response.json()
        except httpx.ConnectError as exc:
            raise StewardError(
                f"Could not reach Ollama at {self.base_url}. Start it with `ollama serve`, "
                f"or change the URL in settings."
            ) from exc
        except httpx.TimeoutException as exc:
            raise StewardError(f"Timed out contacting Ollama at {self.base_url}.") from exc
        names = []
        for item in payload.get("models") or []:
            name = item.get("name") or item.get("model")
            if name:
                names.append(name)
        return sorted(names)

    def complete(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None,
        on_token: Callable[[str], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_format_message(message) for message in messages],
            "stream": True,
            "options": {"temperature": self.temperature},
        }
        if tools:
            payload["tools"] = tools
        parser = OllamaStreamParser()
        try:
            with self._client() as client:
                with client.stream("POST", "/api/chat", json=payload) as response:
                    if response.status_code >= 400:
                        body = response.read().decode(errors="replace")
                        _raise_for_body(response.status_code, body)
                    for line in response.iter_lines():
                        if cancel is not None and cancel.is_set():
                            raise Cancelled("".join(parser.parts))
                        delta = parser.feed_line(line)
                        if delta and on_token:
                            on_token(delta)
        except Cancelled:
            raise
        except ToolUnsupportedError:
            raise
        except StewardError:
            raise
        except httpx.ConnectError as exc:
            raise StewardError(
                f"Could not reach Ollama at {self.base_url}. Start it with `ollama serve`, "
                "or change the URL in settings."
            ) from exc
        except httpx.TimeoutException as exc:
            raise StewardError(f"Timed out waiting for {self.model} from Ollama.") from exc
        except httpx.HTTPError as exc:
            raise StewardError(f"Ollama request failed: {exc}") from exc
        return parser.finish()

    def _client(self) -> httpx.Client:
        timeout = httpx.Timeout(self.timeout, connect=5.0)
        kwargs: dict[str, Any] = {"base_url": self.base_url, "timeout": timeout}
        if self.transport is not None:
            kwargs["transport"] = self.transport
        return httpx.Client(**kwargs)


class OllamaStreamParser:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.tool_calls: list[ToolCall] = []

    def feed_line(self, line: str) -> str:
        line = line.strip()
        if not line:
            return ""
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise StewardError(f"Ollama returned a line that was not JSON: {line[:200]}") from exc
        if isinstance(data, dict) and data.get("error") and "message" not in data:
            raise StewardError(f"Ollama error: {data['error']}")
        message = data.get("message") or {}
        piece = message.get("content") or ""
        if piece:
            self.parts.append(piece)
        raw_calls = message.get("tool_calls") or []
        if raw_calls:
            self.tool_calls = [_parse_tool_call(raw, index) for index, raw in enumerate(raw_calls)]
        return piece

    def finish(self) -> LLMResponse:
        return LLMResponse(content="".join(self.parts), tool_calls=list(self.tool_calls))


def _format_message(message: ChatMessage) -> dict[str, Any]:
    payload: dict[str, Any] = {"role": message.role, "content": message.content or ""}
    if message.role == "assistant" and message.tool_calls:
        payload["tool_calls"] = [
            {
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in message.tool_calls
        ]
    if message.role == "tool":
        if message.name:
            payload["tool_name"] = message.name
    return payload


def _parse_tool_call(raw: dict[str, Any], index: int) -> ToolCall:
    function = raw.get("function") or raw
    name = function.get("name") or raw.get("name") or ""
    arguments = function.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError:
            arguments = {}
    if not isinstance(arguments, dict):
        arguments = {}
    call_id = str(raw.get("id") or f"call_{index}_{name}")
    return ToolCall(id=call_id, name=name, arguments=arguments)


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    _raise_for_body(response.status_code, response.text)


def _raise_for_body(status: int, body: str) -> None:
    snippet = body[:500]
    if status in {400, 404, 422, 500} and "tool" in snippet.lower():
        raise ToolUnsupportedError(snippet)
    raise StewardError(f"Ollama returned {status}: {snippet}")
