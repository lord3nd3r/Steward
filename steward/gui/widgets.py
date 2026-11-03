"""Chat bubbles and the tool-call log."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QTextOption
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from steward.core.models import ToolEventRecord


class Bubble(QFrame):
    def __init__(self, who: str, *, mine: bool) -> None:
        super().__init__()
        self.setObjectName("userBubble" if mine else "assistantBubble")
        self.setMinimumWidth(320)
        self.setMaximumWidth(720)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.view = QTextBrowser()
        self.view.setOpenLinks(False)
        self.view.setOpenExternalLinks(False)
        self.view.setFrameShape(QFrame.Shape.NoFrame)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        who_label = QLabel(who)
        who_label.setObjectName("muted")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 10)
        layout.setSpacing(4)
        layout.addWidget(who_label)
        layout.addWidget(self.view)
        self._text = ""
        self._markdown = False

    def append_plain(self, token: str) -> None:
        self._text += token
        self._markdown = False
        self.view.setPlainText(self._text)
        self._refit()

    def set_final(self, text: str, *, markdown: bool) -> None:
        self._text = text
        self._markdown = markdown
        if markdown:
            self.view.setMarkdown(text)
        else:
            self.view.setPlainText(text)
        self._refit()
        QTimer.singleShot(0, self._refit)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._refit()

    def _refit(self) -> None:
        width = max(220, self.width() - 28)
        document = self.view.document()
        document.setTextWidth(width)
        height = int(document.size().height()) + 6
        self.view.setFixedHeight(max(24, height))


class BubbleRow(QWidget):
    def __init__(self, bubble: Bubble, *, mine: bool) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 4, 16, 4)
        if mine:
            layout.addStretch(1)
            layout.addWidget(bubble, 3)
        else:
            layout.addWidget(bubble, 4)
            layout.addStretch(1)


class ToolCard(QFrame):
    def __init__(self, event: ToolEventRecord) -> None:
        super().__init__()
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        if not event.approved:
            self.setObjectName("toolCardDenied")
        elif event.destructive:
            self.setObjectName("toolCardDanger")
        else:
            self.setObjectName("toolCard")
        title = QLabel(f"{event.tool_name}   {event.machine}")
        title.setObjectName("muted")
        state = "denied" if not event.approved else ("destructive · ran" if event.destructive else "ran")
        meta = QLabel(f"{state}   {_clock(event.created_at)}")
        meta.setObjectName("muted")
        command = _command_preview(event)
        command_label = QLabel(command)
        command_label.setWordWrap(True)
        command_label.setMinimumWidth(0)
        command_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        command_label.setFont(QFont("JetBrains Mono", 10))
        command_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        output = QPlainTextEdit()
        output.setReadOnly(True)
        output.setFont(QFont("JetBrains Mono", 9))
        output.setPlainText(event.result or "")
        output.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        output.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        output.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        output.setMinimumWidth(0)
        output.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        lines = min(12, max(3, event.result.count("\n") + 1))
        output.setFixedHeight(18 * lines + 16)
        copy = QPushButton("Copy")
        copy.setObjectName("ghost")
        copy.clicked.connect(lambda: QApplication.clipboard().setText(command))
        header = QHBoxLayout()
        header.addWidget(title, 1)
        header.addWidget(meta)
        header.addWidget(copy)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(6)
        layout.addLayout(header)
        if command:
            layout.addWidget(command_label)
        layout.addWidget(output)


def _command_preview(event: ToolEventRecord) -> str:
    args = event.arguments or {}
    if event.tool_name == "run_command":
        cwd = args.get("cwd")
        command = str(args.get("command") or "")
        return f"(cwd {cwd})\n{command}" if cwd else command
    if event.tool_name in {"read_file", "list_directory", "write_file"}:
        return str(args.get("path") or "")
    if event.tool_name == "save_note":
        return f"{args.get('title')}: {args.get('content')}"
    if event.tool_name == "forget_note":
        return f"id={args.get('note_id') or ''} title={args.get('title') or ''}"
    if event.tool_name == "search_notes":
        return str(args.get("query") or "")
    return ""


def _clock(iso: str) -> str:
    if not iso:
        return ""
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if moment.tzinfo is not None:
        moment = moment.astimezone()
    return moment.strftime("%H:%M:%S")
