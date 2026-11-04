"""Dialogs for agents, machines, notes, settings, and command approval."""

from __future__ import annotations

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from steward.core.errors import StewardError
from steward.core.messages import NO_MACHINES_MESSAGE
from steward.core.llm.base import list_backend_models
from steward.core.models import (
    APPROVAL_LABELS,
    APPROVAL_MODES,
    BACKEND_LABELS,
    BACKENDS,
    TOOL_PROTOCOLS,
    Agent,
    ApprovalDecision,
    ApprovalRequest,
    Machine,
)
from steward.core.service import Steward


class _CallThread(QThread):
    ok = Signal(object)
    err = Signal(str)

    def __init__(self, fn) -> None:
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            self.ok.emit(self._fn())
        except Exception as exc:
            self.err.emit(str(exc))


class AgentDialog(QDialog):
    def __init__(self, steward: Steward, agent: Agent | None = None, parent=None) -> None:
        super().__init__(parent)
        self.steward = steward
        self.agent = agent
        self.deleted = False
        self._thread: _CallThread | None = None
        self.setWindowTitle("Edit agent" if agent else "New agent")
        self.setMinimumWidth(520)
        self.name = QLineEdit(agent.name if agent else "")
        self.persona = QPlainTextEdit(agent.persona if agent else "")
        self.persona.setPlaceholderText("How should this agent behave? What does it look after?")
        self.persona.setFixedHeight(110)
        self.backend = QComboBox()
        for key in BACKENDS:
            self.backend.addItem(BACKEND_LABELS[key], key)
        if agent:
            self.backend.setCurrentIndex(max(0, BACKENDS.index(agent.backend)))
        self.model = QComboBox()
        self.model.setEditable(True)
        if agent:
            self.model.setCurrentText(agent.model)
        refresh = QPushButton("List models")
        refresh.clicked.connect(self._refresh_models)
        self.model_status = QLabel("")
        self.model_status.setObjectName("muted")
        self.model_status.setWordWrap(True)
        model_row = QHBoxLayout()
        model_row.addWidget(self.model, 1)
        model_row.addWidget(refresh)
        self.approval = QComboBox()
        for key in APPROVAL_MODES:
            self.approval.addItem(APPROVAL_LABELS[key], key)
        if agent:
            self.approval.setCurrentIndex(max(0, APPROVAL_MODES.index(agent.approval_mode)))
        self.protocol = QComboBox()
        for key, label in (("auto", "Auto (native, then text)"), ("native", "Native tools only"), ("text", "Text protocol")):
            self.protocol.addItem(label, key)
        if agent:
            self.protocol.setCurrentIndex(max(0, TOOL_PROTOCOLS.index(agent.tool_protocol)))
        self.steps = QSpinBox()
        self.steps.setRange(1, 40)
        self.steps.setValue(agent.max_steps if agent else steward.settings.max_steps)
        form = QFormLayout()
        form.addRow("Name", self.name)
        form.addRow("Persona", self.persona)
        form.addRow("Backend", self.backend)
        form.addRow("Model", model_row)
        form.addRow("", self.model_status)
        form.addRow("Approval", self.approval)
        form.addRow("Tools", self.protocol)
        form.addRow("Max steps", self.steps)
        buttons = QDialogButtonBox()
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        save = buttons.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        save.setObjectName("primary")
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self._save)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        if agent:
            delete = QPushButton("Delete this agent")
            delete.setObjectName("danger")
            delete.clicked.connect(self._delete)
            layout.addWidget(delete)
        layout.addWidget(buttons)
        self.backend.currentIndexChanged.connect(lambda _i: self.model_status.setText(""))

    def _refresh_models(self) -> None:
        backend = self.backend.currentData()
        self.model_status.setText(f"Asking {BACKEND_LABELS.get(backend, backend)}…")
        self._thread = _CallThread(lambda: list_backend_models(self.steward.settings, backend))
        self._thread.ok.connect(self._models_ready)
        self._thread.err.connect(self._models_failed)
        self._thread.start()

    def _models_ready(self, names: object) -> None:
        models = list(names) if isinstance(names, list) else []
        current = self.model.currentText()
        self.model.clear()
        self.model.addItems(models)
        if current:
            self.model.setCurrentText(current)
        if models:
            self.model_status.setText(f"{len(models)} models.")
        else:
            self.model_status.setText("The server answered, but it has no models. Type a name anyway.")

    def _models_failed(self, message: str) -> None:
        self.model_status.setText(message)

    def _save(self) -> None:
        fields = dict(
            name=self.name.text(),
            persona=self.persona.toPlainText(),
            backend=self.backend.currentData(),
            model=self.model.currentText(),
            approval_mode=self.approval.currentData(),
            tool_protocol=self.protocol.currentData(),
            max_steps=self.steps.value(),
        )
        try:
            if self.agent is None:
                self.agent = self.steward.store.create_agent(**fields)
            else:
                self.agent = self.steward.store.update_agent(self.agent.id, **fields)
        except StewardError as exc:
            QMessageBox.warning(self, "Steward", str(exc))
            return
        self.accept()

    def _delete(self) -> None:
        if self.agent is None:
            return
        answer = QMessageBox.question(
            self,
            "Delete agent",
            f"Delete {self.agent.name}, including its notes and conversation?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.steward.store.delete_agent(self.agent.id)
        self.deleted = True
        self.accept()

    def done(self, code: int) -> None:  # type: ignore[override]
        if self._thread is not None and self._thread.isRunning():
            for signal in (self._thread.ok, self._thread.err):
                try:
                    signal.disconnect()
                except (RuntimeError, TypeError):
                    pass
        super().done(code)


class SettingsDialog(QDialog):
    def __init__(self, steward: Steward, parent=None) -> None:
        super().__init__(parent)
        self.steward = steward
        settings = steward.settings
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)
        self._threads: list[_CallThread] = []
        self.ollama = QLineEdit(settings.ollama_base_url)
        self.llama = QLineEdit(settings.llama_cpp_base_url)
        self.steps = QSpinBox()
        self.steps.setRange(1, 40)
        self.steps.setValue(settings.max_steps)
        self.history = QSpinBox()
        self.history.setRange(4, 200)
        self.history.setValue(settings.history_limit)
        self.timeout = QSpinBox()
        self.timeout.setRange(1, 900)
        self.timeout.setValue(settings.command_timeout)
        self.request_timeout = QSpinBox()
        self.request_timeout.setRange(5, 3600)
        self.request_timeout.setValue(int(settings.request_timeout))
        self.memory_backend = QComboBox()
        self.memory_backend.addItem("Keyword", "keyword")
        self.memory_backend.addItem("Ollama embeddings", "ollama")
        self.memory_backend.setCurrentIndex(
            max(0, self.memory_backend.findData(settings.memory_backend))
        )
        self.embedding_model = QLineEdit(settings.memory_embedding_model)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        ollama_row = self._url_row(self.ollama, "ollama")
        llama_row = self._url_row(self.llama, "llama_cpp")
        form = QFormLayout()
        form.addRow("Ollama URL", ollama_row)
        form.addRow("llama.cpp URL", llama_row)
        form.addRow("Default max steps", self.steps)
        form.addRow("History messages", self.history)
        form.addRow("Command timeout (s)", self.timeout)
        form.addRow("Model timeout (s)", self.request_timeout)
        form.addRow("Memory search", self.memory_backend)
        form.addRow("Embedding model", self.embedding_model)
        form.addRow("", self.status)
        hint = QLabel("These URLs are shared. Each agent still chooses its own model. Passwords are never written here.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        buttons = QDialogButtonBox()
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        save = buttons.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        save.setObjectName("primary")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(hint)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _url_row(self, field: QLineEdit, backend: str) -> QWidget:
        button = QPushButton("Test")
        button.clicked.connect(lambda: self._test(backend, field.text().strip()))
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(field, 1)
        layout.addWidget(button)
        return row

    def _test(self, backend: str, url: str) -> None:
        from dataclasses import replace

        trial = replace(self.steward.settings)
        if backend == "ollama":
            trial.ollama_base_url = url
        else:
            trial.llama_cpp_base_url = url
        self.status.setText("Contacting the server…")
        thread = _CallThread(lambda: list_backend_models(trial, backend))
        thread.ok.connect(lambda names: self.status.setText(self._summarize(backend, names)))
        thread.err.connect(lambda message: self.status.setText(message))
        self._threads.append(thread)
        thread.start()

    def _summarize(self, backend: str, names: object) -> str:
        models = list(names) if isinstance(names, list) else []
        label = BACKEND_LABELS.get(backend, backend)
        if not models:
            return f"{label} answered, but it has no models."
        preview = ", ".join(models[:4])
        return f"{label}: {preview}" + (f" (+{len(models) - 4})" if len(models) > 4 else "")

    def _apply_fields(self) -> None:
        settings = self.steward.settings
        settings.ollama_base_url = self.ollama.text().strip() or settings.ollama_base_url
        settings.llama_cpp_base_url = self.llama.text().strip() or settings.llama_cpp_base_url
        settings.max_steps = self.steps.value()
        settings.history_limit = self.history.value()
        settings.command_timeout = self.timeout.value()
        settings.request_timeout = float(self.request_timeout.value())
        settings.memory_backend = str(self.memory_backend.currentData())
        settings.memory_embedding_model = self.embedding_model.text().strip() or "nomic-embed-text"

    def _save(self) -> None:
        self._apply_fields()
        try:
            self.steward.settings.save()
            self.steward.reload_settings()
        except StewardError as exc:
            QMessageBox.warning(self, "Steward", str(exc))
            return
        self.accept()


class MachineDialog(QDialog):
    def __init__(self, steward: Steward, parent=None) -> None:
        super().__init__(parent)
        self.steward = steward
        self._thread: _CallThread | None = None
        self.setWindowTitle("Machines")
        self.resize(760, 480)
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._load_row)
        self.name = QLineEdit()
        self.host = QLineEdit()
        self.user = QLineEdit()
        self.this_computer = QCheckBox("This computer")
        self.this_computer.toggled.connect(self._sync_target)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(22)
        self.identity = QLineEdit()
        self.browse = QPushButton("Browse")
        self.browse.clicked.connect(self._browse)
        self.use_agent = QCheckBox("Use ssh-agent")
        self.use_agent.setChecked(True)
        self.sudo_notes = QPlainTextEdit()
        self.sudo_notes.setPlaceholderText("How sudo works on this machine. Never write the password.")
        self.sudo_notes.setFixedHeight(70)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        identity_row = QWidget()
        identity_layout = QHBoxLayout(identity_row)
        identity_layout.setContentsMargins(0, 0, 0, 0)
        identity_layout.addWidget(self.identity, 1)
        identity_layout.addWidget(self.browse)
        form = QFormLayout()
        form.addRow("Name", self.name)
        form.addRow("", self.this_computer)
        form.addRow("Host", self.host)
        form.addRow("User", self.user)
        form.addRow("Port", self.port)
        form.addRow("Identity file", identity_row)
        form.addRow("", self.use_agent)
        form.addRow("Sudo notes", self.sudo_notes)
        save = QPushButton("Save machine")
        save.setObjectName("primary")
        save.clicked.connect(self._save)
        remove = QPushButton("Remove")
        remove.setObjectName("danger")
        remove.clicked.connect(self._remove)
        self.test_ssh = QPushButton("Test SSH")
        self.test_ssh.clicked.connect(self._test)
        refresh = QPushButton("Refresh facts")
        refresh.clicked.connect(self._refresh_facts)
        new = QPushButton("New")
        new.clicked.connect(self._blank)
        row = QHBoxLayout()
        row.addWidget(new)
        row.addWidget(self.test_ssh)
        row.addWidget(refresh)
        row.addStretch(1)
        row.addWidget(remove)
        row.addWidget(save)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addLayout(form)
        right_layout.addLayout(row)
        right_layout.addWidget(self.status)
        splitter = QSplitter()
        splitter.addWidget(self.list)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        layout = QVBoxLayout(self)
        layout.addWidget(splitter)
        layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        self._reload()
        self._blank()

    def _reload(self) -> None:
        self.list.clear()
        for machine in self.steward.store.list_machines():
            if machine.is_local:
                label = f"{machine.name}  this computer"
            else:
                label = f"{machine.name}  {machine.user}@{machine.host}"
            self.list.addItem(label)
            self.list.item(self.list.count() - 1).setData(Qt.ItemDataRole.UserRole, machine.name)

    def _blank(self) -> None:
        self.list.clearSelection()
        self.name.setText("")
        self.this_computer.setChecked(False)
        self.host.setText("")
        self.user.setText("")
        self.port.setValue(22)
        self.identity.setText("")
        self.use_agent.setChecked(True)
        self.sudo_notes.setPlainText("")
        self.name.setReadOnly(False)
        self._sync_target()
        self._show_intro()

    def _show_intro(self) -> None:
        if not self.steward.store.list_machines():
            self.status.setText(NO_MACHINES_MESSAGE + " Or tick This computer, choose a name, and save.")
        else:
            self.status.setText(
                "Tick This computer for the machine Steward is running on. "
                "Leave it unticked for an SSH host. Verify its first key with OpenSSH before connecting."
            )

    def _sync_target(self) -> None:
        local = self.this_computer.isChecked()
        for widget in (self.host, self.user, self.port, self.identity, self.browse, self.use_agent, self.test_ssh):
            widget.setEnabled(not local)

    def _load_row(self, row: int) -> None:
        item = self.list.item(row)
        if item is None:
            return
        name = item.data(Qt.ItemDataRole.UserRole)
        if not name:
            return
        machine = self.steward.store.get_machine(name)
        if machine is None:
            return
        self.name.setText(machine.name)
        self.this_computer.setChecked(machine.is_local)
        self.host.setText("" if machine.is_local else machine.host)
        self.user.setText("" if machine.is_local else machine.user)
        self.port.setValue(22 if machine.is_local or machine.port < 1 else machine.port)
        self.identity.setText("" if machine.is_local else (machine.identity_file or ""))
        self._sync_target()
        self.use_agent.setChecked(machine.use_agent)
        self.sudo_notes.setPlainText(machine.sudo_notes)
        cached = self.steward.store.get_facts(machine.name)
        if cached is None:
            self.status.setText(f"{machine.name}: facts not gathered yet. Use Refresh facts.")
        else:
            from steward.core.facts import describe

            self.status.setText(describe(cached))

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "SSH private key", "", "Keys (*)")
        if path:
            self.identity.setText(path)

    def _machine_from_form(self) -> Machine:
        local = self.this_computer.isChecked()
        return Machine(
            name=self.name.text(),
            host="" if local else self.host.text(),
            user="" if local else self.user.text(),
            port=0 if local else self.port.value(),
            identity_file=None if local else (self.identity.text().strip() or None),
            sudo_notes=self.sudo_notes.toPlainText(),
            use_agent=False if local else self.use_agent.isChecked(),
            is_local=local,
        )

    def _save(self) -> None:
        try:
            stored = self.steward.store.upsert_machine(self._machine_from_form())
        except StewardError as exc:
            QMessageBox.warning(self, "Steward", str(exc))
            return
        self.status.setText(f"Saved {stored.name}.")
        self._reload()
        self._select(stored.name)

    def _remove(self) -> None:
        name = self.name.text().strip()
        if not name:
            return
        answer = QMessageBox.question(self, "Remove machine", f"Remove {name} from Steward?")
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.steward.store.delete_machine(name)
        except StewardError as exc:
            QMessageBox.warning(self, "Steward", str(exc))
            return
        self._reload()
        self._blank()

    def _select(self, name: str) -> None:
        for row in range(self.list.count()):
            item = self.list.item(row)
            if item and item.data(Qt.ItemDataRole.UserRole) == name:
                self.list.setCurrentRow(row)
                return

    def _test(self) -> None:
        if self.this_computer.isChecked():
            self.status.setText("This computer does not use SSH. Save it, then refresh facts.")
            return
        try:
            machine = self.steward.store.upsert_machine(self._machine_from_form())
        except StewardError as exc:
            QMessageBox.warning(self, "Steward", str(exc))
            return
        self._reload()
        self._select(machine.name)
        self.status.setText(f"Connecting to {machine.user}@{machine.host}…")

        def attempt() -> str:
            from steward.core.execution import run_ssh_command

            result = run_ssh_command(
                machine,
                self.steward.store,
                "echo steward-ok && hostname",
                timeout=20,
            )
            text = (result.stdout or result.stderr or "").strip()
            return f"exit {result.exit_code}: {text}"

        self._thread = _CallThread(attempt)
        self._thread.ok.connect(lambda text: self.status.setText(str(text)))
        self._thread.err.connect(lambda text: self.status.setText(str(text)))
        self._thread.start()

    def _refresh_facts(self) -> None:
        name = self.name.text().strip()
        if not name or self.steward.store.get_machine(name) is None:
            self.status.setText("Save the machine first, then refresh its facts.")
            return
        target = name
        self.status.setText(f"Reading facts for {target}…")

        def attempt() -> str:
            from steward.core.facts import describe, refresh_machine_facts

            found = refresh_machine_facts(self.steward.store, target)
            return describe(found[0]) if found else f"{target}: facts unavailable"

        self._thread = _CallThread(attempt)
        self._thread.ok.connect(lambda text: self.status.setText(str(text)))
        self._thread.err.connect(lambda text: self.status.setText(str(text)))
        self._thread.start()


