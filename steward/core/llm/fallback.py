"""Text tool-call protocol for models without native tool calling."""

from __future__ import annotations

import json
import re
import uuid

from steward.core.llm.base import LLMResponse
from steward.core.models import ToolCall

_TAG_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```tool\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def parse_tool_calls(text: str) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for match in list(_TAG_RE.finditer(text)) + list(_FENCE_RE.finditer(text)):
        payload = _extract_object(match.group(1))
        if not payload:
            continue
        name = payload.get("name") or payload.get("tool")
        if not isinstance(name, str) or not name.strip():
            continue
        arguments = payload.get("arguments", payload.get("args", {}))
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        calls.append(ToolCall(id=f"text_{uuid.uuid4().hex[:10]}", name=name.strip(), arguments=arguments))
    return calls


def strip_tool_calls(text: str) -> str:
    cleaned = _TAG_RE.sub("", text)
    cleaned = _FENCE_RE.sub("", cleaned)
    return cleaned.strip()


def apply_text_tool_calls(response: LLMResponse) -> LLMResponse:
    """If the model wrote tool calls as text, promote them to structured calls."""
    if response.tool_calls or not response.content:
        return response
    parsed = parse_tool_calls(response.content)
    if not parsed:
        return response
    response.tool_calls = parsed
    response.content = strip_tool_calls(response.content)
    response.used_text_protocol = True
    return response


def _extract_object(text: str) -> dict | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    value = json.loads(text[start : index + 1])
                except json.JSONDecodeError:
                    return None
                return value if isinstance(value, dict) else None
    return None
