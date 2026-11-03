import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_fresh_window_invites_the_first_agent_and_a_machine():
    pytest.importorskip("PySide6")
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtWidgets import QApplication

    from steward.core.messages import NO_MACHINES_MESSAGE
    from steward.core.service import Steward
    from steward.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    window = MainWindow(Steward())
    window.show()
    app.processEvents()
    assert window.empty.isVisible()
    assert "first agent" in window.empty.text().lower()
    assert window.machine_hint.text() == NO_MACHINES_MESSAGE
    assert window.machine_hint.isVisible()
    from steward.gui.dialogs import MachineDialog

    dialog = MachineDialog(window.steward)
    assert dialog.this_computer.text() == "This computer"
    assert NO_MACHINES_MESSAGE in dialog.status.text()
    dialog.this_computer.setChecked(True)
    assert not dialog.host.isEnabled()
    dialog.close()
    window.close()


def test_window_opens_and_lists_an_agent():
    pytest.importorskip("PySide6")
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtWidgets import QApplication

    from steward.core.service import Steward
    from steward.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    steward = Steward()
    steward.store.create_agent("House", model="llama3.1", persona="Looks after the machines.")
    window = MainWindow(steward)
    window.show()
    app.processEvents()
    assert window.windowTitle() == "Steward"
    assert window.agent_list.count() == 1
    assert "House" in window.agent_title.text()
    window.close()
