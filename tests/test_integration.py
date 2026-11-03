"""The real Ollama client, the agent loop, a local command, and a recalled note."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from steward.core.agent import AgentRunner
from steward.core.llm.base import build_backend
from steward.core.models import ApprovalDecision
from steward.core.settings import Settings
from steward.core.store import Store
from steward.core.tools import default_registry


class _Handler(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        _Handler.calls.append(body)
        index = len(_Handler.calls)
        if index == 1:
            message = {
                "role": "assistant",
                "content": "Checking the hostname.",
                "tool_calls": [
                    {
                        "type": "function",
                        "function": {
                            "name": "run_command",
                            "arguments": {"command": "echo hello-steward", "machine": "local"},
                        },
                    }
                ],
            }
        elif index == 2:
            message = {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "type": "function",
                        "function": {
                            "name": "save_note",
                            "arguments": {
                                "title": "Drink",
                                "content": "Likes black coffee in the morning.",
                            },
                        },
                    }
                ],
            }
        else:
            message = {"role": "assistant", "content": "You like black coffee in the morning."}
        payload = (json.dumps({"model": "demo", "message": message, "done": True}) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format, *_args):
        return


def test_ollama_http_loop_runs_a_command_and_recalls_a_note():
    _Handler.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        settings = Settings()
        settings.ollama_base_url = f"http://127.0.0.1:{server.server_address[1]}"
        settings.command_timeout = 10
        store = Store()
        agent = store.create_agent("House", model="demo", backend="ollama", approval_mode="full_auto")
        runner = AgentRunner(store, settings, default_registry(), build_backend)
        tokens: list[str] = []

        def approve(request):
            raise AssertionError(f"full-auto should not ask for {request.summary}")

        first = runner.run_turn(
            agent.id,
            "Check this machine and remember that I like black coffee in the morning.",
            on_token=tokens.append,
            approval_handler=approve,
        )
        assert "Checking the hostname." in tokens
        assert any("hello-steward" in json.dumps(call) for call in _Handler.calls)
        assert store.find_note_by_title(agent.id, "Drink") is not None
        assert "coffee" in first.text

        second = runner.run_turn(
            agent.id,
            "What do I drink in the morning?",
            approval_handler=lambda _request: ApprovalDecision(True),
        )
        assert "black coffee" in second.text
        system = _Handler.calls[-1]["messages"][0]["content"]
        assert "black coffee" in system
        assert "run_command" in json.dumps(_Handler.calls[0]["tools"])
    finally:
        server.shutdown()
        server.server_close()
