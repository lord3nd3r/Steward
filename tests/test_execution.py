"""Exercise real pipes and child processes, without using a model server."""

import shlex
import sys
import threading
import time
from pathlib import Path

import pytest

from steward.core.execution import _MAX_BYTES, _OutputBuffer, run_local_command


def python_command(source):
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


def test_output_is_bounded_while_both_streams_are_drained():
    result = run_local_command(python_command(
        "import os; os.write(1, b'x' * 2000000); os.write(2, b'y' * 2000000)"
    ), timeout=10)
    assert result.exit_code == 0
    for text, character in ((result.stdout, "x"), (result.stderr, "y")):
        assert text.startswith(character * _MAX_BYTES)
        assert "truncated 1936000 bytes" in text
        assert len(text) < _MAX_BYTES + 100


def test_capture_does_not_keep_discarded_bytes():
    output = _OutputBuffer()
    for _ in range(1000):
        output.append(b"a" * 8192)
    assert len(output.data) == _MAX_BYTES
    assert output.discarded == 8192000 - _MAX_BYTES


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
def test_stop_kills_children_even_after_shell_exits(tmp_path, stop):
    pid_file = tmp_path / "child.pid"
    source = f"import os,time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    # Bash exits immediately, leaving a child holding the output pipes open.
    command = python_command(source) + " & exit 0"
    cancel = threading.Event()
    results = []
    worker = threading.Thread(target=lambda: results.append(
        run_local_command(command, timeout=1 if stop == "timeout" else 10, cancel=cancel)
    ))
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not pid_file.exists() or not pid_file.read_text():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        pid = int(pid_file.read_text())
        if stop == "cancel":
            cancel.set()
        worker.join(5)
        assert not worker.is_alive()
        assert results[0].exit_code == (130 if stop == "cancel" else 124)
        deadline = time.monotonic() + 2
        while True:
            try:
                state = Path(f"/proc/{pid}/stat").read_text().split(")", 1)[1].split()[0]
            except FileNotFoundError:
                break
            if state == "Z":  # Dead, awaiting reaping by the container's init.
                break
            assert time.monotonic() < deadline, "child survived Stop"
            time.sleep(0.01)
    finally:
        cancel.set()
        worker.join(5)


def test_pre_cancelled_command_never_starts(tmp_path):
    cancel = threading.Event()
    cancel.set()
    target = tmp_path / "should-not-exist"
    result = run_local_command(f"touch {shlex.quote(str(target))}", timeout=5, cancel=cancel)
    assert result.exit_code == 130
    assert not target.exists()


def test_failure_and_partial_output_are_preserved():
    result = run_local_command("printf before; printf problem >&2; exit 7", timeout=5)
    assert (result.exit_code, result.stdout, result.stderr) == (7, "before", "problem")
    result = run_local_command("printf before; sleep 10", timeout=0.1)
    assert result.exit_code == 124
    assert result.stdout == "before"
    assert "Timed out" in result.stderr
