"""Render the Steward window offscreen for the README and review."""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("STEWARD_CONFIG_DIR", "/tmp/steward-shot-config")
os.environ.setdefault("STEWARD_DATA_DIR", "/tmp/steward-shot-data")
os.environ.setdefault("STEWARD_DISABLE_PLUGINS", "1")

from PySide6.QtWidgets import QApplication

from steward.core.models import ApprovalRequest, Machine
from steward.core.service import Steward
from steward.gui.dialogs import ApprovalDialog
from steward.gui.main_window import MainWindow
from steward.gui.theme import apply_theme
from steward.gui.widgets import Bubble


def main() -> None:
    app = QApplication(sys.argv)
    apply_theme(app)
    steward = Steward()
    for agent in steward.store.list_agents():
        steward.store.delete_agent(agent.id)
    for machine in steward.store.list_machines():
        steward.store.delete_machine(machine.name)

    house = steward.store.create_agent(
        "Agent",
        model="model",
        backend="ollama",
        persona="Looks after the machines on this network. Checks before changing anything.",
        approval_mode="ask",
        max_steps=12,
    )
    steward.store.create_agent(
        "Other",
        model="model",
        backend="llama_cpp",
        persona="Helps with code and the machines it builds on.",
        approval_mode="auto_readonly",
    )
    steward.store.upsert_machine(Machine(name="box", is_local=True))
    steward.store.upsert_machine(
        Machine(
            name="host",
            host="host.example",
            user="user",
            sudo_notes="Password required. The user is in the sudo group.",
        )
    )
    steward.store.save_note(house.id, "Coffee", "Likes black coffee in the morning, no sugar.", "food")
    steward.store.save_note(house.id, "host", "host is user@host.example. Updates happen on Sunday.", "machines")

    steward.store.add_message(house.id, "user", "Is host fully up to date? The webcam on this machine also dropped out after the last kernel update.")
    steward.store.add_message(
        house.id,
        "assistant",
        "I'll check the package list on host first, then look at the webcam on this computer.",
    )
    steward.store.add_tool_event(
        house.id,
        "run_command",
        {"command": "apt list --upgradable", "machine": "host"},
        "exit_code: 0\nstdout:\nlinux-image-generic/noble-updates 6.8.0-51 amd64 [upgradable from: 6.8.0-48]\nfwupd/noble-updates 1.9.26 amd64 [upgradable from: 1.9.16]",
        machine="host",
        approved=True,
        destructive=False,
    )
    steward.store.add_message(
        house.id,
        "assistant",
        "host is behind by a kernel and fwupd. I have not upgraded it yet.\n\nOn this computer the webcam is present on the USB bus, but `uvcvideo` is not loaded. I can load it if you want.\n\nI also noted that you take your coffee black.",
    )
    steward.store.add_tool_event(
        house.id,
        "run_command",
        {"command": "lsusb; lsmod | grep uvc", "machine": "box"},
        "exit_code: 0\nstdout:\nBus 002 Device 004: ID 046d:0825 Logitech, Inc. Webcam C270\n(no uvcvideo module)",
        machine="box",
        approved=True,
        destructive=False,
    )
    steward.store.add_tool_event(
        house.id,
        "save_note",
        {"title": "Coffee", "content": "Likes black coffee in the morning, no sugar."},
        "Saved note #1 (Coffee).",
        machine="memory",
        approved=True,
        destructive=False,
    )

    window = MainWindow(steward)
    window.resize(1280, 820)
    window.show()
    for _ in range(4):
        app.processEvents()
    for bubble in window.findChildren(Bubble):
        bubble._refit()
    app.processEvents()
    window.grab().save(sys.argv[1])

    dialog = ApprovalDialog(
        ApprovalRequest(
            tool_name="run_command",
            arguments={"command": "sudo apt update && sudo apt upgrade -y", "machine": "host"},
            machine="host",
            summary="sudo apt update && sudo apt upgrade -y",
            reason="This agent is set to ask before every action.",
            destructive=False,
            needs_sudo=True,
        )
    )
    dialog.setFixedWidth(640)
    dialog.adjustSize()
    dialog.show()
    app.processEvents()
    dialog.grab().save(sys.argv[2])
    print(sys.argv[1])
    print(sys.argv[2])


if __name__ == "__main__":
    main()
