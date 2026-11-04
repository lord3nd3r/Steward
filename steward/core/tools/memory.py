"""Per-agent notes: save, search, and forget."""

from __future__ import annotations

from typing import Any

from steward.core.errors import StewardError
from steward.core.tools.base import Tool, ToolContext


class SaveNoteTool(Tool):
    name = "save_note"
    description = (
        "Remember a durable fact or preference about the user or their machines. "
        "Saving again with the same title updates that note. "
        "Never store passwords, keys, or tokens."
    )
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short label, reused when the fact changes."},
            "content": {"type": "string", "description": "The fact itself, in a sentence or two."},
            "tags": {"type": "string", "description": "Optional comma-separated tags."},
        },
        "required": ["title", "content"],
    }
    approval_class = "safe"

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        title = arguments.get("title")
        content = arguments.get("content")
        tags = arguments.get("tags") or ""
        if not isinstance(title, str) or not isinstance(content, str):
            return "Error: title and content are required."
        try:
            note = context.store.save_note(context.agent_id, title, content, str(tags))
        except StewardError as exc:
            return f"Error: {exc}"
        return f"Saved note #{note.id} ({note.title})."


class SearchNotesTool(Tool):
    name = "search_notes"
    description = "Search this agent's notes using the configured keyword or embedding index."
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Words to look for in titles, tags, and note text."},
        },
        "required": ["query"],
    }
    approval_class = "safe"

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            return "Error: query is required."
        notes = context.store.relevant_notes(context.agent_id, query.strip(), limit=8)
        # relevant_notes also pads with recent notes; for an explicit search,
        # prefer actual keyword hits and say so when nothing overlapped.
        hits = context.store.list_notes(context.agent_id)
        query_lower = query.lower()
        matched = [
            note
            for note in hits
            if query_lower in f"{note.title}\n{note.content}\n{note.tags}".lower()
            or any(word.lower() in f"{note.title} {note.content} {note.tags}".lower() for word in query.split() if len(word) >= 3)
        ]
        chosen = matched or notes
        if not chosen:
            return "No notes saved yet."
        if not matched:
            header = "No exact keyword match. Related notes:\n"
        else:
            header = ""
        lines = [f"#{note.id} {note.title}: {note.content}" for note in chosen[:8]]
        return header + "\n".join(lines)


class ForgetNoteTool(Tool):
    name = "forget_note"
    description = "Delete one note by id or by exact title. Use this when a saved fact is wrong."
    parameters = {
        "type": "object",
        "properties": {
            "note_id": {"type": "integer", "description": "Note id from search_notes or the system prompt."},
            "title": {"type": "string", "description": "Exact note title, if you do not have the id."},
        },
    }
    approval_class = "mutate"

    def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        note_id = arguments.get("note_id")
        title = arguments.get("title")
        if isinstance(note_id, str) and note_id.isdigit():
            note_id = int(note_id)
        target = None
        if isinstance(note_id, int):
            target = context.store.get_note(context.agent_id, note_id)
        elif isinstance(title, str) and title.strip():
            target = context.store.find_note_by_title(context.agent_id, title)
        else:
            return "Error: provide note_id or title."
        if target is None:
            return "No note found with that id or title."
        context.store.delete_note(context.agent_id, target.id)
        return f"Forgot note #{target.id} ({target.title})."
