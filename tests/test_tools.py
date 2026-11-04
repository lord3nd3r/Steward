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
    for name in (
        "run_command", "read_file", "write_file", "restore_file", "list_directory",
        "save_note", "search_notes", "forget_note",
    ):
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
    store.upsert_machine(Machine(name="box", is_local=True))
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

    missing = registry.get("run_command").execute({"command": "true", "machine": "missing"}, context)
    assert "Unknown machine" in missing


def test_write_preview_and_restore(tmp_path):
    store = Store()
    store.upsert_machine(Machine(name="box", is_local=True))
    agent = store.create_agent("Workshop", model="test")
    context = ToolContext(agent_id=agent.id, store=store, settings=Settings())
    registry = default_registry()
    write = registry.get("write_file")
    restore = registry.get("restore_file")
    target = tmp_path / "settings.ini"
    target.write_text("mode=old\n")

    preview = write.preview(
        {"path": str(target), "content": "mode=new\n"}, context
    )
    assert "-mode=old" in preview
    assert "+mode=new" in preview

    result = write.execute(
        {"path": str(target), "content": "mode=new\n"}, context
    )
    backup_id = result.split("Backup ID: ", 1)[1].split(".", 1)[0]
    assert target.read_text() == "mode=new\n"
    assert restore.classify({"backup_id": backup_id}).destructive
    assert "Restored" in restore.execute({"backup_id": backup_id}, context)
    assert target.read_text() == "mode=old\n"

    created = tmp_path / "created.txt"
    result = write.execute({"path": str(created), "content": "new\n"}, context)
    backup_id = result.split("Backup ID: ", 1)[1].split(".", 1)[0]
    assert created.exists()
    restore.execute({"backup_id": backup_id}, context)
    assert not created.exists()


def test_sensitive_write_is_destructive():
    tool = default_registry().get("write_file")
    assert tool.classify({"path": "/etc/shadow"}).destructive
    assert tool.classify({"path": "/etc/sudoers.d/local"}).destructive
    assert not tool.classify({"path": "/tmp/notes.txt"}).destructive


def test_remote_writes_require_review_in_full_auto():
    from steward.core.approval import needs_human_approval

    store = Store()
    store.upsert_machine(Machine(name="remote", host="host.example", user="ada"))
    tool = default_registry().get("write_file")
    context = ToolContext(agent_id="x", store=store, settings=Settings())
    assessment = tool.classify_with_context(
        {"path": "/tmp/alias", "machine": "remote"}, context
    )
    assert assessment.requires_review
    assert needs_human_approval("full_auto", "mutate", assessment)[0]


def test_implicit_remote_machine_writes_require_review():
    store = Store()
    store.upsert_machine(Machine(name="remote", host="host.example", user="ada"))
    tool = default_registry().get("write_file")
    context = ToolContext(agent_id="x", store=store, settings=Settings())
    assessment = tool.classify_with_context({"path": "/tmp/alias"}, context)
    assert assessment.requires_review


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
    def __init__(self, out=b"remote-ok\n", err=b""):
        self.out = out
        self.err = err

    def shutdown_write(self):
        pass

    def recv_ready(self):
        return bool(self.out)

    def recv_stderr_ready(self):
        return bool(self.err)

    def recv(self, n):
        data, self.out = self.out[:n], self.out[n:]
        return data

    def recv_stderr(self, n):
        data, self.err = self.err[:n], self.err[n:]
        return data

    def exit_status_ready(self):
        return True

    def recv_exit_status(self):
        return 0


class _Stdin:
    def __init__(self):
        self.buf = b""
        self.channel = _Channel()

    def write(self, data):
        self.buf += data

    def flush(self):
        pass


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
        self.host_key_policy = None
        self.closed = False

    def load_system_host_keys(self):
        pass

    def load_host_keys(self, _path):
        pass

    def set_missing_host_key_policy(self, policy):
        self.host_key_policy = policy

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
    assert isinstance(client.host_key_policy, paramiko.RejectPolicy)
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


def test_ssh_drains_both_streams_with_bounded_output(monkeypatch):
    client = _Client()
    channel = _Channel(out=b"x" * 200000, err=b"y" * 200000)

    def execute(command, timeout=None):
        out, err = _Reader(b""), _Reader(b"")
        out.channel = err.channel = channel
        return client.stdin, out, err

    client.exec_command = execute
    monkeypatch.setattr(paramiko, "SSHClient", lambda: client)
    store = Store()
    machine = store.upsert_machine(Machine(name="SER", host="ser.local", user="ada"))
    result = run_ssh_command(machine, store, "noisy-command", timeout=5)
    assert result.exit_code == 0
    assert result.stdout.startswith("x" * 64000)
    assert result.stderr.startswith("y" * 64000)
    assert "truncated 136000 bytes" in result.stdout
    assert "truncated 136000 bytes" in result.stderr
    assert client.closed


def test_unknown_ssh_host_key_is_rejected_before_command(monkeypatch):
    client = _Client()

    def connect(**_kwargs):
        client.host_key_policy.missing_host_key(client, "host.example", _Key())

    client.connect = connect
    monkeypatch.setattr(paramiko, "SSHClient", lambda: client)
    store = Store()
    machine = store.upsert_machine(Machine(name="remote", host="host.example", user="ada"))
    with pytest.raises(Exception, match="not verified"):
        run_ssh_command(machine, store, "touch /tmp/not-run", timeout=5)
    assert client.commands == []
