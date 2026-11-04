"""Local subprocesses and SSH sessions.

Sudo passwords are accepted only as bytes on stdin via ``sudo -S``. They are
not written into the command string, the database, or notes.
"""

from __future__ import annotations

import hashlib
import os
import selectors
import re
import shlex
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

import paramiko

from steward.core.approval import apply_sudo_password, classify_command, scrub_secret
from steward.core.errors import StewardError
from steward.core.models import CommandResult, Machine
from steward.core.store import Store

_MAX_BYTES = 64_000

_PERMISSION_RE = re.compile(
    r"permission denied"
    r"|operation not permitted"
    r"|superuser privileges"
    r"|requires? superuser"
    r"|must be (?:the )?root"
    r"|need(?:s|ed)? to be root"
    r"|you must be root"
    r"|unless you are root"
    r"|are you root\?"
    r"|root privileges are required"
    r"|could not open lock file"
    r"|unable to lock directory"
    r"|could not get lock",
    re.IGNORECASE,
)
_SUDO_PASSWORD_RE = re.compile(
    r"a password is required"
    r"|a terminal is required"
    r"|no password was provided"
    r"|sorry, try again"
    r"|incorrect password"
    r"|authentication failure",
    re.IGNORECASE,
)
_SUDO_REJECTED_RE = re.compile(
    r"not in the sudoers|not allowed to execute|is not allowed to run",
    re.IGNORECASE,
)
_SUDO_HINT = (
    "Hint: permission denied. Retry once with sudo or doas if this needs root. "
    "Steward will ask for approval and the password. Do not paste a password."
)
_SUDO_PASSWORD_HINT = (
    "Hint: sudo needs a password. Retry the same command so Steward can prompt again. "
    "Do not paste a password."
)
_SUDO_NONINTERACTIVE_HINT = (
    "Hint: sudo -n cannot prompt. Retry the same command without -n. "
    "Steward will ask for approval and the password. Do not paste a password."
)
_SUDO_REJECTED_HINT = "Hint: sudo was rejected. Do not retry it. Tell the user what failed."


