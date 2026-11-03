import json

import httpx

from steward.core.llm.base import ChatMessage
from steward.core.llm.fallback import parse_tool_calls, strip_tool_calls
from steward.core.llm.llamacpp import OpenAIStreamParser
from steward.core.llm.ollama import OllamaBackend, OllamaStreamParser
from steward.core.models import ToolCall


def test_text_protocol_parser_accepts_tags_and_fences():
    text = """
Checking.
<tool_call>
{"name": "run_command", "arguments": {"command": "uname -a", "machine": "local"}}
</tool_call>
```tool
{"name": "save_note", "arguments": {"title": "Host", "content": "The laptop is called oak."}}
```
"""
    calls = parse_tool_calls(text)
    assert [call.name for call in calls] == ["run_command", "save_note"]
    assert calls[0].arguments["command"] == "uname -a"
    assert "tool_call" not in strip_tool_calls(text)
    assert parse_tool_calls("no tools here") == []
    assert parse_tool_calls('<tool_call>{"name": "x", "arguments": {"a": 1}, "extra": true}</tool_call>')[0].arguments == {"a": 1}


def test_ollama_stream_parser_accumulates_text_and_tool_calls():
    parser = OllamaStreamParser()
    first = parser.feed_line(json.dumps({"message": {"role": "assistant", "content": "Hi"}, "done": False}))
    second = parser.feed_line(
        json.dumps(
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"type": "function", "function": {"name": "run_command", "arguments": {"command": "ls"}}}
                    ],
                },
                "done": True,
            }
        )
    )
    assert first == "Hi"
    assert second == ""
    response = parser.finish()
    assert response.content == "Hi"
    assert response.tool_calls[0].name == "run_command"
    assert response.tool_calls[0].arguments == {"command": "ls"}


def test_openai_stream_parser_joins_tool_arguments():
    parser = OpenAIStreamParser()
    parser.feed_line('data: {"choices":[{"delta":{"content":"Hi"}}]}')
    parser.feed_line(
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"run_command","arguments":""}}]}}]}'
    )
    parser.feed_line(
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"command\\": \\"ls\\"}"}}]}}]}'
    )
    parser.feed_line("data: [DONE]")
    response = parser.finish()
    assert response.content == "Hi"
    assert response.tool_calls == [ToolCall(id="c1", name="run_command", arguments={"command": "ls"})]


def test_ollama_client_streams_with_a_mock_transport():
    body = (
        json.dumps({"message": {"role": "assistant", "content": "Hel"}, "done": False})
        + "\n"
        + json.dumps({"message": {"role": "assistant", "content": "lo"}, "done": True})
        + "\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        payload = json.loads(request.content)
        assert payload["messages"][0]["role"] == "user"
        assert payload["tools"][0]["function"]["name"] == "run_command"
        return httpx.Response(200, content=body.encode(), headers={"content-type": "application/x-ndjson"})

    backend = OllamaBackend("http://ollama.test", "demo", transport=httpx.MockTransport(handler))
    tokens = []
    response = backend.complete(
        [ChatMessage("user", "hi")],
        tools=[{"type": "function", "function": {"name": "run_command", "parameters": {}}}],
        on_token=tokens.append,
    )
    assert response.content == "Hello"
    assert tokens == ["Hel", "lo"]


def test_ollama_reports_missing_tool_support():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "model does not support tools"})

    backend = OllamaBackend("http://ollama.test", "demo", transport=httpx.MockTransport(handler))
    from steward.core.errors import ToolUnsupportedError

    try:
        backend.complete([ChatMessage("user", "hi")], tools=[{"type": "function", "function": {"name": "x"}}])
    except ToolUnsupportedError as exc:
        assert "tool" in str(exc).lower()
    else:
        raise AssertionError("expected ToolUnsupportedError")
