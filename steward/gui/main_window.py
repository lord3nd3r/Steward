"""The Steward window: agents, a streaming transcript, and the tool log."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QAction, QFont, QKeySequence
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from steward.core.messages import NO_MACHINES_MESSAGE
from steward.core.models import APPROVAL_LABELS, BACKEND_LABELS, TurnResult
from steward.core.service import Steward
from steward.gui.dialogs import AgentDialog, ApprovalDialog, MachineDialog, NotesDialog, SettingsDialog
from steward.gui.widgets import Bubble, BubbleRow, ToolCard
from steward.gui.workers import TurnWorker


class MainWindow(QMainWindow):
    def __init__(self, steward: Steward | None = None) -> None:
        super().__init__()
        self.steward = steward or Steward()
        self.setWindowTitle("Steward")
        self.resize(1180, 760)
        self.setMinimumSize(960, 600)
        self._agent_id: str | None = None
        self._worker: TurnWorker | None = None
        self._live: Bubble | None = None
        self._build()
        self._refresh_agents()

    def _build(self) -> None:
        self._build_menu()
        sidebar = self._build_sidebar()
        center = self._build_chat()
        activity = self._build_activity()
        splitter = QSplitter()
        splitter.addWidget(sidebar)
        splitter.addWidget(center)
        splitter.addWidget(activity)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([250, 620, 320])
        self.setCentralWidget(splitter)
        self.statusBar().showMessage("Ready")

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("File")
        file_menu.addAction("Settings", self._open_settings).setShortcut(QKeySequence("Ctrl+,"))
        file_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence("Ctrl+Q"))
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)
        agents = self.menuBar().addMenu("Agents")
        new_action = QAction("New agent", self)
        new_action.setShortcut(QKeySequence("Ctrl+N"))
        new_action.triggered.connect(self._new_agent)
        agents.addAction(new_action)
        agents.addAction("Edit agent", self._edit_agent)
        agents.addAction("Machines", self._open_machines)
        agents.addAction("Notes", self._open_notes)

    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setMinimumWidth(210)
        wordmark = QLabel("Steward")
        wordmark.setObjectName("wordmark")
        wordmark.setFont(QFont("Liberation Serif", 22))
        tagline = QLabel("Your Linux machines, with memory")
        tagline.setObjectName("tagline")
        new_agent = QPushButton("New agent")
        new_agent.setObjectName("primary")
        new_agent.clicked.connect(self._new_agent)
        self.agent_list = QListWidget()
        self.agent_list.currentRowChanged.connect(self._agent_row_changed)
        machines = QPushButton("Machines")
        notes = QPushButton("Notes")
        settings = QPushButton("Settings")
        for button in (machines, notes, settings):
            button.setObjectName("ghost")
        machines.clicked.connect(self._open_machines)
        notes.clicked.connect(self._open_notes)
        settings.clicked.connect(self._open_settings)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 16, 12, 12)
        layout.addWidget(wordmark)
        layout.addWidget(tagline)
        layout.addSpacing(12)
        layout.addWidget(new_agent)
        layout.addWidget(self.agent_list, 1)
        layout.addWidget(machines)
        layout.addWidget(notes)
        layout.addWidget(settings)
        return sidebar

    def _build_chat(self) -> QWidget:
        pane = QWidget()
        pane.setObjectName("chatPane")
        self.agent_title = QLabel("Steward")
        self.agent_title.setFont(QFont("Liberation Serif", 20))
        self.agent_persona = QLabel("Create an agent to start. Each one keeps its own model and its own notes.")
        self.agent_persona.setObjectName("persona")
        self.agent_persona.setWordWrap(True)
        self.agent_meta = QLabel("")
        self.agent_meta.setObjectName("muted")
        edit = QPushButton("Edit")
        edit.clicked.connect(self._edit_agent)
        header = QWidget()
        header.setObjectName("header")
        header_row = QHBoxLayout(header)
        titles = QVBoxLayout()
        titles.addWidget(self.agent_title)
        titles.addWidget(self.agent_persona)
        titles.addWidget(self.agent_meta)
        header_row.addLayout(titles, 1)
        header_row.addWidget(edit, alignment=Qt.AlignmentFlag.AlignTop)

        self.transcript_host = QWidget()
        self.transcript_layout = QVBoxLayout(self.transcript_host)
        self.transcript_layout.setContentsMargins(8, 12, 8, 12)
        self.transcript_layout.addStretch(1)
        self.transcript_scroll = QScrollArea()
        self.transcript_scroll.setWidgetResizable(True)
        self.transcript_scroll.setWidget(self.transcript_host)
        self.transcript_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.transcript_scroll.viewport().setStyleSheet("background: #16130f;")

        self.empty = QLabel(
            "No agents yet.\n\n"
            "Create your first agent. Steward starts empty: no sample agents, "
            "and no machines until you add them."
        )
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        self.empty.setObjectName("persona")

        self.error_banner = QLabel("")
        self.error_banner.setObjectName("errorBanner")
        self.error_banner.setWordWrap(True)
        self.error_banner.hide()
        self.machine_hint = QLabel("")
        self.machine_hint.setObjectName("muted")
        self.machine_hint.setWordWrap(True)
        self.machine_hint.hide()

        self.composer = QPlainTextEdit()
        self.composer.setPlaceholderText("Ask something, or tell this agent a fact worth keeping.")
        self.composer.setFixedHeight(84)
        self.composer.installEventFilter(self)
        self.send_button = QPushButton("Send")
        self.send_button.setObjectName("primary")
        self.send_button.clicked.connect(self._send)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setObjectName("danger")
        self.stop_button.clicked.connect(self._stop)
        self.stop_button.hide()
        hint = QLabel("Enter to send  ·  Shift+Enter for a new line")
        hint.setObjectName("muted")
        composer_row = QHBoxLayout()
        composer_row.addWidget(self.composer, 1)
        buttons = QVBoxLayout()
        buttons.addWidget(self.send_button)
        buttons.addWidget(self.stop_button)
        buttons.addStretch(1)
        composer_row.addLayout(buttons)

        layout = QVBoxLayout(pane)
        layout.setContentsMargins(0, 0, 0, 8)
        layout.addWidget(header)
        layout.addWidget(self.empty)
        layout.addWidget(self.transcript_scroll, 1)
        layout.addWidget(self.error_banner)
        layout.addWidget(self.machine_hint)
        layout.addLayout(composer_row)
        layout.addWidget(hint)
        return pane

    def _build_activity(self) -> QWidget:
        pane = QWidget()
        pane.setObjectName("activityPane")
        pane.setMinimumWidth(240)
        title = QLabel("ACTIVITY")
        title.setObjectName("section")
        self.activity_empty = QLabel("Commands, file edits, and notes the agent writes show up here, with their output.")
        self.activity_empty.setWordWrap(True)
        self.activity_empty.setObjectName("persona")
        self.activity_host = QWidget()
        self.activity_layout = QVBoxLayout(self.activity_host)
        self.activity_layout.setContentsMargins(8, 8, 8, 8)
        self.activity_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.activity_host)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.viewport().setStyleSheet("background: #16130f;")
        self.activity_scroll = scroll
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(8, 12, 8, 8)
        layout.addWidget(title)
        layout.addWidget(self.activity_empty)
        layout.addWidget(scroll, 1)
        return pane

    def eventFilter(self, watched, event) -> bool:  # type: ignore[override]
        if watched is self.composer and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
                self._send()
                return True
        return super().eventFilter(watched, event)

    def _refresh_agents(self, select_id: str | None = None) -> None:
        agents = self.steward.store.list_agents()
        wanted = select_id or self._agent_id
        self.agent_list.blockSignals(True)
        self.agent_list.clear()
        selected_row = 0
        for index, agent in enumerate(agents):
            label = f"{agent.name}\n{BACKEND_LABELS.get(agent.backend, agent.backend)} · {agent.model}"
            self.agent_list.addItem(label)
            item = self.agent_list.item(index)
            item.setData(Qt.ItemDataRole.UserRole, agent.id)
            item.setSizeHint(QSize(220, 52))
            if agent.id == wanted:
                selected_row = index
        if agents:
            self.agent_list.setCurrentRow(selected_row)
        self.agent_list.blockSignals(False)
        if not agents:
            self._agent_id = None
            self._show_empty(True)
            return
        self._show_agent(agents[selected_row].id)

    def _agent_row_changed(self, row: int) -> None:
        if self._worker and self._worker.isRunning():
            return
        item = self.agent_list.item(row)
        if item is None:
            return
        self._show_agent(item.data(Qt.ItemDataRole.UserRole))

    def _show_empty(self, empty: bool) -> None:
        self.empty.setVisible(empty)
        self.transcript_scroll.setVisible(not empty)
        self.composer.setEnabled(not empty)
        self.send_button.setEnabled(not empty)
        if empty:
            self.agent_title.setText("Steward")
            self.agent_persona.setText("Create your first agent. Each one keeps its own model and its own notes.")
            self.agent_meta.setText("")
            self._clear_transcript()
            self._clear_activity()
        self._refresh_setup_hint()

    def _show_agent(self, agent_id: str) -> None:
        agent = self.steward.store.get_agent(agent_id)
        if agent is None:
            self._refresh_agents()
            return
        self._agent_id = agent.id
        self._show_empty(False)
        self.agent_title.setText(agent.name)
        self.agent_persona.setText(agent.persona)
        approval = APPROVAL_LABELS.get(agent.approval_mode, agent.approval_mode)
        backend = BACKEND_LABELS.get(agent.backend, agent.backend)
        self.agent_meta.setText(f"{backend}  ·  {agent.model}  ·  {approval}  ·  {agent.max_steps} steps")
        self.composer.setPlaceholderText(f"Ask {agent.name}, or tell it something worth remembering.")
        self._clear_error()
        self._refresh_setup_hint()
        self._render_history(agent.id, agent.name)

    def _refresh_setup_hint(self) -> None:
        if self.steward.store.list_machines():
            self.machine_hint.hide()
            self.machine_hint.setText("")
            return
        self.machine_hint.setText(NO_MACHINES_MESSAGE)
        self.machine_hint.show()

    def _render_history(self, agent_id: str, agent_name: str) -> None:
        self._clear_transcript()
        self._clear_activity()
        for message in self.steward.store.list_messages(agent_id, limit=200):
            if message.role == "user":
                self._add_bubble("You", message.content, mine=True, markdown=False)
            elif message.role == "assistant" and message.content.strip():
                self._add_bubble(agent_name, message.content, mine=False, markdown=True)
        events = self.steward.store.list_tool_events(agent_id, limit=80)
        for event in events:
            self._add_tool_card(event)
        self._scroll_transcript()

    def _clear_transcript(self) -> None:
        while self.transcript_layout.count() > 1:
            item = self.transcript_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._live = None

    def _clear_activity(self) -> None:
        while self.activity_layout.count() > 1:
            item = self.activity_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.activity_empty.show()

    def _add_bubble(self, who: str, text: str, *, mine: bool, markdown: bool) -> Bubble:
        bubble = Bubble(who, mine=mine)
        bubble.set_final(text, markdown=markdown)
        row = BubbleRow(bubble, mine=mine)
        self.transcript_layout.insertWidget(self.transcript_layout.count() - 1, row)
        self._scroll_transcript()
        return bubble

    def _add_tool_card(self, event) -> None:
        self.activity_empty.hide()
        card = ToolCard(event)
        self.activity_layout.insertWidget(self.activity_layout.count() - 1, card)
        bar = self.activity_scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _scroll_transcript(self) -> None:
        bar = self.transcript_scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _send(self) -> None:
        if self._worker and self._worker.isRunning():
            return
        if not self._agent_id:
            return
        text = self.composer.toPlainText().strip()
        if not text:
            return
        agent = self.steward.store.get_agent(self._agent_id)
        if agent is None:
            return
        self.composer.clear()
        self._clear_error()
        self._add_bubble("You", text, mine=True, markdown=False)
        self._set_running(True)
        self.steward.reload_settings()
        self._worker = TurnWorker(self.steward, agent.id, text)
        self._worker.token.connect(self._on_token)
        self._worker.segment.connect(self._on_segment)
        self._worker.tool_done.connect(self._add_tool_card)
        self._worker.status.connect(self.statusBar().showMessage)
        self._worker.need_approval.connect(self._on_approval)
        self._worker.finished_turn.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._live = None
        self._worker.start()

    def _on_token(self, token: str) -> None:
        agent = self.steward.store.get_agent(self._agent_id or "")
        name = agent.name if agent else "Agent"
        if self._live is None:
            self._live = self._add_bubble(name, "", mine=False, markdown=False)
        self._live.append_plain(token)
        self._scroll_transcript()

    def _on_segment(self, text: str) -> None:
        agent = self.steward.store.get_agent(self._agent_id or "")
        name = agent.name if agent else "Agent"
        if self._live is None:
            self._live = self._add_bubble(name, text, mine=False, markdown=True)
        else:
            self._live.set_final(text, markdown=True)
        self._live = None
        self._scroll_transcript()

    def _on_approval(self, request) -> None:
        self.statusBar().showMessage("Waiting for approval")
        if self._worker is None:
            return
        dialog = ApprovalDialog(request, self)
        dialog.exec()
        self._worker.submit_decision(dialog.decision())

    def _on_finished(self, result: object) -> None:
        self._set_running(False)
        if isinstance(result, TurnResult) and result.cancelled:
            self.statusBar().showMessage("Stopped")
        elif isinstance(result, TurnResult) and result.truncated:
            self.statusBar().showMessage("Reached the step limit")
        else:
            self.statusBar().showMessage("Ready")
        self._live = None

    def _on_failed(self, message: str) -> None:
        self._set_running(False)
        self._show_error(message)
        self.statusBar().showMessage("Could not finish that turn")
        self._live = None

    def _stop(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self.statusBar().showMessage("Stopping…")

    def _set_running(self, running: bool) -> None:
        self.send_button.setVisible(not running)
        self.stop_button.setVisible(running)
        self.composer.setEnabled(not running)
        self.agent_list.setEnabled(not running)

    def _show_error(self, message: str) -> None:
        self.error_banner.setText(message)
        self.error_banner.show()

    def _clear_error(self) -> None:
        self.error_banner.hide()
        self.error_banner.setText("")

    def _new_agent(self) -> None:
        dialog = AgentDialog(self.steward, parent=self)
        if dialog.exec() != dialog.DialogCode.Accepted or dialog.agent is None or dialog.deleted:
            return
        self._refresh_agents(select_id=dialog.agent.id)

    def _edit_agent(self) -> None:
        if not self._agent_id:
            self._new_agent()
            return
        agent = self.steward.store.get_agent(self._agent_id)
        if agent is None:
            return
        dialog = AgentDialog(self.steward, agent, parent=self)
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        if dialog.deleted:
            self._agent_id = None
            self._refresh_agents()
            return
        self._refresh_agents(select_id=dialog.agent.id if dialog.agent else None)

    def _open_machines(self) -> None:
        MachineDialog(self.steward, self).exec()
        self._refresh_setup_hint()

    def _open_notes(self) -> None:
        if not self._agent_id:
            self.statusBar().showMessage("Create an agent before editing notes")
            return
        agent = self.steward.store.get_agent(self._agent_id)
        if agent is None:
            return
        NotesDialog(self.steward, agent, self).exec()

    def _open_settings(self) -> None:
        SettingsDialog(self.steward, self).exec()
        self.steward.reload_settings()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(3000)
        super().closeEvent(event)