def run_local_command(
    command: str,
    *,
    timeout: int,
    cwd: str | None = None,
    sudo_password: str | None = None,
    cancel: threading.Event | None = None,
) -> CommandResult:
    if cancel is not None and cancel.is_set():
        return CommandResult(130, "", "Cancelled.")
    command_to_run, stdin_bytes = apply_sudo_password(command, sudo_password)
    try:
        proc = subprocess.Popen(
            command_to_run,
            shell=True,
            executable="/bin/bash",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd or None,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        return CommandResult(127, "", f"Could not start bash: {exc}")
    except OSError as exc:
        return CommandResult(1, "", str(exc))

    out, err = _OutputBuffer(), _OutputBuffer()
    deadline = time.monotonic() + timeout
    notice = ""
    code = None
    pending = memoryview(stdin_bytes or b"")
    try:
        with selectors.DefaultSelector() as selector:
            for stream, buffer in ((proc.stdout, out), (proc.stderr, err)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, buffer)
            if pending:
                os.set_blocking(proc.stdin.fileno(), False)
                selector.register(proc.stdin, selectors.EVENT_WRITE, None)
            else:
                proc.stdin.close()
            while selector.get_map() or proc.poll() is None:
                if cancel is not None and cancel.is_set():
                    code, notice = 130, "Cancelled."
                    break
                if time.monotonic() >= deadline:
                    code, notice = 124, f"Timed out after {timeout}s."
                    break
                for key, _ in selector.select(timeout=0.05):
                    if key.fileobj is proc.stdin:
                        try:
                            written = os.write(key.fd, pending)
                            pending = pending[written:]
                        except BrokenPipeError:
                            pending = pending[:0]
                        if not pending:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                    else:
                        chunk = os.read(key.fd, 8192)
                        if chunk:
                            key.data.append(chunk)
                        else:
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
            if code is not None:
                _kill_process_group(proc)
                # Drain bytes already in the pipes without waiting for a
                # descendant that deliberately escaped the process group.
                for key in list(selector.get_map().values()):
                    if key.fileobj is proc.stdin:
                        continue
                    for _ in range(16):
                        try:
                            chunk = os.read(key.fd, 8192)
                        except BlockingIOError:
                            break
                        if not chunk:
                            break
                        key.data.append(chunk)
        proc.wait()
        return _result(
            proc.returncode if code is None else code,
            out.text(),
            (err.text() + "\n" + notice).strip(),
            sudo_password,
        )
    finally:
        if proc.poll() is None:
            _kill_process_group(proc)
            proc.wait()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()


def _kill_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class _OutputBuffer:
    """Keep a bounded prefix while continuing to drain the producer."""

    def __init__(self) -> None:
        self.data = bytearray()
        self.discarded = 0

    def append(self, chunk: bytes) -> None:
        keep = min(len(chunk), _MAX_BYTES - len(self.data))
        self.data.extend(chunk[:keep])
        self.discarded += len(chunk) - keep

    def text(self) -> str:
        text = _decode(bytes(self.data))
        if self.discarded:
            text += f"\n...[truncated {self.discarded} bytes]"
        return text


def run_ssh_command(
    machine: Machine,
    store: Store,
    command: str,
    *,
    timeout: int,
    cwd: str | None = None,
    sudo_password: str | None = None,
    cancel: threading.Event | None = None,
    connect_timeout: int | None = None,
) -> CommandResult:
    if cancel is not None and cancel.is_set():
        return CommandResult(130, "", "Cancelled.")
    command_to_run = command
    if cwd:
        command_to_run = f"cd {shlex.quote(cwd)} && {command_to_run}"
    command_to_run, stdin_bytes = apply_sudo_password(command_to_run, sudo_password)
    client = connect_ssh(
        machine,
        store,
        timeout=15 if connect_timeout is None else connect_timeout,
    )
    try:
        stdin, stdout, stderr = client.exec_command(command_to_run, timeout=timeout)
        if stdin_bytes:
            stdin.write(stdin_bytes)
            stdin.flush()
        channel = stdout.channel
        channel.shutdown_write()
        out, err = _OutputBuffer(), _OutputBuffer()
        deadline = time.monotonic() + timeout
        while True:
            if cancel is not None and cancel.is_set():
                return _result(130, out.text(), (err.text() + "\nCancelled.").strip(), sudo_password)
            if time.monotonic() >= deadline:
                return _result(124, out.text(), (err.text() + f"\nTimed out after {timeout}s.").strip(), sudo_password)
            # Drain both SSH streams on every pass. Reading stdout to EOF
            # first can deadlock when stderr fills the SSH window.
            received = False
            if channel.recv_ready():
                out.append(channel.recv(8192))
                received = True
            if channel.recv_stderr_ready():
                err.append(channel.recv_stderr(8192))
                received = True
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                return _result(channel.recv_exit_status(), out.text(), err.text(), sudo_password)
            if not received:
                time.sleep(0.02)
    finally:
        client.close()


def connect_ssh(machine: Machine, store: Store, *, timeout: int = 15) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    known_hosts = Path.home() / ".ssh" / "known_hosts"
    if known_hosts.exists():
        try:
            client.load_host_keys(str(known_hosts))
        except OSError:
            pass

    class _AcceptOnce(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, client: paramiko.SSHClient, hostname: str, key: paramiko.PKey) -> None:
            client.get_host_keys().add(hostname, key.get_name(), key)

    client.set_missing_host_key_policy(_AcceptOnce())
    kwargs: dict[str, object] = {
        "hostname": machine.host,
        "port": machine.port,
        "username": machine.user,
        "timeout": timeout,
        "auth_timeout": 20,
        "banner_timeout": 15,
        "allow_agent": machine.use_agent,
        "look_for_keys": not bool(machine.identity_file),
    }
    if machine.identity_file:
        kwargs["pkey"] = load_private_key(machine.identity_file)
    try:
        client.connect(**kwargs)  # type: ignore[arg-type]
    except paramiko.BadHostKeyException as exc:
        client.close()
        raise StewardError(
            f"Host key for {machine.name} ({machine.host}) does not match ~/.ssh/known_hosts. "
            "Refusing to connect. If this change was expected, update known_hosts and clear the "
            "fingerprint stored for this machine."
        ) from exc
    except paramiko.AuthenticationException as exc:
        client.close()
        raise StewardError(
            f"SSH authentication failed for {machine.user}@{machine.host}. "
            "Check the user, the identity file, and ssh-agent."
        ) from exc
    except paramiko.PasswordRequiredException as exc:
        client.close()
        raise StewardError(
            "That SSH key is encrypted. Add it to ssh-agent with `ssh-add`, "
            "or use a key without a passphrase. Steward does not store passphrases."
        ) from exc
    except paramiko.SSHException as exc:
        client.close()
        raise StewardError(f"SSH error talking to {machine.name}: {exc}") from exc
    except (OSError, socket.error) as exc:
        client.close()
        raise StewardError(f"Could not reach {machine.user}@{machine.host}:{machine.port} ({exc})") from exc

    transport = client.get_transport()
    if transport is None:
        client.close()
        raise StewardError(f"SSH transport to {machine.name} did not start.")
    remote_key = transport.get_remote_server_key()
    digest = fingerprint_of(remote_key)
    if machine.host_key_fingerprint and machine.host_key_fingerprint != digest:
        client.close()
        raise StewardError(
            f"Host key for {machine.name} changed "
            f"(stored {machine.host_key_fingerprint}, saw {digest}). Refusing to connect."
        )
    if machine.host_key_fingerprint != digest:
        store.set_host_key_fingerprint(machine.name, digest)
        machine.host_key_fingerprint = digest
    return client


def load_private_key(path: str) -> paramiko.PKey:
    expanded = str(Path(path).expanduser())
    errors: list[str] = []
    for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return cls.from_private_key_file(expanded)
        except paramiko.PasswordRequiredException:
            raise
        except Exception as exc:
            errors.append(str(exc))
    detail = errors[-1] if errors else "unrecognized key"
    raise StewardError(f"Could not read the SSH key {expanded}: {detail}")


def fingerprint_of(key: paramiko.PKey) -> str:
    return "SHA256:" + hashlib.sha256(key.asbytes()).hexdigest()


def remote_home(client: paramiko.SSHClient) -> str:
    _stdin, stdout, _stderr = client.exec_command('printf %s "$HOME"')
    home = stdout.read().decode(errors="replace").strip()
    if stdout.channel.recv_exit_status() != 0 or not home.startswith("/"):
        raise StewardError("Could not resolve the remote home directory.")
    return home


def read_remote_bytes(machine: Machine, store: Store, path: str, limit: int) -> bytes:
    client = connect_ssh(machine, store)
    try:
        sftp = client.open_sftp()
        try:
            target = _remote_path(client, sftp, path)
            with sftp.open(target, "rb") as handle:
                return handle.read(limit)
        finally:
            sftp.close()
    finally:
        client.close()


def write_remote_bytes(machine: Machine, store: Store, path: str, data: bytes, mkdirs: bool) -> str:
    client = connect_ssh(machine, store)
    try:
        sftp = client.open_sftp()
        try:
            target = _remote_path(client, sftp, path)
            if mkdirs:
                _sftp_mkdirs(sftp, str(Path(target).parent))
            with sftp.open(target, "wb") as handle:
                handle.write(data)
            return target
        finally:
            sftp.close()
    finally:
        client.close()


def list_remote_directory(machine: Machine, store: Store, path: str) -> list[tuple[str, str, int]]:
    client = connect_ssh(machine, store)
    try:
        sftp = client.open_sftp()
        try:
            target = _remote_path(client, sftp, path)
            entries = []
            for attr in sftp.listdir_attr(target):
                kind = "dir" if _is_dir_mode(attr.st_mode) else "file"
                entries.append((attr.filename, kind, int(attr.st_size or 0)))
            return entries
        finally:
            sftp.close()
    finally:
        client.close()


def format_command_result(result: CommandResult, command: str | None = None) -> str:
    parts = [f"exit_code: {result.exit_code}"]
    parts.append("stdout:\n" + result.stdout if result.stdout else "stdout: (empty)")
    if result.stderr:
        parts.append("stderr:\n" + result.stderr)
    if command:
        hint = sudo_retry_hint(command, result)
        if hint:
            parts.append(hint)
    return "\n".join(parts)


def sudo_retry_hint(command: str, result: CommandResult) -> str | None:
    """Tell a small model how to recover from a privilege failure.

    The hint is text in the tool result. The next command still goes through
    approval, and Steward still refuses to store the sudo password.
    """
    if result.exit_code == 0:
        return None
    blob = f"{result.stderr}\n{result.stdout}"
    privileged = classify_command(command).needs_sudo
    if privileged and _SUDO_PASSWORD_RE.search(blob):
        if re.search(r"\bsudo\s+(?:-n|--non-interactive)\b", command):
            return _SUDO_NONINTERACTIVE_HINT
        return _SUDO_PASSWORD_HINT
    if privileged and _SUDO_REJECTED_RE.search(blob):
        return _SUDO_REJECTED_HINT
    if not privileged and _PERMISSION_RE.search(blob):
        return _SUDO_HINT
    return None


def _remote_path(client: paramiko.SSHClient, sftp: paramiko.SFTPClient, path: str) -> str:
    if path == "~" or path.startswith("~/"):
        home = remote_home(client)
        suffix = path[1:] if path != "~" else ""
        return home + suffix
    return path


def _sftp_mkdirs(sftp: paramiko.SFTPClient, directory: str) -> None:
    if not directory or directory == "/":
        return
    parts = [part for part in directory.split("/") if part]
    current = "/" if directory.startswith("/") else ""
    for part in parts:
        current = f"{current.rstrip('/')}/{part}"
        try:
            sftp.stat(current)
        except OSError:
            try:
                sftp.mkdir(current)
            except OSError:
                pass


def _is_dir_mode(mode: int | None) -> bool:
    if mode is None:
        return False
    import stat

    return stat.S_ISDIR(mode)


def _decode(data: bytes) -> str:
    if b"\x00" in data[:1024]:
        return f"(binary output, {len(data)} bytes, not shown)"
    text = data.decode("utf-8", errors="replace")
    if len(text) <= _MAX_BYTES:
        return text.rstrip("\n")
    extra = len(text) - _MAX_BYTES
    return text[:_MAX_BYTES] + f"\n...[truncated {extra} characters]"


def _result(code: int, stdout: str, stderr: str, secret: str | None) -> CommandResult:
    return CommandResult(code, scrub_secret(stdout, secret), scrub_secret(stderr, secret))
