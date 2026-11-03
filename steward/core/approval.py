"""Command risk classification and the per-agent approval policy.

Destructive commands always require a person, including in full-auto mode.
The classifier is a heuristic, not a sandbox: the approval dialog shows the
exact command that will run.
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from steward.core.models import CommandAssessment

_QUOTE_RE = re.compile(r"'(?:\\.|[^'])*'|\"(?:\\.|[^\"])*\"")
_SUB_RE = re.compile(r"\$\(([^)]*)\)|`([^`]*)`")

_SIMPLE_READONLY = {
    "ls", "cat", "head", "tail", "less", "more", "file", "stat", "du", "df", "free",
    "ps", "uptime", "whoami", "id", "uname", "hostname", "date", "pwd", "which",
    "whereis", "echo", "printf", "true", "false", "wc", "sort", "uniq", "cut", "tr",
    "column", "nl", "od", "hexdump", "xxd", "strings", "lsof", "lscpu", "lsblk",
    "lsusb", "lspci", "lsmem", "lsmod", "sensors", "cal", "printenv", "dmesg",
    "journalctl", "timedatectl", "hostnamectl", "localectl", "who", "w", "last",
    "groups", "getent", "blkid", "findmnt", "basename", "dirname", "realpath",
    "readlink", "cmp", "diff", "comm", "sha256sum", "sha1sum", "md5sum", "cksum",
    "nproc", "arch", "vmstat", "iostat", "mpstat", "pidof", "pgrep", "ss", "netstat",
    "ping", "ping6", "traceroute", "tracepath", "dig", "nslookup", "host", "rg",
    "grep", "egrep", "fgrep", "awk", "tty", "lsb_release", "seq", "tac", "rev",
    "fold", "paste", "join", "test", "[", ":", "true", "false",
    "uname", "hostnamectl", "who",
}

_WRAPPERS = {"env", "command", "time", "nice", "nohup", "stdbuf"}
_WRAPPERS_WITH_ARG = {"-n", "--adjustment", "-o", "--output", "-e", "--arg"}
_SUDO_WITH_ARG = {
    "-u", "-g", "-p", "-C", "-T", "-h", "-R", "--user", "--group", "--prompt",
    "--role", "--chdir", "--close-from", "--host",
}
_QUOTE_SAFE = {"echo", "printf", "cat", "grep", "rg", "egrep", "fgrep", "less", "more", "head", "tail"}

_GIT_READONLY = {"status", "log", "diff", "show", "branch", "remote", "rev-parse", "ls-files", "describe"}
_SYSTEMCTL_READONLY = {
    "status", "is-active", "is-enabled", "is-failed", "list-units", "list-unit-files",
    "show", "cat", "list-dependencies", "get-default",
}
_DOCKER_READONLY = {"ps", "images", "logs", "inspect", "version", "info", "top", "port"}
_APT_READONLY = {"list", "show", "search", "policy"}
_DNF_READONLY = {"list", "info", "search", "repoquery", "check-update"}


def classify_command(command: str) -> CommandAssessment:
    """Classify a shell command as readonly, mutating, or destructive."""
    text = command.strip()
    if not text:
        return CommandAssessment("mutating", False, ("empty command",))

    # Pipes are removed when the command is split into stages, so shell
    # pipelines have to be judged against the whole line first.
    structural = _structural_danger(_strip_quotes(text))
    if structural:
        return CommandAssessment("destructive", _command_needs_sudo(text), (structural,))

    level = "readonly"
    reasons: list[str] = []
    needs_sudo = False

    for stage in _split_stages(text):
        stage_level, stage_sudo, stage_reasons = _classify_stage(stage)
        needs_sudo = needs_sudo or stage_sudo
        reasons.extend(stage_reasons)
        level = _worse(level, stage_level)

    return CommandAssessment(level, needs_sudo, tuple(reasons))


def needs_human_approval(
    mode: str,
    approval_class: str,
    assessment: CommandAssessment | None,
) -> tuple[bool, str, bool]:
    """Return (required, reason, password_only).

    ``password_only`` means the action would have run on its own, but sudo
    needs a password that Steward refuses to store. The dialog still appears.
    """
    destructive = bool(assessment and assessment.destructive)
    if destructive:
        reason = "Flagged as destructive"
        if assessment and assessment.reasons:
            reason += f" ({assessment.reasons[0]})"
        reason += ". Steward always asks before running this."
        return True, reason, False

    if approval_class == "safe":
        return False, "", False

    sudo = bool(assessment and assessment.needs_sudo)
    if mode == "full_auto":
        if sudo:
            return True, "Sudo needs your password for this command. It is kept only for this turn, then forgotten.", True
        return False, "", False

    if mode == "ask":
        return True, "This agent is set to ask before every action.", False

    if mode == "auto_readonly":
        readonly = approval_class == "read" or (assessment is not None and assessment.level == "readonly")
        if readonly and not sudo:
            return False, "", False
        if readonly and sudo:
            return True, "The command is read-only, but sudo needs your password. It is not stored.", True
        return True, "This action can change something, and this agent only auto-approves reads.", False

    raise ValueError(f"Unknown approval mode: {mode}")


def path_is_sensitive(path: str, *, remote: bool = False) -> bool:
    """True for paths that should always ask, even in full-auto."""
    if not path:
        return False
    if remote:
        candidate = path
        if candidate.startswith("~/"):
            candidate = candidate[1:]
    else:
        candidate = os.path.realpath(os.path.expanduser(path))
    normalized = candidate.replace("\\", "/")
    names = {
        "/etc/passwd",
        "/etc/shadow",
        "/etc/gshadow",
        "/etc/sudoers",
    }
    if normalized in names:
        return True
    if normalized.startswith("/etc/sudoers.d/"):
        return True
    for prefix in ("/boot", "/dev", "/proc", "/sys"):
        if normalized == prefix or normalized.startswith(prefix + "/"):
            return True
    if normalized.endswith("/.ssh/authorized_keys") or normalized.endswith("/.ssh/id_rsa"):
        return True
    return False


def apply_sudo_password(command: str, password: str | None) -> tuple[str, bytes | None]:
    """Insert ``sudo -S -p ''`` so a password can be passed on stdin.

    The password is never placed in the command string. ``doas`` is left
    unchanged (run it passwordless, or use sudo).
    """
    if not password or "sudo" not in command:
        return command, None
    rewritten, count = _rewrite_sudo(command)
    if count == 0:
        return command, None
    return rewritten, f"{password}\n".encode() * count


def scrub_secret(text: str, secret: str | None) -> str:
    if secret and secret in text:
        return text.replace(secret, "***")
    return text


def _worse(left: str, right: str) -> str:
    order = {"readonly": 0, "mutating": 1, "destructive": 2}
    return left if order[left] >= order[right] else right


def _classify_stage(stage: str) -> tuple[str, bool, list[str]]:
    code = _strip_quotes(stage)
    needs_sudo = False
    reasons: list[str] = []

    structural = _structural_danger(code)
    if structural:
        return "destructive", _stage_uses_sudo(stage), [structural]

    substitution = "$(" in code or "`" in code
    if substitution:
        for inner in _SUB_RE.findall(code):
            payload = inner[0] or inner[1]
            if payload.strip():
                inner_assessment = classify_command(payload)
                if inner_assessment.destructive:
                    return "destructive", True if inner_assessment.needs_sudo else _stage_uses_sudo(stage), [
                        "destructive command inside a substitution"
                    ]
                needs_sudo = needs_sudo or inner_assessment.needs_sudo

    try:
        tokens = shlex.split(stage, posix=True)
    except ValueError:
        return "mutating", _stage_uses_sudo(stage), ["could not parse the command"]

    tokens, sudo_here = _unwrap(tokens)
    needs_sudo = needs_sudo or sudo_here
    if not tokens:
        return "mutating", needs_sudo, ["no command left after wrappers"]

    argv_hit = _argv_destructive(tokens)
    if argv_hit:
        return "destructive", needs_sudo, [argv_hit]

    head = Path(tokens[0]).name
    if head not in _QUOTE_SAFE:
        for payload in _quoted_payloads(stage):
            hit = _structural_danger(payload) or _argv_from_text(payload)
            if hit:
                return "destructive", needs_sudo, [hit]

    if substitution or _has_file_redirect(code):
        return "mutating", needs_sudo, reasons

    if _is_readonly(tokens):
        return "readonly", needs_sudo, reasons
    return "mutating", needs_sudo, reasons


def _argv_from_text(payload: str) -> str | None:
    try:
        tokens = shlex.split(payload, posix=True)
    except ValueError:
        return _structural_danger(payload)
    tokens, _ = _unwrap(tokens)
    if not tokens:
        return None
    return _argv_destructive(tokens)


def _argv_destructive(tokens: list[str]) -> str | None:
    name = Path(tokens[0]).name
    args = tokens[1:]
    if name in {
        "dd", "shred", "wipefs", "fdisk", "parted", "sgdisk", "sfdisk", "cryptsetup",
        "shutdown", "reboot", "poweroff", "halt", "mkswap", "lvremove", "vgremove",
        "pvremove", "userdel", "groupdel", "killall", "pkill", "visudo", "passwd",
    }:
        return name
    if name.startswith("mkfs"):
        return name
    if name == "rm":
        blob = " ".join(args)
        if re.search(r"(^|\s)-[A-Za-z]*[rRfF]", blob) or "--recursive" in blob or "--force" in blob:
            return "rm -r or rm -f"
        if any(a in {"/", "/*", "~", "~/*"} for a in args):
            return "rm of a root path"
    if name == "chmod":
        if any(a == "-R" or a.startswith("--recursive") for a in args):
            return "chmod -R"
        if any(a in {"777", "0777", "a+rwx"} for a in args):
            return "chmod 777"
    if name == "chown" and ("-R" in args or "--recursive" in args):
        return "chown -R"
    if name == "find" and "-delete" in args:
        return "find -delete"
    if name == "truncate":
        return "truncate"
    if name == "kill" and any(a in {"-9", "-KILL"} or a.upper() == "-KILL" for a in args):
        return "kill -9"
    if name == "git" and _git_destructive(args):
        return "git history rewrite or force push"
    if name in {"apt", "apt-get"} and any(a in {"remove", "purge", "autoremove"} for a in args):
        return "package removal"
    if name in {"dnf", "yum"} and any(a in {"remove", "erase"} for a in args):
        return "package removal"
    if name == "pacman" and any(a.startswith("-") and not a.startswith("--") and re.search(r"[RUD]", a) for a in args):
        return "pacman removal"
    if name == "systemctl" and any(a in {"mask", "poweroff", "reboot", "halt", "kexec"} for a in args):
        return "systemctl power or mask"
    if name in {"docker", "podman"}:
        if args and args[0] in {"rm", "rmi"}:
            return "container delete"
        if len(args) >= 2 and args[0] == "volume" and args[1] == "rm":
            return "volume delete"
        if len(args) >= 2 and args[0] == "system" and args[1] == "prune":
            return "docker system prune"
    if name == "kubectl" and args and args[0] == "delete":
        return "kubectl delete"
    if name == "iptables" and "-F" in args:
        return "iptables -F"
    if name == "ufw" and any(a in {"reset", "disable"} for a in args):
        return "ufw reset"
    if name == "nft" and "flush" in args:
        return "nft flush"
    if name == "zfs" and "destroy" in args:
        return "zfs destroy"
    if name == "btrfs" and "delete" in args:
        return "btrfs delete"
    if name == "git" and False:
        return None
    return None


def _git_destructive(args: list[str]) -> bool:
    index = 0
    while index < len(args) and args[index].startswith("-"):
        if args[index] in {"-C", "-c", "--git-dir", "--work-tree"} and index + 1 < len(args):
            index += 2
            continue
        index += 1
    if index >= len(args):
        return False
    sub = args[index]
    rest = args[index + 1 :]
    if sub == "reset" and "--hard" in rest:
        return True
    if sub == "clean" and any(a.startswith("-") and "f" in a[1:] for a in rest):
        return True
    if sub == "push" and ("--force" in rest or "-f" in rest or "--force-with-lease" in rest):
        return True
    return False


def _is_readonly(tokens: list[str]) -> bool:
    name = Path(tokens[0]).name
    args = tokens[1:]
    if name in _SIMPLE_READONLY:
        if name == "mount":
            return len(args) == 0 or args == ["-l"]
        if name == "find":
            blocked = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"}
            return not any(a in blocked for a in args)
        return True
    if name == "sed":
        return not any(a == "-i" or a.startswith("-i") for a in args)
    if name == "git":
        return _git_sub(args) in _GIT_READONLY
    if name == "systemctl":
        return _first_positional(args) in _SYSTEMCTL_READONLY
    if name in {"docker", "podman"}:
        return bool(args) and args[0] in _DOCKER_READONLY
    if name == "apt":
        return _first_positional(args) in _APT_READONLY
    if name == "apt-cache":
        return True
    if name == "dpkg":
        flags = {a for a in args if a.startswith("-")}
        return bool(flags) and flags <= {"-l", "-L", "-s", "-S", "--list", "--status", "--print-avail", "--compare-versions"}
    if name in {"dnf", "yum"}:
        return _first_positional(args) in _DNF_READONLY
    if name == "rpm":
        return any(a in {"-q", "--query", "-qi", "-qa"} or (a.startswith("-") and "q" in a) for a in args)
    if name == "pacman":
        return _pacman_readonly(args)
    if name == "ip":
        return _ip_readonly(args)
    if name == "nmcli":
        if not args:
            return True
        if args[0] in {"general", "radio", "device"} and "delete" not in args and "modify" not in args:
            if args[0] == "device" and any(a in {"connect", "disconnect", "reapply", "modify"} for a in args):
                return False
            return "up" not in args and "down" not in args
        if args[0] == "connection" and (len(args) == 1 or args[1] == "show"):
            return True
        return False
    if name == "mount":
        return len(args) == 0 or args == ["-l"]
    return False


def _git_sub(args: list[str]) -> str:
    index = 0
    while index < len(args) and args[index].startswith("-"):
        if args[index] in {"-C", "-c", "--git-dir", "--work-tree"} and index + 1 < len(args):
            index += 2
            continue
        index += 1
    return args[index] if index < len(args) else ""


def _first_positional(args: list[str]) -> str:
    for arg in args:
        if not arg.startswith("-"):
            return arg
    return ""


def _pacman_readonly(args: list[str]) -> bool:
    flags = "".join(a[1:] for a in args if a.startswith("-") and not a.startswith("--"))
    if re.search(r"[RUD]", flags):
        return False
    if "S" in flags and not any(letter in flags for letter in ("s", "i", "l")):
        return False
    return "Q" in flags or "s" in flags or "i" in flags or "l" in flags


def _ip_readonly(args: list[str]) -> bool:
    positionals = [a for a in args if not a.startswith("-")]
    if not positionals:
        return True
    obj = positionals[0]
    if obj not in {"addr", "address", "link", "route", "neigh", "rule", "maddr"}:
        return False
    mutating = {"add", "del", "delete", "set", "change", "replace", "flush", "append"}
    return not any(word in mutating for word in positionals[1:])


def _unwrap(tokens: list[str]) -> tuple[list[str], bool]:
    needs_sudo = False
    while tokens:
        while tokens and _is_assignment(tokens[0]):
            tokens = tokens[1:]
        if not tokens:
            break
        head = tokens[0]
        if head in {"sudo", "doas"} or Path(head).name in {"sudo", "doas"}:
            needs_sudo = True
            tokens = _consume_sudo(tokens)
            continue
        if Path(head).name in _WRAPPERS:
            tokens = _consume_wrapper(tokens)
            continue
        break
    return tokens, needs_sudo


def _consume_sudo(tokens: list[str]) -> list[str]:
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return tokens[index + 1 :]
        if token.startswith("-"):
            key = token.split("=", 1)[0]
            index += 1
            if key in _SUDO_WITH_ARG and "=" not in token and index < len(tokens):
                index += 1
            continue
        break
    return tokens[index:]


def _consume_wrapper(tokens: list[str]) -> list[str]:
    index = 1
    while index < len(tokens) and tokens[index].startswith("-"):
        token = tokens[index]
        index += 1
        key = token.split("=", 1)[0]
        if key in _WRAPPERS_WITH_ARG and "=" not in token and index < len(tokens):
            index += 1
    return tokens[index:]


def _is_assignment(token: str) -> bool:
    if token.startswith("-") or "=" not in token:
        return False
    name = token.split("=", 1)[0]
    return bool(name) and all(ch.isalnum() or ch == "_" for ch in name)


def _command_needs_sudo(command: str) -> bool:
    return any(_stage_uses_sudo(stage) for stage in _split_stages(command))


def _stage_uses_sudo(stage: str) -> bool:
    try:
        tokens = shlex.split(stage, posix=True)
    except ValueError:
        return bool(re.search(r"(^|[;&|(`])\s*(sudo|doas)\b", stage))
    _, used = _unwrap(tokens)
    return used


def _structural_danger(code: str) -> str | None:
    checks = (
        (r"\|\s*(sudo\s+)?(ba|z|da|k)?sh\b", "pipe into a shell"),
        (r"\|\s*sudo\b", "pipe into sudo"),
        (r">\s*/dev/(sd|nvme|mmcblk|hd)\w*", "redirect onto a disk"),
        (r"\bof=/dev/(sd|nvme|mmcblk|hd|zero)\w*", "write to a device path"),
        (r":\(\)\s*\{", "fork bomb"),
        (r"\bdrop\s+(table|database)\b", "SQL drop"),
    )
    for pattern, reason in checks:
        if re.search(pattern, code, re.IGNORECASE):
            return reason
    return None


def _has_file_redirect(code: str) -> bool:
    if "&>" in code:
        return True
    stripped = re.sub(r"\d*>&+\d+", "", code)
    stripped = re.sub(r">&\d+", "", stripped)
    return ">" in stripped


def _strip_quotes(text: str) -> str:
    return _QUOTE_RE.sub(" ", text)


def _quoted_payloads(text: str) -> list[str]:
    found: list[str] = []
    for match in re.finditer(r"'([^']*)'|\"([^\"]*)\"", text):
        found.append(match.group(1) if match.group(1) is not None else match.group(2) or "")
    return found


def _split_stages(command: str) -> list[str]:
    stages: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(command):
        char = command[index]
        if quote:
            buf.append(char)
            if char == quote and command[index - 1] != "\\":
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            buf.append(char)
            index += 1
            continue
        if char in {";", "\n"}:
            stages.append("".join(buf))
            buf = []
            index += 1
            continue
        if char == "|" and index + 1 < len(command) and command[index + 1] == "|":
            stages.append("".join(buf))
            buf = []
            index += 2
            continue
        if char == "|" or (char == "&" and index + 1 < len(command) and command[index + 1] == "&"):
            stages.append("".join(buf))
            buf = []
            index += 1 if char == "|" else 2
            continue
        buf.append(char)
        index += 1
    stages.append("".join(buf))
    return [stage.strip() for stage in stages if stage.strip()]


def _rewrite_sudo(command: str) -> tuple[str, int]:
    """Replace command-position ``sudo`` tokens outside quotes."""
    out: list[str] = []
    index = 0
    count = 0
    quote: str | None = None
    while index < len(command):
        char = command[index]
        if quote:
            out.append(char)
            if char == quote and command[index - 1] != "\\":
                quote = None
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            out.append(char)
            index += 1
            continue
        if command.startswith("sudo", index) and _sudo_at(command, index):
            out.append("sudo -S -p ''")
            count += 1
            index += 4
            continue
        out.append(char)
        index += 1
    return "".join(out), count


def _sudo_at(command: str, index: int) -> bool:
    end = index + 4
    if end < len(command) and (command[end].isalnum() or command[end] == "_"):
        return False
    if index > 0 and (command[index - 1].isalnum() or command[index - 1] == "_"):
        return False
    before = command[:index].rstrip()
    if before == "":
        return True
    return before.endswith((";", "|", "&", "(", "`"))
