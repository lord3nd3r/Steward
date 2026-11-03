"""Shared helpers. Names here are fixtures, not shipped defaults."""

from steward.core.models import Machine


def add_computer(store, name: str = "box"):
    return store.upsert_machine(Machine(name=name, is_local=True))
