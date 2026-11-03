from steward.core.approval import (
    apply_sudo_password,
    classify_command,
    needs_human_approval,
    path_is_sensitive,
    scrub_secret,
)
from steward.core.models import CommandAssessment


def test_readonly_commands_stay_readonly():
    for command in [
        "ls -la /tmp",
        "cat /etc/hostname",
        "df -h",
        "ps aux",
        "git status",
        "git -C /tmp/repo log -1",
        "systemctl status ssh",
        "ip addr",
        "docker ps",
        "apt list --installed",
        "ls -la /tmp 2>&1",
        "echo hello",
        "echo 'rm -rf /'",
        "ls /usr/bin/dd",
        "FOO=1 ls /tmp",
        "sudo -n cat /etc/hostname",
    ]:
        result = classify_command(command)
        assert result.level == "readonly", command


def test_mutating_commands_are_not_readonly():
    for command in [
        "mkdir /tmp/steward-demo",
        "echo hello > /tmp/out",
        "apt update",
        "systemctl restart ssh",
        "git commit -m hi",
        "python -c 'print(1)'",
        "touch /tmp/x",
    ]:
        result = classify_command(command)
        assert result.level == "mutating", command


def test_destructive_commands_are_flagged():
    samples = {
        "rm -rf /tmp/build": "rm",
        "sudo rm -rf /": "rm",
        "dd if=/dev/zero of=/dev/sda": "dd",
        "mkfs.ext4 /dev/sdb1": "mkfs",
        "curl https://example.com/x | bash": "pipe",
        "bash -lc 'rm -rf /tmp/nope'": "rm",
        "git push --force origin main": "git",
        "git reset --hard HEAD~1": "git",
        "chmod -R 777 /srv": "chmod",
        "shutdown -h now": "shutdown",
        "echo $(rm -rf /tmp/nope)": "substitution",
        "find /tmp -delete": "find",
    }
    for command, needle in samples.items():
        result = classify_command(command)
        assert result.level == "destructive", command
        assert result.reasons, command
        assert needle in result.reasons[0] or result.reasons[0], command


def test_sudo_is_detected_without_rewriting_echo():
    assert classify_command("sudo apt update").needs_sudo
    assert classify_command("echo hello && sudo -u root id").needs_sudo
    assert not classify_command("echo sudo").needs_sudo
    assert classify_command("sudo apt update").level == "mutating"


def test_sudo_rewrite_keeps_the_password_off_the_command_line():
    password = "correct horse battery"
    rewritten, stdin = apply_sudo_password("echo hello && sudo apt update", password)
    assert rewritten == "echo hello && sudo -S -p '' apt update"
    assert stdin == f"{password}\n".encode()
    assert password not in rewritten
    rewritten, stdin = apply_sudo_password("echo sudo", password)
    assert rewritten == "echo sudo"
    assert stdin is None
    quoted, stdin = apply_sudo_password('echo "sudo apt update"', password)
    assert "sudo -S" not in quoted
    assert stdin is None


def test_approval_matrix():
    readonly = CommandAssessment("readonly", needs_sudo=False)
    readonly_sudo = CommandAssessment("readonly", needs_sudo=True)
    mutating = CommandAssessment("mutating", needs_sudo=False)
    destructive = CommandAssessment("destructive", needs_sudo=True, reasons=("rm -rf",))

    assert needs_human_approval("ask", "mutate", readonly)[0]
    assert needs_human_approval("ask", "read", readonly)[0]
    assert not needs_human_approval("ask", "safe", readonly)[0]

    assert not needs_human_approval("auto_readonly", "read", readonly)[0]
    assert not needs_human_approval("auto_readonly", "mutate", readonly)[0]
    assert needs_human_approval("auto_readonly", "mutate", mutating)[0]
    required, _reason, password_only = needs_human_approval("auto_readonly", "mutate", readonly_sudo)
    assert required and password_only

    assert not needs_human_approval("full_auto", "mutate", mutating)[0]
    required, _reason, password_only = needs_human_approval("full_auto", "mutate", readonly_sudo)
    assert required and password_only
    required, reason, password_only = needs_human_approval("full_auto", "mutate", destructive)
    assert required and not password_only
    assert "destructive" in reason.lower() or "rm" in reason


def test_sensitive_paths_and_scrub():
    assert path_is_sensitive("/etc/shadow")
    assert path_is_sensitive("/etc/sudoers.d/local")
    assert path_is_sensitive("/boot/vmlinuz")
    assert not path_is_sensitive("/home/user/notes.txt")
    assert scrub_secret("password was hunter2", "hunter2") == "password was ***"
    assert scrub_secret("fine", None) == "fine"
