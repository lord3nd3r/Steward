"""System prompt assembly.

Machine facts and matching notes are injected every turn. The facts themselves
are cached; see ``steward.core.facts``. With no saved machines the prompt is
only the instruction to add this computer, so command rules stay out of the
context until they can be used.
"""

from __future__ import annotations

from steward.core.facts import fact_line
from steward.core.messages import NO_MACHINES_MESSAGE
from steward.core.models import Agent, Machine, MachineFacts, Note


def build_system_prompt(
    agent: Agent,
    notes: list[Note],
    machines: list[Machine],
    facts: list[MachineFacts] | None = None,
) -> str:
    note_block = _notes_block(notes)
    persona = agent.persona.strip()
    persona_block = f"Persona:\n{persona}\n\n" if persona else ""
    if not machines:
        return _no_machines_prompt(agent.name, persona_block, note_block)
    by_name = {item.machine.casefold(): item for item in (facts or [])}
    machine_lines = []
    for machine in machines:
        summary = fact_line(by_name.get(machine.name.casefold()))
        extra = f" Sudo notes: {machine.sudo_notes}" if machine.sudo_notes else ""
        if machine.is_local:
            machine_lines.append(f"- {machine.name}: this computer. {summary}.{extra}")
        else:
            key = machine.identity_file or "ssh-agent or default keys"
            machine_lines.append(
                f"- {machine.name}: {machine.user}@{machine.host}:{machine.port} via SSH ({key}). {summary}.{extra}"
            )
    heading = (
        "Machines (use these names; the only one is the default):"
        if len(machines) == 1
        else "Machines (use these names):"
    )
    return f"""You are {agent.name}, a local Linux sysadmin. Use tools to investigate and act. Do not stop at advice.

{persona_block}{heading}
{chr(10).join(machine_lines)}

Notes (memory the user can read):
{note_block}

Rules:
- Use the machine line for distro, package manager, and privilege. Root: no sudo. Otherwise put `sudo` or `doas` (whichever the line names) on installs, upgrades, services, and system files. Example: `sudo DEBIAN_FRONTEND=noninteractive apt-get -y full-upgrade`. Steward asks approval and the password. Never ask for a password in chat, and never store secrets.
- Non-interactive flags: apt `-y` and `DEBIAN_FRONTEND=noninteractive`; dnf/yum `-y`; pacman `--noconfirm`; zypper `-n`.
- Fully up to date: update then upgrade (apt `update && full-upgrade`; dnf `upgrade`; pacman `-Syu`; zypper `up`; apk `upgrade`), then `flatpak update -y` and `snap refresh` if the machine line lists them. Report what changed and if a reboot is required (`/var/run/reboot-required`, or running kernel vs the installed one).
- Troubleshoot before changing. For a device such as a webcam: `lsusb`, `v4l2-ctl --list-devices`, `dmesg`, `journalctl -b`, `groups`, and permissions.
- After a change, check the resulting state with an appropriate read-only tool. Report what the check confirms and what remains unverified. A successful command alone does not prove the problem is fixed.
- One short sentence, then the tool. Use the result; do not invent output. A permission-denied hint means retry once with sudo. If the user denied the action, stop.
- `save_note` for a durable fact; reuse the title to update it. File tools run as the login user, not root.

No native tools? Emit only:
<tool_call>
{{"name": "tool_name", "arguments": {{"arg": "value"}}}}
</tool_call>
Final answer is plain text, no tool_call. Be concise. Lead with the result.
"""


def _no_machines_prompt(name: str, persona_block: str, note_block: str) -> str:
    return f"""You are {name}, a local assistant. You have no machines, so you cannot run commands or touch files.

{persona_block}Tell the user exactly this, and do not invent a machine:
{NO_MACHINES_MESSAGE}

Notes (memory the user can read):
{note_block}

Rules:
- `save_note` for a durable fact; reuse the title to update it. Never store secrets.
- One short sentence, then the tool. Do not invent output. If the user denied an action, stop.

No native tools? Emit only:
<tool_call>
{{"name": "tool_name", "arguments": {{"arg": "value"}}}}
</tool_call>
Final answer is plain text, no tool_call. Be concise. Lead with the result.
"""


def _notes_block(notes: list[Note]) -> str:
    if not notes:
        return "- (none yet)"
    lines = []
    for note in notes:
        tags = f" [{note.tags}]" if note.tags else ""
        lines.append(f"- ({note.id}) {note.title}{tags}: {note.content}")
    return "\n".join(lines)
