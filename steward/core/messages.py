"""User-facing sentences shared by the CLI, the GUI, and the agent.

A fresh install has no agents and no machines. These sentences are the
migration path: nothing is inserted for the user, and the text says how to
add this computer.
"""

NO_AGENTS_MESSAGE = (
    "No agents yet. Create your first agent with:\n"
    "steward agents create --name <name> --model <model>"
)

NO_MACHINES_MESSAGE = (
    "No machines configured. Add this computer with:\n"
    "steward machines add <name> --local"
)

MACHINE_TOOLS = frozenset({"run_command", "read_file", "write_file", "list_directory"})
