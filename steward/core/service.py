"""The object the CLI and the GUI both hold."""

from __future__ import annotations

from steward.core.agent import AgentRunner
from steward.core.llm.base import build_backend
from steward.core.settings import Settings
from steward.core.store import Store
from steward.core.tools import default_registry


class Steward:
    def __init__(self, store: Store | None = None, settings: Settings | None = None, registry=None, backend_factory=None) -> None:
        self.store = store or Store()
        self.settings = settings or Settings.load()
        self.registry = registry or default_registry()
        self.backend_factory = backend_factory or build_backend
        self.runner = AgentRunner(self.store, self.settings, self.registry, self.backend_factory)

    def reload_settings(self) -> Settings:
        self.settings = Settings.load()
        self.runner.settings = self.settings
        return self.settings


def open_steward() -> Steward:
    return Steward()
