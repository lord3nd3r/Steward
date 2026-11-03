"""Note retrieval.

Keyword search is the MVP. ``MemoryIndex`` is the extension point for a later
embedding ranker (Ollama ``/api/embeddings``). Select it with
``memory.backend`` in the config; unknown values fall back to keywords and
the setting is left in place so a future build can pick it up.
"""

from __future__ import annotations

import re
from typing import Protocol

from steward.core.models import Note


class MemoryIndex(Protocol):
    def search(self, notes: list[Note], query: str, limit: int) -> list[Note]:
        """Return the notes most relevant to ``query``, newest used as a tiebreak."""


class KeywordMemoryIndex:
    """Score notes by overlapping words. Recent notes fill the list when a
    query has little vocabulary in common with what was saved, so preferences
    still show up on an unrelated question."""

    def search(self, notes: list[Note], query: str, limit: int) -> list[Note]:
        if limit <= 0 or not notes:
            return []
        tokens = [token.lower() for token in re.findall(r"[A-Za-z0-9_'-]{3,}", query)]
        phrase = query.strip().lower()
        scored: list[tuple[int, str, Note]] = []
        for note in notes:
            haystack = f"{note.title}\n{note.content}\n{note.tags}".lower()
            score = sum(1 for token in tokens if token in haystack)
            if phrase and phrase in haystack:
                score += 3
            scored.append((score, note.updated_at, note))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        chosen = [note for score, _, note in scored if score > 0][:limit]
        if len(chosen) < min(3, limit):
            recent = sorted(notes, key=lambda note: note.updated_at, reverse=True)
            for note in recent:
                if note not in chosen:
                    chosen.append(note)
                if len(chosen) >= min(3, len(notes)):
                    break
        return chosen[:limit]


class OllamaEmbeddingIndex:
    """Hook for a future embedding ranker. Not used by the agent loop.

    A later version can embed ``note.title + note.content`` with
    ``POST {base_url}/api/embeddings`` and rank by cosine similarity against
    the user message. Keep ``KeywordMemoryIndex`` as the fallback when the
    embed server is down.
    """

    def __init__(self, base_url: str, model: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model

    def search(self, notes: list[Note], query: str, limit: int) -> list[Note]:
        raise NotImplementedError(
            "Ollama embedding search is not implemented yet. "
            "Leave memory.backend set to 'keyword'."
        )


def get_memory_index(backend_name: str) -> MemoryIndex:
    if backend_name in {"", "keyword"}:
        return KeywordMemoryIndex()
    # Unknown / future backends stay on keywords until they are implemented.
    return KeywordMemoryIndex()
