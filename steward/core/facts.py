"""Cached OS, package-manager, and privilege facts for each saved machine.

Facts are gathered once per machine and reused on later turns.
``steward machines refresh`` probes again. The probe is read-only: it does not
run sudo and it does not change the host.
"""

from __future__ import annotations

import re
import shlex
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from steward.core.errors import Cancelled, StewardError
from steward.core.execution import run_local_command, run_ssh_command
from steward.core.messages import NO_MACHINES_MESSAGE
from steward.core.models import MachineFacts
from steward.core.store import Store, utcnow

PROBE_TIMEOUT = 8
PROBE_CONNECT_TIMEOUT = 5

# POSIX sh. Values are parsed in Python so a hostile os-release cannot grow the prompt.
PROBE_SCRIPT = r"""
set +e
os_id=
os_ver=
if [ -r /etc/os-release ]; then
  os_id=$(sed -n 's/^ID=//p' /etc/os-release | head -n 1)
  os_ver=$(sed -n 's/^VERSION_ID=//p' /etc/os-release | head -n 1)
fi
uid=$(id -u 2>/dev/null || printf '%s\n' '?')
user=$(id -un 2>/dev/null || printf '%s\n' '?')
pkg=
for c in apt-get dnf pacman zypper apk xbps-install yum; do
  if command -v "$c" >/dev/null 2>&1; then
    pkg=$c
    break
  fi
done
case "$pkg" in
  apt-get) pkg=apt ;;
  xbps-install) pkg=xbps ;;
esac
if [ "$uid" = 0 ]; then
  priv=root
elif command -v sudo >/dev/null 2>&1; then
  priv=sudo
elif command -v doas >/dev/null 2>&1; then
  priv=doas
else
  priv=none
fi
extras=
if command -v flatpak >/dev/null 2>&1; then
  extras=flatpak
fi
if command -v snap >/dev/null 2>&1; then
  if [ -n "$extras" ]; then
    extras=${extras},snap
  else
    extras=snap
  fi
fi
printf 'user=%s\nuid=%s\nos_id=%s\nos_version=%s\npkg=%s\nsudo=%s\nextras=%s\n' \
  "$user" "$uid" "$os_id" "$os_ver" "$pkg" "$priv" "$extras"
exit 0
"""

_PKGS = {"apt", "dnf", "yum", "pacman", "zypper", "apk", "xbps"}
_PRIV = {"root", "sudo", "doas", "none"}
_EXTRAS = {"flatpak", "snap"}
_TOKEN = re.compile(r"^[A-Za-z0-9_.:@+-]{1,40}$")

Probe = Callable[[Store, str], MachineFacts]


def probe_command() -> str:
    return "sh -c " + shlex.quote(PROBE_SCRIPT)


def fact_line(facts: MachineFacts | None) -> str:
    """One short, injection-resistant clause for the system prompt."""
    if facts is None:
        return "facts not gathered"
    os_id = _token(facts.os_id)
    os_version = _token(facts.os_version)
    user = _token(facts.user_name)
    pkg = facts.pkg.strip() if facts.pkg.strip() in _PKGS else ""
    sudo_state = facts.sudo_state.strip() if facts.sudo_state.strip() in _PRIV else ""
    extras = "+".join(part for part in facts.extras.split(",") if part.strip() in _EXTRAS)
    os_name = f"{os_id} {os_version}".strip()
    if not any((os_name, pkg, user, sudo_state, extras, facts.is_root)):
        return "facts unavailable"
    parts: list[str] = [os_name or "unknown OS"]
    if pkg:
        parts.append(pkg)
    if facts.is_root:
        parts.append("root")
    else:
        parts.append(f"user {user}" if user else "user ?")
        if sudo_state == "sudo":
            parts.append("sudo")
        elif sudo_state == "doas":
            parts.append("doas")
        elif sudo_state == "none":
            parts.append("no sudo")
    if extras:
        parts.append(extras)
    return ", ".join(parts)


def describe(facts: MachineFacts) -> str:
    """CLI/GUI line. Includes the probe error, which the system prompt omits."""
    line = fact_line(facts)
    if facts.error and line == "facts unavailable":
        detail = " ".join(facts.error.split())[:180]
        return f"{facts.machine}: facts unavailable ({detail})"
    return f"{facts.machine}: {line}"


