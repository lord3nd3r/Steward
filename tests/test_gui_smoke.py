import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


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
