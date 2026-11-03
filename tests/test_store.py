import pytest

from steward.core.errors import StewardError
from steward.core.models import Machine, ToolCall
from steward.core.settings import Settings
from steward.core.store import Store


def test_settings_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path / "cfg"))
    settings = Settings.load()
    assert settings.ollama_base_url.startswith("http")
    settings.ollama_base_url = "http://127.0.0.1:11435"
    settings.max_steps = 9
    settings.save()
    again = Settings.load()
    assert again.ollama_base_url == "http://127.0.0.1:11435"
    assert again.max_steps == 9
    monkeypatch.setenv("STEWARD_OLLAMA_URL", "http://example.test:1")
    assert Settings.load().ollama_base_url == "http://example.test:1"


def test_agent_and_note_lifecycle():
    store = Store()
    agent = store.create_agent("House", model="llama3.1", persona="Keeps the machines tidy.")
    assert store.get_agent("house").id == agent.id
    with pytest.raises(StewardError):
        store.create_agent("House", model="other")
    with pytest.raises(StewardError):
        store.create_agent("9bad", model="x")

    note = store.save_note(agent.id, "Coffee", "Likes black coffee in the morning.", "food")
    updated = store.save_note(agent.id, "coffee", "Likes black coffee, no sugar.", "food")
    assert updated.id == note.id
    assert "no sugar" in updated.content
    found = store.relevant_notes(agent.id, "what should I drink this morning?")
    assert any("coffee" in item.content.lower() for item in found)

    store.update_note(agent.id, note.id, title="Coffee", content="Switching to tea.", tags="food")
    assert store.get_note(agent.id, note.id).content.startswith("Switching")
    assert store.delete_note(agent.id, note.id)
    assert store.get_note(agent.id, note.id) is None


def test_messages_keep_tool_calls_and_drop_orphan_tools():
    store = Store()
    agent = store.create_agent("Workshop", model="qwen2.5")
    store.add_message(agent.id, "user", "check the disk")
    store.add_message(
        agent.id,
        "assistant",
        "Looking.",
        tool_calls=[ToolCall(id="c1", name="run_command", arguments={"command": "df -h"})],
    )
    store.add_message(agent.id, "tool", "exit_code: 0", tool_call_id="c1", name="run_command")
    store.add_message(agent.id, "assistant", "Plenty of space.")
    history = store.recent_messages(agent.id, limit=2)
    assert history[0].role != "tool"
    full = store.list_messages(agent.id)
    assert full[1].tool_calls[0].arguments["command"] == "df -h"

    store.clear_history(agent.id)
    assert store.list_messages(agent.id) == []
    store.save_note(agent.id, "Disk", "Root filesystem is the big one.")
    store.delete_agent(agent.id)
    assert store.get_agent(agent.id) is None
    assert store.list_notes(agent.id) == []


def test_machine_registry_rejects_local_and_updates():
    store = Store()
    with pytest.raises(StewardError):
        store.upsert_machine(Machine(name="local", host="x", user="me"))
    machine = store.upsert_machine(
        Machine(name="SER", host="ser.local", user="ada", port=22, sudo_notes="password required")
    )
    assert store.get_machine("ser").host == "ser.local"
    store.set_host_key_fingerprint("SER", "SHA256:abc")
    assert store.get_machine("SER").host_key_fingerprint == "SHA256:abc"
    renamed = store.upsert_machine(
        Machine(name="SER", host="10.0.0.8", user="ada", sudo_notes="password required")
    )
    assert renamed.host == "10.0.0.8"
    assert renamed.host_key_fingerprint == "SHA256:abc"
    store.delete_machine("ser")
    assert store.list_machines() == []
