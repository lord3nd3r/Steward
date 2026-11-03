"""llama.cpp llama-server client (OpenAI-compatible /v1/chat/completions)."""

from __future__ import annotations

import json
import threading
from typing import Any, Callable

import httpx

from steward.core.errors import Cancelled, StewardError, ToolUnsupportedError
from steward.core.llm.base import ChatMessage, LLMResponse
from steward.core.models import ToolCall


class LlamaCppBackend:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout: float = 180,
        temperature: float = 0.3,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = _normalize_base(base_url)
        self.model = model
        self.timeout = timeout
        self.temperature = temperature
        self.transport = transport

    def list_models(self) -> list[str]:
        try:
            with self._client() as client:
                response = client.get("/models")
                _raise_for_status(response)
                payload = response.json()
        except httpx.ConnectError as exc:
            raise StewardError(
                f"Could not reach llama-server at {self.base_url}. "
                "Start it with `llama-server -m <model.gguf> --port 8080`, or change the URL in settings."
            ) from exc
        except httpx.TimeoutException as exc:
            raise StewardError(f"Timed out contacting llama-server at {self.base_url}.") from exc
        names = []
        for item in payload.get("data") or []:
            model_id = item.get("id")
            if model_id:
                names.append(model_id)
        return sorted(names)

    def complete(
        self,
        messages: list[ChatMessage],
        tools: list[dict[str, Any]] | None,
        on_token: Callable[[str], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model or "local",
            "messages": [_format_message(message) for message in messages],
            "stream": True,
            "temperature": self.temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        try:
            with self._client() as client:
                with client.stream("POST", "/chat/completions", json=payload) as response:
                    if response.status_code >= 400:
                        body = response.read().decode(errors="replace")
                        _raise_for_body(response.status_code, body)
                    content_type = response.headers.get("content-type", "")
                    if "application/json" in content_type and "text/event-stream" not in content_type:
                        response.read()
                        return _parse_complete_json(json.loads(response.text), on_token)
                    parser = OpenAIStreamParser()
                    for line in response.iter_lines():
                        if cancel is not None and cancel.is_set():
                            raise Cancelled("".join(parser.parts))
                        delta = parser.feed_line(line)
                        if delta and on_token:
                            on_token(delta)
                    return parser.finish()
        except Cancelled:
            raise
        except ToolUnsupportedError:
            raise
        except StewardError:
            raise
        except httpx.ConnectError as exc:
            raise StewardError(
                f"Could not reach llama-server at {self.base_url}. "
                "Start it with `llama-server -m <model.gguf>`, or change the URL in settings."
            ) from exc
        except httpx.TimeoutException as exc:
            raise StewardError(f"Timed out waiting for {self.model or 'the model'} from llama-server.") from exc
        except httpx.HTTPError as exc:
            raise StewardError(f"llama-server request failed: {exc}") from exc

    def _client(self) -> httpx.Client:
        timeout = httpx.Timeout(self.timeout, connect=5.0)
        kwargs: dict[str, Any] = {"base_url": self.base_url, "timeout": timeout}
        if self.transport is not None:
            kwargs["transport"] = self.transport
        return httpx.Client(**kwargs)


class OpenAIStreamParser:
    def __init__(self) -> None:
        self.parts: list[str] = []
        self.calls: dict[int, dict[str, str]] = {}

    def feed_line(self, line: str) -> str:
        line = line.strip()
        if not line or line.startswith(":"):
            return ""
        if line.startswith("data:"):
            line = line[len("data:") :].strip()
        if line == "[DONE]" or not line:
            return ""
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return ""
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            if "tool" in message.lower():
                raise ToolUnsupportedError(message)
            raise StewardError(f"llama-server error: {message}")
        choices = data.get("choices") or []
        if not choices:
            return ""
        delta = choices[0].get("delta") or {}
        piece = delta.get("content") or ""
        if piece:
            self.parts.append(piece)
        for raw in delta.get("tool_calls") or []:
            index = int(raw.get("index") or 0)
            slot = self.calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if raw.get("id"):
                slot["id"] = raw["id"]
            function = raw.get("function") or {}
            if function.get("name"):
                slot["name"] += function["name"]
            if function.get("arguments"):
                slot["arguments"] += function["arguments"]
        return piece

    def finish(self) -> LLMResponse:
        return LLMResponse(content="".join(self.parts), tool_calls=_slots_to_calls(self.calls))


def _parse_complete_json(data: dict[str, Any], on_token: Callable[[str], None] | None) -> LLMResponse:
    choices = data.get("choices") or []
    if not choices:
        return LLMResponse(content="")
    message = choices[0].get("message") or {}
    content = message.get("content") or ""
    if content and on_token:
        on_token(content)
    calls = []
    for index, raw in enumerate(message.get("tool_calls") or []):
        function = raw.get("function") or {}
        arguments = function.get("arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments else {}
            except json.JSONDecodeError:
                arguments = {}
        calls.append(
            ToolCall(
                id=str(raw.get("id") or f"call_{index}"),
                name=function.get("name") or "",
                arguments=arguments if isinstance(arguments, dict) else {},
            )
        )
    return LLMResponse(content=content, tool_calls=calls)


def _slots_to_calls(slots: dict[int, dict[str, str]]) -> list[ToolCall]:
    calls = []
    for index in sorted(slots):
        slot = slots[index]
        raw_args = slot["arguments"]
        try:
            arguments = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError:
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        calls.append(
            ToolCall(
                id=slot["id"] or f"call_{index}_{slot['name']}",
                name=slot["name"],
                arguments=arguments,
            )
        )
    return calls


def _format_message(message: ChatMessage) -> dict[str, Any]:
    if message.role == "assistant" and message.tool_calls:
        return {
            "role": "assistant",
            "content": message.content or None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                }
                for call in message.tool_calls
            ],
        }
    if message.role == "tool":
        return {
            "role": "tool",
            "tool_call_id": message.tool_call_id or message.name or "call",
            "content": message.content,
        }
    return {"role": message.role, "content": message.content}


def _normalize_base(url: str) -> str:
    url = url.rstrip("/")
    if url.endswith("/v1"):
        return url
    return url + "/v1"


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    _raise_for_body(response.status_code, response.text)


def _raise_for_body(status: int, body: str) -> None:
    snippet = body[:500]
    if status in {400, 404, 422, 500} and "tool" in snippet.lower():
        raise ToolUnsupportedError(snippet)
    raise StewardError(f"llama-server returned {status}: {snippet}")
