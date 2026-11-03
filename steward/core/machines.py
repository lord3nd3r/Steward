"""Resolve a saved machine. Nothing is built in, including this computer."""

from __future__ import annotations

from steward.core.errors import StewardError
from steward.core.messages import NO_MACHINES_MESSAGE
from steward.core.models import Machine


def resolve_machine(store, name: object) -> Machine:
    """Pick a saved machine.

    With one machine, an empty name uses it. With several, the name is required.
    With none, the error tells the user how to add this computer.
    """
    machines = store.list_machines()
    if not machines:
        raise StewardError(NO_MACHINES_MESSAGE)
    text = name.strip() if isinstance(name, str) else ""
    if not text:
        if len(machines) == 1:
            return machines[0]
        known = ", ".join(item.name for item in machines)
        raise StewardError(f"Name the machine. Known machines: {known}.")
    found = store.get_machine(text)
    if found is None:
        known = ", ".join(item.name for item in machines)
        raise StewardError(f"Unknown machine '{text}'. Known machines: {known}.")
    return found
