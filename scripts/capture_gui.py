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
        "House",
        model="llama3.1",
        backend="ollama",
        persona="Looks after the Linux machines on this network. Checks before changing anything, and keeps notes on how the house is set up.",
        approval_mode="ask",
        max_steps=12,
    )
    steward.store.create_agent(
        "Workshop",
        model="qwen2.5-coder",
        backend="llama_cpp",
        persona="Helps with the local code and the machines it builds on.",
        approval_mode="auto_readonly",
    )
    steward.store.upsert_machine(
        Machine(
            name="SER",
            host="ser.local",
            user="ada",
            sudo_notes="Password required. User is in the sudo group.",
        )
    )
    steward.store.save_note(house.id, "Coffee", "Likes black coffee in the morning, no sugar.", "food")
    steward.store.save_note(house.id, "SER", "SER is the living-room server, ada@ser.local. Updates happen on Sunday.", "machines")

    steward.store.add_message(house.id, "user", "Is SER fully up to date? The webcam on this machine also dropped out after the last kernel update.")
    steward.store.add_message(
        house.id,
        "assistant",
        "I'll check SER's package list first, then look at the webcam on this machine.",
    )
    steward.store.add_tool_event(
        house.id,
        "run_command",
        {"command": "apt list --upgradable", "machine": "SER"},
        "exit_code: 0\nstdout:\nlinux-image-generic/noble-updates 6.8.0-51 amd64 [upgradable from: 6.8.0-48]\nfwupd/noble-updates 1.9.26 amd64 [upgradable from: 1.9.16]",
        machine="SER",
        approved=True,
        destructive=False,
    )
    steward.store.add_message(
        house.id,
        "assistant",
        "SER is behind by a kernel and fwupd. I have not upgraded it yet.\n\nOn this machine the webcam is present on the USB bus, but `uvcvideo` is not loaded. I can load it if you want.\n\nI also noted that you take your coffee black.",
    )
    steward.store.add_tool_event(
        house.id,
        "run_command",
        {"command": "lsusb; lsmod | grep uvc", "machine": "local"},
        "exit_code: 0\nstdout:\nBus 002 Device 004: ID 046d:0825 Logitech, Inc. Webcam C270\n(no uvcvideo module)",
        machine="local",
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
            arguments={"command": "sudo apt update && sudo apt upgrade -y", "machine": "SER"},
            machine="SER",
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
