"""Keep every test off the real Steward config and database."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("STEWARD_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("STEWARD_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("STEWARD_DISABLE_PLUGINS", "1")
    monkeypatch.delenv("STEWARD_OLLAMA_URL", raising=False)
    monkeypatch.delenv("STEWARD_LLAMA_CPP_URL", raising=False)
    return tmp_path
