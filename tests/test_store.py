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


def test_fresh_store_has_no_agents_or_machines():
    store = Store()
    assert store.list_agents() == []
    assert store.list_machines() == []
    again = Store(store.db_path)
    assert again.list_agents() == []
    assert again.list_machines() == []


def test_machine_registry_requires_an_explicit_target_and_updates():
    from steward.core.models import MachineFacts

    store = Store()
    here = store.upsert_machine(Machine(name="box", is_local=True))
    assert here.is_local
    assert here.host == ""
    with pytest.raises(StewardError, match="already saved"):
        store.upsert_machine(Machine(name="other", is_local=True))
    machine = store.upsert_machine(
        Machine(name="host", host="host.example", user="user", port=22, sudo_notes="password required")
    )
    assert store.get_machine("host").host == "host.example"
    assert not machine.is_local
    store.set_host_key_fingerprint("host", "SHA256:abc")
    store.save_facts(MachineFacts(machine="host", os_id="debian", os_version="12", pkg="apt", sudo_state="sudo", user_name="user"))
    updated = store.upsert_machine(
        Machine(name="host", host="10.0.0.8", user="user", sudo_notes="password required")
    )
    assert updated.host == "10.0.0.8"
    assert updated.host_key_fingerprint == "SHA256:abc"
    assert store.get_facts("host") is None
    kept = store.upsert_machine(
        Machine(name="host", host="10.0.0.8", user="user", sudo_notes="still password")
    )
    assert kept.sudo_notes == "still password"
    store.save_facts(MachineFacts(machine="host", os_id="debian", os_version="12", pkg="apt", sudo_state="sudo", user_name="user"))
    store.delete_machine("host")
    assert store.get_facts("host") is None
    store.delete_machine("box")
    assert store.list_machines() == []


def test_old_database_is_not_given_a_machine(tmp_path):
    import sqlite3

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE machines (
            name TEXT PRIMARY KEY,
            host TEXT NOT NULL,
            user TEXT NOT NULL,
            port INTEGER NOT NULL,
            identity_file TEXT,
            sudo_notes TEXT NOT NULL DEFAULT '',
            use_agent INTEGER NOT NULL DEFAULT 1,
            host_key_fingerprint TEXT NOT NULL DEFAULT ''
        )
        """
    )
    conn.execute("INSERT INTO machines (name, host, user, port) VALUES ('host', 'host.example', 'user', 22)")
    conn.commit()
    conn.close()
    store = Store(db)
    assert [item.name for item in store.list_agents()] == []
    machines = store.list_machines()
    assert [item.name for item in machines] == ["host"]
    assert machines[0].is_local is False
