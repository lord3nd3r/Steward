import threading

from steward.core.agent import AgentRunner
from steward.core.errors import ToolUnsupportedError
from steward.core.llm.base import LLMResponse
from steward.core.models import ToolCall
from steward.core.settings import Settings
from steward.core.store import Store
from steward.core.tools import default_registry
from tests.fakes import ScriptedBackend


def _runner(store, backend, settings=None):
    return AgentRunner(
        store,
        settings or Settings(),
        default_registry(),
        lambda _settings, _agent: backend,
    )


def _approve_all(request):
    from steward.core.models import ApprovalDecision

    return ApprovalDecision(approved=True)


def _deny_all(request):
    from steward.core.models import ApprovalDecision

    return ApprovalDecision(approved=False)


def test_loop_runs_a_command_then_answers(tmp_path):
    target = tmp_path / "marker.txt"
    backend = ScriptedBackend(
        [
            LLMResponse(
                content="I'll write that down on disk.",
                tool_calls=[
                    ToolCall(
                        id="c1",
                        name="run_command",
                        arguments={"command": f"printf hi > {target}"},
                    )
                ],
            ),
            LLMResponse(content="Wrote hi."),
        ]
    )
    store = Store()
    agent = store.create_agent("House", model="scripted", approval_mode="full_auto")
    asked = []
    result = _runner(store, backend).run_turn(
        agent.id,
        "leave a marker",
        approval_handler=lambda request: asked.append(request) or _approve_all(request),
    )
    assert result.text == "Wrote hi."
    assert target.read_text() == "hi"
    assert asked == []
    assert "exit_code: 0" in backend.seen[1][-1].content
    assert any(message.role == "assistant" and message.content == "Wrote hi." for message in store.list_messages(agent.id))


def test_denied_command_does_not_run(tmp_path):
    target = tmp_path / "nope"
    backend = ScriptedBackend(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="c1", name="run_command", arguments={"command": f"touch {target}"})],
            ),
            LLMResponse(content="You said no."),
        ]
    )
    store = Store()
    agent = store.create_agent("House", model="scripted", approval_mode="ask")
    result = _runner(store, backend).run_turn(agent.id, "touch it", approval_handler=_deny_all)
    assert not target.exists()
    assert "Denied" in backend.seen[1][-1].content
    assert result.text == "You said no."


def test_destructive_always_asks_even_in_full_auto(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("safe")
    backend = ScriptedBackend(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(id="c1", name="run_command", arguments={"command": f"rm -rf {victim}"})
                ],
            ),
            LLMResponse(content="Left it alone."),
        ]
    )
    store = Store()
    agent = store.create_agent("House", model="scripted", approval_mode="full_auto")
    calls = []
    _runner(store, backend).run_turn(
        agent.id,
        "wipe the directory",
        approval_handler=lambda request: calls.append(request) or _deny_all(request),
    )
    assert calls and calls[0].destructive
    assert (victim / "keep").read_text() == "safe"


def test_notes_are_saved_and_recalled_next_turn():
    backend = ScriptedBackend(
        [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCall(
                        id="n1",
                        name="save_note",
                        arguments={"title": "Drink", "content": "Likes black coffee in the morning."},
                    )
                ],
            ),
            LLMResponse(content="I'll remember the coffee."),
            LLMResponse(content="You like black coffee in the morning."),
        ]
    )
    store = Store()
    agent = store.create_agent("House", model="scripted", approval_mode="ask")
    asked = []
    runner = _runner(store, backend)
    runner.run_turn(
        agent.id,
        "Remember that I like black coffee in the morning.",
        approval_handler=lambda request: asked.append(request) or _approve_all(request),
    )
    assert asked == []
    notes = store.list_notes(agent.id)
    assert notes and "coffee" in notes[0].content
    runner.run_turn(agent.id, "What do I drink in the morning?", approval_handler=_approve_all)
    system = backend.seen[-1][0].content
    assert "black coffee" in system
    assert "House" in system


def test_text_protocol_and_unsupported_tools():
    class Flaky(ScriptedBackend):
        def complete(self, messages, tools=None, on_token=None, cancel=None):
            if tools:
                self.tools_seen.append(tools)
                raise ToolUnsupportedError("model does not support tools")
            return super().complete(messages, tools=None, on_token=on_token, cancel=cancel)

    backend = Flaky(
        [
            LLMResponse(
                content='<tool_call>\n{"name": "save_note", "arguments": {"title": "City", "content": "Lives in Portland."}}\n</tool_call>'
            ),
            LLMResponse(content="Portland, noted."),
        ]
    )
    store = Store()
    agent = store.create_agent("House", model="scripted", approval_mode="ask", tool_protocol="auto")
    result = _runner(store, backend).run_turn(agent.id, "I live in Portland.", approval_handler=_approve_all)
    assert result.text == "Portland, noted."
    assert store.find_note_by_title(agent.id, "City").content.startswith("Lives in Portland")


def test_max_steps_stops_the_loop():
    calls = [
        LLMResponse(content="", tool_calls=[ToolCall(id=f"c{i}", name="search_notes", arguments={"query": "x"})])
        for i in range(4)
    ]
    backend = ScriptedBackend(calls)
    store = Store()
    agent = store.create_agent("House", model="scripted", max_steps=2)
    result = _runner(store, backend).run_turn(agent.id, "look", approval_handler=_approve_all)
    assert result.truncated
    assert "maximum number of tool steps" in result.text
    assert len(backend.responses) == 2


def test_cancel_between_steps():
    cancel = threading.Event()

    class Once(ScriptedBackend):
        def complete(self, messages, tools=None, on_token=None, cancel=None):
            response = super().complete(messages, tools, on_token, None)
            if cancel is not None:
                cancel.set()
            return response

    backend = Once(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="c1", name="search_notes", arguments={"query": "x"})],
            ),
            LLMResponse(content="should not be used"),
        ]
    )
    store = Store()
    agent = store.create_agent("House", model="scripted", max_steps=4)
    result = _runner(store, backend).run_turn(agent.id, "go", approval_handler=_approve_all, cancel=cancel)
    assert result.cancelled


def test_sudo_password_is_reused_for_the_rest_of_the_turn(tmp_path):
    from steward.core.models import ApprovalDecision

    backend = ScriptedBackend(
        [
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="c1", name="run_command", arguments={"command": "sudo -n true"})],
            ),
            LLMResponse(
                content="",
                tool_calls=[ToolCall(id="c2", name="run_command", arguments={"command": "sudo -n true"})],
            ),
            LLMResponse(content="done"),
        ]
    )
    store = Store()
    agent = store.create_agent("House", model="scripted", approval_mode="full_auto")
    calls = []
    settings = Settings()
    settings.command_timeout = 8

    def approve(request):
        calls.append(request)
        return ApprovalDecision(approved=True, sudo_password="temporary")

    _runner(store, backend, settings).run_turn(agent.id, "check sudo twice", approval_handler=approve)
    assert len(calls) == 1
    assert calls[0].needs_sudo