class NotesDialog(QDialog):
    def __init__(self, steward: Steward, agent: Agent, parent=None) -> None:
        super().__init__(parent)
        self.steward = steward
        self.agent = agent
        self._current_id: int | None = None
        self.setWindowTitle(f"Notes · {agent.name}")
        self.resize(760, 480)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter notes")
        self.search.textChanged.connect(self._refill)
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._load)
        self.title = QLineEdit()
        self.tags = QLineEdit()
        self.content = QPlainTextEdit()
        form = QFormLayout()
        form.addRow("Title", self.title)
        form.addRow("Tags", self.tags)
        form.addRow("Note", self.content)
        save = QPushButton("Save note")
        save.setObjectName("primary")
        save.clicked.connect(self._save)
        delete = QPushButton("Delete")
        delete.setObjectName("danger")
        delete.clicked.connect(self._delete)
        new = QPushButton("New")
        new.clicked.connect(self._blank)
        row = QHBoxLayout()
        row.addWidget(new)
        row.addStretch(1)
        row.addWidget(delete)
        row.addWidget(save)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addLayout(form)
        right_layout.addLayout(row)
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(self.search)
        left_layout.addWidget(self.list)
        splitter = QSplitter()
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        layout = QVBoxLayout(self)
        intro = QLabel(f"{agent.name} uses these notes in later conversations. Passwords do not belong here.")
        intro.setObjectName("muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        layout.addWidget(splitter)
        layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        self._refill()

    def _notes(self):
        notes = self.steward.store.list_notes(self.agent.id)
        query = self.search.text().strip().lower()
        if not query:
            return notes
        return [note for note in notes if query in f"{note.title}\n{note.content}\n{note.tags}".lower()]

    def _refill(self) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for note in self._notes():
            self.list.addItem(f"#{note.id}  {note.title}")
            self.list.item(self.list.count() - 1).setData(Qt.ItemDataRole.UserRole, note.id)
        self.list.blockSignals(False)

    def _blank(self) -> None:
        self._current_id = None
        self.list.clearSelection()
        self.title.setText("")
        self.tags.setText("")
        self.content.setPlainText("")

    def _load(self, row: int) -> None:
        item = self.list.item(row)
        if item is None:
            return
        note_id = item.data(Qt.ItemDataRole.UserRole)
        note = self.steward.store.get_note(self.agent.id, int(note_id))
        if note is None:
            return
        self._current_id = note.id
        self.title.setText(note.title)
        self.tags.setText(note.tags)
        self.content.setPlainText(note.content)

    def _save(self) -> None:
        try:
            if self._current_id is None:
                note = self.steward.store.save_note(
                    self.agent.id, self.title.text(), self.content.toPlainText(), self.tags.text()
                )
            else:
                note = self.steward.store.update_note(
                    self.agent.id,
                    self._current_id,
                    title=self.title.text(),
                    content=self.content.toPlainText(),
                    tags=self.tags.text(),
                )
        except StewardError as exc:
            QMessageBox.warning(self, "Steward", str(exc))
            return
        self._current_id = note.id
        self._refill()

    def _delete(self) -> None:
        if self._current_id is None:
            return
        self.steward.store.delete_note(self.agent.id, self._current_id)
        self._blank()
        self._refill()


class ApprovalDialog(QDialog):
    def __init__(self, request: ApprovalRequest, parent=None) -> None:
        super().__init__(parent)
        self.request = request
        self.setWindowTitle("Steward — approve action")
        self.setMinimumWidth(560)
        self.setModal(True)
        heading = QLabel("Approve this action" if not request.password_only else "Sudo password")
        font = heading.font()
        font.setPointSize(16)
        heading.setFont(font)
        reason = QLabel(request.reason)
        reason.setWordWrap(True)
        reason.setObjectName("muted")
        where = QLabel(f"{request.tool_name}    on    {request.machine}")
        where.setObjectName("chipQuiet")
        summary = QPlainTextEdit(request.summary)
        summary.setReadOnly(True)
        summary.setFont(self._mono())
        summary.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        summary.document().setTextWidth(520)
        summary.setFixedHeight(min(220, max(72, int(summary.document().size().height()) + 20)))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Sent only to sudo for this command. Not stored.")
        layout = QVBoxLayout(self)
        layout.addWidget(heading)
        layout.addWidget(where)
        layout.addWidget(reason)
        if request.destructive:
            banner = QLabel("This looks destructive. Steward asks even in full-auto.")
            banner.setObjectName("dangerBanner")
            banner.setWordWrap(True)
            layout.addWidget(banner)
        layout.addWidget(summary)
        if request.needs_sudo:
            layout.addWidget(QLabel("Sudo password"))
            layout.addWidget(self.password)
        else:
            self.password.hide()
        buttons = QHBoxLayout()
        deny = QPushButton("Deny")
        approve = QPushButton("Run anyway" if request.destructive else "Approve")
        approve.setObjectName("danger" if request.destructive else "primary")
        deny.clicked.connect(self.reject)
        approve.clicked.connect(self.accept)
        if request.destructive:
            deny.setDefault(True)
        else:
            approve.setDefault(True)
        buttons.addWidget(deny)
        buttons.addStretch(1)
        buttons.addWidget(approve)
        layout.addLayout(buttons)
        if request.needs_sudo:
            self.password.setFocus()

    def decision(self) -> ApprovalDecision:
        if self.result() != QDialog.DialogCode.Accepted:
            return ApprovalDecision(approved=False)
        password = self.password.text() if self.request.needs_sudo else ""
        return ApprovalDecision(approved=True, sudo_password=password or None)

    @staticmethod
    def _mono():
        from PySide6.QtGui import QFont

        return QFont("JetBrains Mono", 10)
