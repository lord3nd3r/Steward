from pathlib import Path

import paramiko
import pytest

from steward.core.approval import apply_sudo_password
from steward.core.execution import run_local_command, run_ssh_command
from steward.core.models import Machine
from steward.core.store import Store
from steward.core.tools import default_registry
from steward.core.tools.base import Tool, ToolContext
from steward.core.settings import Settings


def test_builtin_registry_lists_the_core_tools():
    registry = default_registry()
    for name in ("run_command", "read_file", "write_file", "list_directory", "save_note", "search_notes", "forget_note"):
        assert registry.get(name) is not None
    assert any(schema["function"]["name"] == "run_command" for schema in registry.schemas())


def test_custom_tool_can_be_registered():
    class Ping(Tool):
        name = "ping_tool"
        description = "Reply pong."
        parameters = {"type": "object", "properties": {}}
        approval_class = "safe"

        def execute(self, arguments, context):
            return "pong"

    registry = default_registry()
    registry.register(Ping())
    assert registry.get("ping_tool").execute({}, None) == "pong"


def test_local_command_and_files(tmp_path):
    result = run_local_command("echo hello-steward", timeout=10)
    assert result.exit_code == 0
    assert "hello-steward" in result.stdout

    secret = "not-the-output"
    rewritten, stdin = apply_sudo_password("echo hello-steward", secret)
    assert secret not in rewritten
    assert stdin is None

    registry = default_registry()
    store = Store()
    context = ToolContext(agent_id="x", store=store, settings=Settings())
    folder = tmp_path / "notes"
    text = registry.get("run_command").execute(
        {"command": "echo from-tool", "cwd": str(tmp_path)},
        context,
    )
    assert "from-tool" in text
    assert "exit_code: 0" in text

    written = registry.get("write_file").execute(
        {"path": str(folder / "a.txt"), "content": "alpha"},
        context,
    )
    assert "Wrote" in written
    assert (folder / "a.txt").read_text() == "alpha"
    read = registry.get("read_file").execute({"path": str(folder / "a.txt")}, context)
    assert read == "alpha"
    listing = registry.get("list_directory").execute({"path": str(folder)}, context)
    assert "a.txt" in listing

    missing = registry.get("run_command").execute({"command": "true", "machine": "SER"}, context)
    assert "unknown machine" in missing


def test_sensitive_write_is_destructive():
    tool = default_registry().get("write_file")
    assert tool.classify({"path": "/etc/shadow"}).destructive
    assert tool.classify({"path": "/etc/sudoers.d/local"}).destructive
    assert not tool.classify({"path": "/tmp/notes.txt"}).destructive


def test_shell_classify_does_not_run_the_command(tmp_path):
    registry = default_registry()
    target = tmp_path / "victim"
    assessment = registry.get("run_command").classify({"command": f"rm -rf {target}"})
    assert assessment.destructive
    assert not target.exists()


class _Key:
    def asbytes(self):
        return b"host-key-bytes"

    def get_name(self):
        return "ssh-ed25519"


class _Transport:
    def get_remote_server_key(self):
        return _Key()


class _Channel:
    def shutdown_write(self):
        pass

    def recv_exit_status(self):
        return 0


class _Stdin:
    def __init__(self):
        self.buf = b""
        self.channel = _Channel()

    def write(self, data):
        self.buf += data


class _Reader:
    def __init__(self, data: bytes):
        self._data = data
        self.channel = _Channel()

    def read(self, _n=None):
        return self._data


class _Client:
    def __init__(self):
        self.commands = []
        self.stdin = _Stdin()
        self.kwargs = {}
        self.closed = False

    def load_system_host_keys(self):
        pass

    def load_host_keys(self, _path):
        pass

    def set_missing_host_key_policy(self, _policy):
        pass

    def connect(self, **kwargs):
        self.kwargs = kwargs

    def exec_command(self, command, timeout=None):
        self.commands.append(command)
        return self.stdin, _Reader(b"remote-ok\n"), _Reader(b"")

    def get_transport(self):
        return _Transport()

    def close(self):
        self.closed = True


def test_ssh_command_pins_host_key_and_hides_the_password(monkeypatch):
    client = _Client()
    monkeypatch.setattr(paramiko, "SSHClient", lambda: client)
    store = Store()
    machine = store.upsert_machine(Machine(name="SER", host="ser.local", user="ada"))
    result = run_ssh_command(
        machine,
        store,
        "sudo apt update",
        timeout=15,
        sudo_password="s3cret-value",
    )
    assert result.exit_code == 0
    assert "remote-ok" in result.stdout
    assert client.commands == ["sudo -S -p '' apt update"]
    assert client.stdin.buf == b"s3cret-value\n"
    assert "s3cret-value" not in client.commands[0]
    assert "s3cret-value" not in result.stdout
    pinned = store.get_machine("SER")
    assert pinned.host_key_fingerprint.startswith("SHA256:")

    machine = store.get_machine("SER")
    machine.host_key_fingerprint = "SHA256:different"
    with pytest.raises(Exception, match="Host key"):
        run_ssh_command(machine, store, "true", timeout=5)
