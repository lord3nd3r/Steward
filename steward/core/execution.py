"""Local subprocesses and SSH sessions.

Sudo passwords are accepted only as bytes on stdin via ``sudo -S``. They are
not written into the command string, the database, or notes.
"""

from __future__ import annotations

import hashlib
import shlex
import socket
import subprocess
import threading
import time
from pathlib import Path

import paramiko

from steward.core.approval import apply_sudo_password, scrub_secret
from steward.core.errors import StewardError
from steward.core.models import CommandResult, Machine
from steward.core.store import Store

_MAX_BYTES = 64_000


def run_local_command(
    command: str,
    *,
    timeout: int,
    cwd: str | None = None,
    sudo_password: str | None = None,
    cancel: threading.Event | None = None,
) -> CommandResult:
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
        )
    except FileNotFoundError as exc:
        return CommandResult(127, "", f"Could not start bash: {exc}")
    except OSError as exc:
        return CommandResult(1, "", str(exc))

    holder: dict[str, object] = {}

    def _wait() -> None:
        try:
            out, err = proc.communicate(stdin_bytes, timeout=timeout)
            holder["out"] = (proc.returncode, out, err)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, err = proc.communicate()
            holder["out"] = (124, out, err, True)
        except Exception as exc:  # pragma: no cover - defensive
            holder["error"] = exc

    thread = threading.Thread(target=_wait, daemon=True)
    thread.start()
    while thread.is_alive():
        if cancel is not None and cancel.is_set():
            proc.kill()
            thread.join(timeout=2)
            out, err = _take_output(holder)
            return _result(130, out, (err + "\nCancelled.").strip(), sudo_password)
        thread.join(0.05)

    if "error" in holder:
        return CommandResult(1, "", scrub_secret(str(holder["error"]), sudo_password))
    payload = holder.get("out")
    if not isinstance(payload, tuple):
        return CommandResult(1, "", "The command ended without output.")
    timed_out = len(payload) == 4
    code, out, err = payload[0], payload[1], payload[2]
    stdout = _decode(out if isinstance(out, bytes) else b"")
    stderr = _decode(err if isinstance(err, bytes) else b"")
    if timed_out:
        stderr = (stderr + f"\nTimed out after {timeout}s.").strip()
    return _result(int(code) if code is not None else 1, stdout, stderr, sudo_password)


def run_ssh_command(
    machine: Machine,
    store: Store,
    command: str,
    *,
    timeout: int,
    cwd: str | None = None,
    sudo_password: str | None = None,
    cancel: threading.Event | None = None,
) -> CommandResult:
    command_to_run = command
    if cwd:
        command_to_run = f"cd {shlex.quote(cwd)} && {command_to_run}"
    command_to_run, stdin_bytes = apply_sudo_password(command_to_run, sudo_password)
    client = connect_ssh(machine, store)
    try:
        stdin, stdout, stderr = client.exec_command(command_to_run, timeout=timeout)
        if stdin_bytes and stdin is not None:
            stdin.write(stdin_bytes)
        try:
            stdin.channel.shutdown_write()
        except Exception:
            pass
        holder: dict[str, object] = {}

        def _read() -> None:
            try:
                out = stdout.read()
                err = stderr.read()
                code = stdout.channel.recv_exit_status()
                holder["out"] = (code, out, err)
            except Exception as exc:
                holder["error"] = exc

        thread = threading.Thread(target=_read, daemon=True)
        thread.start()
        start = time.monotonic()
        while thread.is_alive():
            if cancel is not None and cancel.is_set():
                client.close()
                thread.join(timeout=1)
                return CommandResult(130, "", "Cancelled.")
            if time.monotonic() - start > timeout:
                client.close()
                thread.join(timeout=1)
                return CommandResult(124, "", f"Timed out after {timeout}s.")
            thread.join(0.05)
        if "error" in holder:
            raise StewardError(f"SSH command on {machine.name} failed: {holder['error']}")
        code, out, err = holder["out"]  # type: ignore[misc]
        return _result(int(code), _decode(out), _decode(err), sudo_password)
    finally:
        client.close()


def connect_ssh(machine: Machine, store: Store) -> paramiko.SSHClient:
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
        "timeout": 15,
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


def format_command_result(result: CommandResult) -> str:
    parts = [f"exit_code: {result.exit_code}"]
    parts.append("stdout:\n" + result.stdout if result.stdout else "stdout: (empty)")
    if result.stderr:
        parts.append("stderr:\n" + result.stderr)
    return "\n".join(parts)


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


def _take_output(holder: dict[str, object]) -> tuple[str, str]:
    payload = holder.get("out")
    if isinstance(payload, tuple) and len(payload) >= 3:
        out = payload[1] if isinstance(payload[1], bytes) else b""
        err = payload[2] if isinstance(payload[2], bytes) else b""
        return _decode(out), _decode(err)
    return "", ""
