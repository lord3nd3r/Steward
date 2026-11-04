"""SQLite persistence for agents, machines, transcripts, notes, and tool logs."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from steward.core.errors import StewardError
from steward.core.memory_index import get_memory_index
from steward.core.messages import NO_AGENTS_MESSAGE
from steward.core.models import (
    APPROVAL_MODES,
    BACKENDS,
    TOOL_PROTOCOLS,
    Agent,
    FileBackup,
    Machine,
    MachineFacts,
    Note,
    StoredMessage,
    ToolCall,
    ToolEventRecord,
)
from steward.core.paths import database_path, ensure_private_dir
from steward.core.settings import Settings

_NAME_OK = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]{0,39}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    persona TEXT NOT NULL,
    backend TEXT NOT NULL,
    model TEXT NOT NULL,
    approval_mode TEXT NOT NULL,
    tool_protocol TEXT NOT NULL,
    max_steps INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS machines (
    name TEXT PRIMARY KEY COLLATE NOCASE,
    host TEXT NOT NULL,
    user TEXT NOT NULL,
    port INTEGER NOT NULL,
    identity_file TEXT,
    sudo_notes TEXT NOT NULL DEFAULT '',
    use_agent INTEGER NOT NULL DEFAULT 1,
    host_key_fingerprint TEXT NOT NULL DEFAULT '',
    is_local INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    tool_calls_json TEXT,
    tool_call_id TEXT,
    tool_name TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tool_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    result TEXT NOT NULL,
    machine TEXT NOT NULL,
    approved INTEGER NOT NULL,
    destructive INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS file_backups (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    machine TEXT NOT NULL,
    path TEXT NOT NULL,
    existed INTEGER NOT NULL,
    content BLOB,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS machine_facts (
    machine TEXT PRIMARY KEY COLLATE NOCASE,
    user_name TEXT NOT NULL DEFAULT '',
    is_root INTEGER NOT NULL DEFAULT 0,
    os_id TEXT NOT NULL DEFAULT '',
    os_version TEXT NOT NULL DEFAULT '',
    pkg TEXT NOT NULL DEFAULT '',
    sudo_state TEXT NOT NULL DEFAULT '',
    extras TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    gathered_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_agent ON messages(agent_id, id);
CREATE INDEX IF NOT EXISTS idx_notes_agent ON notes(agent_id);
CREATE INDEX IF NOT EXISTS idx_events_agent ON tool_events(agent_id, id);
CREATE INDEX IF NOT EXISTS idx_backups_created ON file_backups(created_at);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Store:
    def __init__(self, db_path=None) -> None:
        self.db_path = db_path or database_path()
        ensure_private_dir(self.db_path.parent)
        if self.db_path.exists():
            self.db_path.chmod(0o600)
        self._lock = threading.Lock()
        with self._lock:
            with self._connect() as conn:
                conn.executescript(_SCHEMA)
                _migrate(conn)
        self.db_path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    # ----- agents ---------------------------------------------------------

    def list_agents(self) -> list[Agent]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM agents ORDER BY name COLLATE NOCASE").fetchall()
        return [_agent(row) for row in rows]

    def get_agent(self, id_or_name: str) -> Agent | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM agents WHERE id = ?", (id_or_name,)).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM agents WHERE name = ? COLLATE NOCASE", (id_or_name,)
                ).fetchone()
        return _agent(row) if row else None

    def create_agent(
        self,
        name: str,
        *,
        persona: str = "",
        backend: str = "ollama",
        model: str = "",
        approval_mode: str = "ask",
        tool_protocol: str = "auto",
        max_steps: int | None = None,
    ) -> Agent:
        clean = _validate_name(name, "Agent name")
        backend = _validate_choice(backend, BACKENDS, "backend")
        approval_mode = _validate_choice(approval_mode, APPROVAL_MODES, "approval mode")
        tool_protocol = _validate_choice(tool_protocol, TOOL_PROTOCOLS, "tool protocol")
        if not model or not str(model).strip():
            raise StewardError("A model name is required.")
        steps = max_steps if max_steps is not None else Settings.load().max_steps
        steps = max(1, min(40, int(steps)))
        now = utcnow()
        agent_id = uuid.uuid4().hex
        persona = persona.strip()
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO agents (
                        id, name, persona, backend, model, approval_mode, tool_protocol,
                        max_steps, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        agent_id, clean, persona, backend, model.strip(), approval_mode,
                        tool_protocol, steps, now, now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise StewardError(f"An agent named {clean} already exists.") from exc
        agent = self.get_agent(agent_id)
        assert agent is not None
        return agent

    def update_agent(self, agent_id: str, **fields: Any) -> Agent:
        current = self.get_agent(agent_id)
        if current is None:
            raise StewardError(f"No agent with id {agent_id}.")
        allowed = {
            "name", "persona", "backend", "model", "approval_mode", "tool_protocol", "max_steps",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise StewardError(f"Cannot update {', '.join(sorted(unknown))}.")
        name = _validate_name(fields.get("name", current.name), "Agent name")
        persona = str(fields.get("persona", current.persona)).strip()
        backend = _validate_choice(fields.get("backend", current.backend), BACKENDS, "backend")
        approval_mode = _validate_choice(
            fields.get("approval_mode", current.approval_mode), APPROVAL_MODES, "approval mode"
        )
        tool_protocol = _validate_choice(
            fields.get("tool_protocol", current.tool_protocol), TOOL_PROTOCOLS, "tool protocol"
        )
        model = str(fields.get("model", current.model)).strip()
        if not model:
            raise StewardError("A model name is required.")
        steps = int(fields.get("max_steps", current.max_steps))
        steps = max(1, min(40, steps))
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    """
                    UPDATE agents
                    SET name = ?, persona = ?, backend = ?, model = ?, approval_mode = ?,
                        tool_protocol = ?, max_steps = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (name, persona, backend, model, approval_mode, tool_protocol, steps, utcnow(), current.id),
                )
        except sqlite3.IntegrityError as exc:
            raise StewardError(f"An agent named {name} already exists.") from exc
        updated = self.get_agent(current.id)
        assert updated is not None
        return updated

    def delete_agent(self, id_or_name: str) -> None:
        agent = self.get_agent(id_or_name)
        if agent is None:
            raise StewardError(f"No agent named {id_or_name}.")
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM agents WHERE id = ?", (agent.id,))

    def clear_history(self, id_or_name: str) -> None:
        agent = self.require_agent(id_or_name)
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM messages WHERE agent_id = ?", (agent.id,))
            conn.execute("DELETE FROM tool_events WHERE agent_id = ?", (agent.id,))

    def require_agent(self, id_or_name: str) -> Agent:
        agent = self.get_agent(id_or_name)
        if agent is None:
            if not self.list_agents():
                raise StewardError(NO_AGENTS_MESSAGE)
            raise StewardError(
                f"No agent named {id_or_name}. "
                "Create one with: steward agents create --name <name> --model <model>"
            )
        return agent

    # ----- machines -------------------------------------------------------

    def list_machines(self) -> list[Machine]:
        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT * FROM machines ORDER BY name COLLATE NOCASE").fetchall()
        return [_machine(row) for row in rows]

    def get_machine(self, name: str) -> Machine | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM machines WHERE name = ? COLLATE NOCASE", (name,)
            ).fetchone()
        return _machine(row) if row else None

    def upsert_machine(self, machine: Machine) -> Machine:
        import getpass

        name = _validate_name(machine.name, "Machine name")
        if machine.is_local:
            host = ""
            try:
                user = getpass.getuser() or ""
            except Exception:
                user = ""
            port = 0
            identity = None
            use_agent = 0
        else:
            host = machine.host.strip()
            user = machine.user.strip()
            if not host or not user:
                raise StewardError(
                    "An SSH machine needs a host and a user. "
                    "For this computer: steward machines add <name> --local"
                )
            port = int(machine.port)
            if port < 1 or port > 65535:
                raise StewardError("SSH port must be between 1 and 65535.")
            identity = machine.identity_file.strip() if machine.identity_file else None
            use_agent = 1 if machine.use_agent else 0
        with self._lock, self._connect() as conn:
            if machine.is_local:
                other = conn.execute(
                    "SELECT name FROM machines WHERE is_local = 1 AND name != ? COLLATE NOCASE",
                    (name,),
                ).fetchone()
                if other is not None:
                    raise StewardError(
                        f"This computer is already saved as {other['name']}. "
                        "Edit that machine, or remove it first."
                    )
            existing = conn.execute(
                "SELECT host_key_fingerprint FROM machines WHERE name = ? COLLATE NOCASE",
                (name,),
            ).fetchone()
            fingerprint = machine.host_key_fingerprint
            if existing and not fingerprint:
                fingerprint = existing["host_key_fingerprint"]
            previous = conn.execute(
                "SELECT host, user, port, is_local FROM machines WHERE name = ? COLLATE NOCASE",
                (name,),
            ).fetchone()
            conn.execute("DELETE FROM machines WHERE name = ? COLLATE NOCASE", (name,))
            conn.execute(
                """
                INSERT INTO machines (
                    name, host, user, port, identity_file, sudo_notes, use_agent,
                    host_key_fingerprint, is_local
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    host,
                    user,
                    port,
                    identity,
                    machine.sudo_notes.strip(),
                    use_agent,
                    fingerprint or "",
                    1 if machine.is_local else 0,
                ),
            )
            changed = previous is not None and (
                previous["host"] != host
                or previous["user"] != user
                or int(previous["port"]) != port
                or bool(previous["is_local"]) != bool(machine.is_local)
            )
            if changed:
                conn.execute(
                    "DELETE FROM machine_facts WHERE machine = ? COLLATE NOCASE",
                    (name,),
                )
        stored = self.get_machine(name)
        assert stored is not None
        return stored

    def delete_machine(self, name: str) -> None:
        if self.get_machine(name) is None:
            raise StewardError(f"No machine named {name}.")
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM machines WHERE name = ? COLLATE NOCASE", (name,))
            conn.execute("DELETE FROM machine_facts WHERE machine = ? COLLATE NOCASE", (name,))

    def get_facts(self, name: str) -> MachineFacts | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM machine_facts WHERE machine = ? COLLATE NOCASE",
                (name,),
            ).fetchone()
        return _facts(row) if row else None

    def save_facts(self, facts: MachineFacts) -> None:
        name = facts.machine.strip()
        if not name:
            raise StewardError("Machine facts need a machine name.")
        error = " ".join(facts.error.split())[:180]
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM machine_facts WHERE machine = ? COLLATE NOCASE", (name,))
            conn.execute(
                """
                INSERT INTO machine_facts (
                    machine, user_name, is_root, os_id, os_version, pkg, sudo_state,
                    extras, error, gathered_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    facts.user_name.strip(),
                    1 if facts.is_root else 0,
                    facts.os_id.strip(),
                    facts.os_version.strip(),
                    facts.pkg.strip(),
                    facts.sudo_state.strip(),
                    facts.extras.strip(),
                    error,
                    facts.gathered_at or utcnow(),
                ),
            )

    def set_host_key_fingerprint(self, name: str, fingerprint: str) -> None:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE machines SET host_key_fingerprint = ? WHERE name = ? COLLATE NOCASE",
                (fingerprint, name),
            )
            if cur.rowcount == 0:
                raise StewardError(f"No machine named {name}.")

    # ----- messages -------------------------------------------------------

    def add_message(
        self,
        agent_id: str,
        role: str,
        content: str,
        *,
        tool_calls: list[ToolCall] | None = None,
        tool_call_id: str | None = None,
        name: str | None = None,
    ) -> int:
        payload = None
        if tool_calls:
            payload = json.dumps(
                [{"id": call.id, "name": call.name, "arguments": call.arguments} for call in tool_calls]
            )
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO messages (
                    agent_id, role, content, tool_calls_json, tool_call_id, tool_name, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (agent_id, role, content, payload, tool_call_id, name, utcnow()),
            )
            return int(cur.lastrowid)

    def list_messages(self, agent_id: str, limit: int = 200) -> list[StoredMessage]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM messages WHERE agent_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (agent_id, limit),
            ).fetchall()
        return [_message(row) for row in reversed(rows)]

    def recent_messages(self, agent_id: str, limit: int) -> list[StoredMessage]:
        rows = self.list_messages(agent_id, limit=limit)
        while rows and rows[0].role == "tool":
            rows.pop(0)
        return rows

    # ----- notes ----------------------------------------------------------

    def list_notes(self, agent_id: str) -> list[Note]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM notes WHERE agent_id = ? ORDER BY updated_at DESC, id DESC",
                (agent_id,),
            ).fetchall()
        return [_note(row) for row in rows]

    def get_note(self, agent_id: str, note_id: int) -> Note | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM notes WHERE agent_id = ? AND id = ?", (agent_id, note_id)
            ).fetchone()
        return _note(row) if row else None

    def find_note_by_title(self, agent_id: str, title: str) -> Note | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM notes WHERE agent_id = ? AND title = ? COLLATE NOCASE",
                (agent_id, title.strip()),
            ).fetchone()
        return _note(row) if row else None

    def save_note(self, agent_id: str, title: str, content: str, tags: str = "") -> Note:
        title = title.strip()
        content = content.strip()
        if not title:
            raise StewardError("A note needs a title.")
        if not content:
            raise StewardError("A note needs some content.")
        tags = tags.strip()
        existing = self.find_note_by_title(agent_id, title)
        now = utcnow()
        if existing:
            with self._lock, self._connect() as conn:
                conn.execute(
                    "UPDATE notes SET content = ?, tags = ?, updated_at = ? WHERE id = ?",
                    (content, tags or existing.tags, now, existing.id),
                )
            note = self.get_note(agent_id, existing.id)
            assert note is not None
            return note
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO notes (agent_id, title, content, tags, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (agent_id, title, content, tags, now, now),
            )
            note_id = int(cur.lastrowid)
        note = self.get_note(agent_id, note_id)
        assert note is not None
        return note

    def update_note(
        self,
        agent_id: str,
        note_id: int,
        *,
        title: str,
        content: str,
        tags: str,
    ) -> Note:
        current = self.get_note(agent_id, note_id)
        if current is None:
            raise StewardError(f"No note {note_id} for this agent.")
        title = title.strip()
        content = content.strip()
        if not title or not content:
            raise StewardError("A note needs a title and some content.")
        other = self.find_note_by_title(agent_id, title)
        if other and other.id != note_id:
            raise StewardError(f"A note titled {title} already exists. Edit that one, or pick another title.")
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE notes SET title = ?, content = ?, tags = ?, updated_at = ? WHERE id = ?",
                (title, content, tags.strip(), utcnow(), note_id),
            )
        note = self.get_note(agent_id, note_id)
        assert note is not None
        return note

    def delete_note(self, agent_id: str, note_id: int) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM notes WHERE agent_id = ? AND id = ?", (agent_id, note_id)
            )
            return cur.rowcount > 0

    def relevant_notes(self, agent_id: str, query: str, limit: int = 8) -> list[Note]:
        notes = self.list_notes(agent_id)
        try:
            settings = Settings.load()
        except StewardError:
            settings = Settings()
        index = get_memory_index(
            settings.memory_backend,
            base_url=settings.ollama_base_url,
            embedding_model=settings.memory_embedding_model,
        )
        return index.search(notes, query, limit)

    def save_file_backup(
        self,
        agent_id: str,
        machine: str,
        path: str,
        content: bytes | None,
    ) -> FileBackup:
        backup_id = uuid.uuid4().hex
        now = utcnow()
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO file_backups
                   (id, agent_id, machine, path, existed, content, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (backup_id, agent_id, machine, path, int(content is not None), content, now),
            )
            row = conn.execute("SELECT * FROM file_backups WHERE id = ?", (backup_id,)).fetchone()
        return _file_backup(row)

    def get_file_backup(self, backup_id: str) -> FileBackup | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM file_backups WHERE id = ?", (backup_id,)).fetchone()
        return _file_backup(row) if row else None

    # ----- tool log -------------------------------------------------------

    def add_tool_event(
        self,
        agent_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        result: str,
        *,
        machine: str,
        approved: bool,
        destructive: bool,
    ) -> ToolEventRecord:
        safe_args = _public_arguments(arguments)
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO tool_events (
                    agent_id, tool_name, arguments_json, result, machine, approved, destructive, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    tool_name,
                    json.dumps(safe_args, default=str),
                    result,
                    machine,
                    1 if approved else 0,
                    1 if destructive else 0,
                    utcnow(),
                ),
            )
            event_id = int(cur.lastrowid)
            row = conn.execute("SELECT * FROM tool_events WHERE id = ?", (event_id,)).fetchone()
        return _event(row)

    def list_tool_events(self, agent_id: str, limit: int = 100) -> list[ToolEventRecord]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tool_events WHERE agent_id = ?
                ORDER BY id DESC LIMIT ?
                """,
                (agent_id, limit),
            ).fetchall()
        return [_event(row) for row in reversed(rows)]

    def backup_to(self, destination: str | Path, *, overwrite: bool = False) -> Path:
        target = Path(destination).expanduser()
        if target.resolve() == self.db_path.resolve():
            raise StewardError("Backup destination must differ from the live database.")
        if target.exists() and not overwrite:
            raise StewardError(f"Backup already exists: {target}. Pass --overwrite to replace it.")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        os.close(descriptor)
        try:
            with self._lock:
                source = self._connect()
                destination_db = sqlite3.connect(temporary)
                try:
                    source.backup(destination_db)
                finally:
                    destination_db.close()
                    source.close()
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
            target.chmod(0o600)
        except Exception as exc:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise StewardError(f"Could not back up the database to {target}: {exc}") from exc
        return target


def _migrate(conn: sqlite3.Connection) -> None:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "machines" not in tables:
        return
    columns = {row[1] for row in conn.execute("PRAGMA table_info(machines)")}
    if "is_local" not in columns:
        conn.execute("ALTER TABLE machines ADD COLUMN is_local INTEGER NOT NULL DEFAULT 0")


def _validate_name(name: str, label: str) -> str:
    clean = " ".join(name.strip().split())
    if not _NAME_OK.match(clean):
        raise StewardError(
            f"{label} must start with a letter and use only letters, numbers, spaces, hyphens, or underscores (max 40)."
        )
    return clean


def _validate_choice(value: str, allowed: tuple[str, ...], label: str) -> str:
    if value not in allowed:
        raise StewardError(f"Unknown {label} '{value}'. Use one of: {', '.join(allowed)}.")
    return value


def _public_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    cleaned = {}
    for key, value in arguments.items():
        if "password" in key.lower() or "secret" in key.lower() or "token" in key.lower():
            continue
        cleaned[key] = value
    return cleaned


def _agent(row: sqlite3.Row) -> Agent:
    return Agent(
        id=row["id"],
        name=row["name"],
        persona=row["persona"],
        backend=row["backend"],
        model=row["model"],
        approval_mode=row["approval_mode"],
        tool_protocol=row["tool_protocol"],
        max_steps=row["max_steps"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _facts(row: sqlite3.Row) -> MachineFacts:
    return MachineFacts(
        machine=row["machine"],
        user_name=row["user_name"],
        is_root=bool(row["is_root"]),
        os_id=row["os_id"],
        os_version=row["os_version"],
        pkg=row["pkg"],
        sudo_state=row["sudo_state"],
        extras=row["extras"],
        error=row["error"],
        gathered_at=row["gathered_at"],
    )


def _machine(row: sqlite3.Row) -> Machine:
    return Machine(
        name=row["name"],
        host=row["host"],
        user=row["user"],
        port=row["port"],
        identity_file=row["identity_file"],
        sudo_notes=row["sudo_notes"],
        use_agent=bool(row["use_agent"]),
        host_key_fingerprint=row["host_key_fingerprint"],
        is_local=bool(row["is_local"]) if "is_local" in row.keys() else False,
    )


def _message(row: sqlite3.Row) -> StoredMessage:
    calls: list[ToolCall] = []
    if row["tool_calls_json"]:
        for item in json.loads(row["tool_calls_json"]):
            arguments = item.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {"_raw": arguments}
            calls.append(ToolCall(id=item.get("id") or "", name=item.get("name") or "", arguments=arguments))
    return StoredMessage(
        id=row["id"],
        agent_id=row["agent_id"],
        role=row["role"],
        content=row["content"],
        tool_calls=calls,
        tool_call_id=row["tool_call_id"],
        name=row["tool_name"],
        created_at=row["created_at"],
    )


def _note(row: sqlite3.Row) -> Note:
    return Note(
        id=row["id"],
        agent_id=row["agent_id"],
        title=row["title"],
        content=row["content"],
        tags=row["tags"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _event(row: sqlite3.Row) -> ToolEventRecord:
    return ToolEventRecord(
        id=row["id"],
        agent_id=row["agent_id"],
        tool_name=row["tool_name"],
        arguments=json.loads(row["arguments_json"]),
        result=row["result"],
        machine=row["machine"],
        approved=bool(row["approved"]),
        destructive=bool(row["destructive"]),
        created_at=row["created_at"],
    )


def _file_backup(row: sqlite3.Row) -> FileBackup:
    return FileBackup(
        id=row["id"],
        agent_id=row["agent_id"],
        machine=row["machine"],
        path=row["path"],
        existed=bool(row["existed"]),
        content=bytes(row["content"]) if row["content"] is not None else None,
        created_at=row["created_at"],
    )
