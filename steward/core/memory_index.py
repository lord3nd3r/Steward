"""Keyword and optional Ollama embedding retrieval for agent notes."""

from __future__ import annotations

import re
import math
from typing import Protocol

import httpx

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
    """Rank notes with Ollama embeddings, falling back to keyword search."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.transport = transport

    def search(self, notes: list[Note], query: str, limit: int) -> list[Note]:
        if limit <= 0 or not notes:
            return []
        texts = [f"{note.title}\n{note.content}\n{note.tags}" for note in notes]
        try:
            kwargs = {"base_url": self.base_url, "timeout": 30.0}
            if self.transport is not None:
                kwargs["transport"] = self.transport
            with httpx.Client(**kwargs) as client:
                response = client.post(
                    "/api/embed",
                    json={"model": self.model, "input": [query, *texts]},
                )
                response.raise_for_status()
                embeddings = response.json().get("embeddings")
            vectors = [[float(value) for value in vector] for vector in embeddings]
            if len(vectors) != len(notes) + 1 or not vectors[0]:
                raise ValueError("Ollama returned an unexpected embedding count.")
            query_vector = vectors[0]
            ranked = []
            for note, vector in zip(notes, vectors[1:]):
                if len(vector) != len(query_vector):
                    raise ValueError("Ollama returned embeddings with different dimensions.")
                score = _cosine_similarity(query_vector, vector)
                ranked.append((score, note.updated_at, note))
            ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
            return [note for _score, _updated, note in ranked[:limit]]
        except (httpx.HTTPError, TypeError, ValueError, KeyError):
            return KeywordMemoryIndex().search(notes, query, limit)


def get_memory_index(
    backend_name: str,
    *,
    base_url: str = "",
    embedding_model: str = "",
) -> MemoryIndex:
    if backend_name in {"", "keyword"}:
        return KeywordMemoryIndex()
    if backend_name == "ollama" and base_url and embedding_model:
        return OllamaEmbeddingIndex(base_url, embedding_model)
    # Unknown / future backends stay on keywords until they are implemented.
    return KeywordMemoryIndex()


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)
