"""Check the live evaluation harness itself without a model or real machines."""

import pytest

from steward.core.llm.base import LLMResponse
from steward.core.models import ToolCall
from tests import test_live_models as live
from tests.fakes import ScriptedBackend


def call(name, **arguments):
    return LLMResponse("", [ToolCall("eval-call", name, arguments)])


@pytest.mark.parametrize("scenario,responses", [
    (live.test_live_selects_the_requested_machine, [
        call("read_file", path="/etc/hostname", machine="workshop"),
        LLMResponse("workshop-fixture-host"),
    ]),
    (live.test_live_investigates_a_failed_command, [
        call("run_command", command="systemctl status steward-demo", machine="workshop"),
        call("run_command", command="journalctl -u steward-demo --no-pager", machine="workshop"),
        LLMResponse("Port 8123 is already in use."),
    ]),
    (live.test_live_stops_after_denial, [
        call("run_command", command="touch /tmp/steward-evaluation-marker", machine="workshop"),
        LLMResponse("The action was denied."),
    ]),
    (live.test_live_recalls_persistent_memory_without_history, [
        call("save_note", title="Maintenance reference", content="amber-cedar-73"),
        LLMResponse("Saved."),
        LLMResponse("amber-cedar-73"),
    ]),
])
def test_model_evaluation_scenarios_with_scripted_backend(monkeypatch, scenario, responses):
    monkeypatch.setenv("STEWARD_EVAL_MODEL", "scripted")
    backend = ScriptedBackend(responses)
    monkeypatch.setattr(live, "build_backend", lambda _settings, _agent: backend)
    evaluation = live.Evaluation(monkeypatch)
    scenario(evaluation)
    if scenario is live.test_live_recalls_persistent_memory_without_history:
        assert "amber-cedar-73" in backend.seen[-1][0].content


def test_simulated_machines_never_execute_unrecognized_actions(monkeypatch, tmp_path):
    monkeypatch.setenv("STEWARD_EVAL_MODEL", "scripted")
    evaluation = live.Evaluation(monkeypatch)
    target = tmp_path / "must-not-exist"
    result = evaluation.execute("run_command", {"command": f"touch {target}", "machine": "workshop"}, None)
    assert "unavailable" in result
    assert not target.exists()
