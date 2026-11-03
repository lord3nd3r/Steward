"""Launch the Steward window."""

from __future__ import annotations

import sys


def main() -> None:
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    from steward.gui.main_window import MainWindow
    from steward.gui.theme import apply_theme

    app = QApplication(sys.argv)
    app.setApplicationName("Steward")
    app.setOrganizationName("steward")
    app.setDesktopFileName("steward")
    apply_theme(app)
    _set_icon(app)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


def _set_icon(app) -> None:
    try:
        from importlib.resources import files

        from PySide6.QtGui import QIcon

        icon = files("steward.assets").joinpath("steward.svg")
        app.setWindowIcon(QIcon(str(icon)))
    except Exception:
        return


if __name__ == "__main__":
    main()