def ensure_machine_facts(
    store: Store,
    *,
    refresh: bool = False,
    names: list[str] | None = None,
    on_status: Callable[[str], None] | None = None,
    probe: Probe | None = None,
    cancel: threading.Event | None = None,
) -> list[MachineFacts]:
    """Return cached facts, probing only the machines that are missing.

    ``refresh`` probes ``names`` again, or every saved machine.
    A failed probe is cached so the next turn does not wait on a dead host.
    """
    targets = _target_names(store, names)
    missing: list[str] = []
    cached: dict[str, MachineFacts] = {}
    for name in targets:
        current = None if refresh else store.get_facts(name)
        if current is None:
            missing.append(name)
        else:
            cached[name.casefold()] = current
    if missing:
        if on_status:
            on_status("Checking OS and privileges")
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        if probe is not None:
            probed = [probe(store, name) for name in missing]
        else:
            probed = _probe_all(store, missing, cancel)
        for item in probed:
            store.save_facts(item)
            cached[item.machine.casefold()] = item
    return [cached[name.casefold()] for name in targets if name.casefold() in cached]


def refresh_machine_facts(store: Store, name: str | None = None) -> list[MachineFacts]:
    """Re-probe one machine, or every saved machine when ``name`` is omitted."""
    if name is None and not store.list_machines():
        raise StewardError(NO_MACHINES_MESSAGE)
    if name is not None:
        _target_names(store, [name])
        return ensure_machine_facts(store, refresh=True, names=[name])
    return ensure_machine_facts(store, refresh=True)


def probe_machine(store: Store, name: str, *, cancel: threading.Event | None = None) -> MachineFacts:
    """Run the read-only probe. Does not raise for an unreachable host."""
    if cancel is not None and cancel.is_set():
        raise Cancelled()
    command = probe_command()
    machine = store.get_machine(name)
    if machine is None:
        return _failed(name, f"unknown machine {name}")
    key = machine.name
    try:
        if machine.is_local:
            result = run_local_command(command, timeout=PROBE_TIMEOUT, cancel=cancel)
        else:
            result = run_ssh_command(
                machine,
                store,
                command,
                timeout=PROBE_TIMEOUT,
                connect_timeout=PROBE_CONNECT_TIMEOUT,
                cancel=cancel,
            )
    except Cancelled:
        raise
    except Exception as exc:
        return _failed(key, str(exc))
    if cancel is not None and cancel.is_set():
        raise Cancelled()
    parsed = parse_probe(key, result.stdout)
    if parsed is None:
        detail = (result.stderr or result.stdout or f"exit {result.exit_code}").strip()
        return _failed(key, detail or "probe produced no facts")
    parsed.gathered_at = utcnow()
    return parsed


def parse_probe(machine: str, stdout: str) -> MachineFacts | None:
    """Parse probe stdout. Returns None when nothing safe could be read."""
    data: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in data:
            data[key] = value.strip().strip('"').strip("'")
    uid = data.get("uid", "")
    is_root = uid == "0"
    user = _token(data.get("user", ""))
    os_id = _token(data.get("os_id", ""))
    os_version = _token(data.get("os_version", ""))
    pkg = data.get("pkg", "").strip()
    if pkg not in _PKGS:
        pkg = ""
    sudo_state = data.get("sudo", "").strip()
    if sudo_state not in _PRIV:
        sudo_state = ""
    if is_root:
        sudo_state = "root"
    extras = ",".join(part.strip() for part in data.get("extras", "").split(",") if part.strip() in _EXTRAS)
    if not any((user, os_id, os_version, pkg, sudo_state, is_root)):
        return None
    return MachineFacts(
        machine=machine,
        user_name=user,
        is_root=is_root,
        os_id=os_id,
        os_version=os_version,
        pkg=pkg,
        sudo_state=sudo_state,
        extras=extras,
    )


def _probe_all(
    store: Store,
    names: list[str],
    cancel: threading.Event | None,
) -> list[MachineFacts]:
    if len(names) == 1:
        return [probe_machine(store, names[0], cancel=cancel)]

    def _one(name: str) -> MachineFacts:
        return probe_machine(store, name, cancel=cancel)

    results: list[MachineFacts] = []
    with ThreadPoolExecutor(max_workers=min(4, len(names))) as pool:
        futures = [pool.submit(_one, name) for name in names]
        for future in futures:
            results.append(future.result())
    return results


def _target_names(store: Store, names: list[str] | None) -> list[str]:
    if names is None:
        return [machine.name for machine in store.list_machines()]
    cleaned: list[str] = []
    for name in names:
        machine = store.get_machine(name)
        if machine is None:
            raise StewardError(f"No machine named {name}.")
        cleaned.append(machine.name)
    return cleaned


def _failed(name: str, detail: str) -> MachineFacts:
    text = " ".join(str(detail).split())[:180] or "probe failed"
    return MachineFacts(
        machine=name,
        error=text,
        gathered_at=utcnow(),
    )


def _token(value: str) -> str:
    value = (value or "").strip()
    return value if _TOKEN.fullmatch(value) else ""
