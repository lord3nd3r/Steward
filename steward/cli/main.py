"""Steward's command line. The GUI uses the same core."""

from __future__ import annotations

import functools
import getpass
import os
import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from steward import __version__
from steward.core.errors import StewardError
from steward.core.facts import describe, fact_line, refresh_machine_facts
from steward.core.messages import NO_AGENTS_MESSAGE, NO_MACHINES_MESSAGE
from steward.core.llm.base import list_backend_models
from steward.core.models import (
    APPROVAL_LABELS,
    BACKEND_LABELS,
    ApprovalDecision,
    ApprovalRequest,
    Machine,
)
from steward.core.paths import config_path, database_path
from steward.core.service import open_steward

console = Console(highlight=False)
app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    invoke_without_command=True,
    help="Local multi-agent assistant. Models stay on your machines.",
)
agents_app = typer.Typer(no_args_is_help=True, help="Create and manage agents. A new install has none.")
machines_app = typer.Typer(
    no_args_is_help=True,
    help="Machines you add yourself, including this computer: steward machines add <name> --local",
)
notes_app = typer.Typer(no_args_is_help=True, help="Read and edit an agent's memory.")
app.add_typer(agents_app, name="agents")
app.add_typer(machines_app, name="machines")
app.add_typer(notes_app, name="notes")


def guarded(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except StewardError as exc:
            console.print(f"[red]Error:[/red] {exc}")
            raise typer.Exit(code=1) from exc
        except KeyboardInterrupt:
            console.print("\nStopped.")
            raise typer.Exit(code=130) from None

    return wrapper


def _version(value: bool) -> None:
    if value:
        console.print(f"steward {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", help="Show the version and exit.", callback=_version, is_eager=True),
) -> None:
    """Local multi-agent assistant. A new install has no agents and no machines."""
    if os.environ.get("STEWARD_DEBUG"):
        import logging

        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")
    if ctx.invoked_subcommand is None:
        _print_empty_state(open_steward())


@app.command("gui")
@guarded
def gui_command() -> None:
    """Open the desktop window."""
    from steward.gui.app import main as gui_main

    gui_main()


@app.command("chat")
@guarded
def chat(agent: str = typer.Option(..., "--agent", "-a", help="Agent name.")) -> None:
    """Talk to an agent. The agent can run tools between replies."""
    steward = open_steward()
    record = steward.store.require_agent(agent)
    if not steward.store.list_machines():
        console.print(f"[yellow]{NO_MACHINES_MESSAGE}[/yellow]")
    console.print(
        f"[bold]{record.name}[/bold]  "
        f"[dim]{BACKEND_LABELS.get(record.backend, record.backend)} · {record.model} · "
        f"{APPROVAL_LABELS.get(record.approval_mode, record.approval_mode)}[/dim]"
    )
    console.print("[dim]/notes  /forget  /help  /quit[/dim]")
    while True:
        try:
            text = console.input(f"[bold]you[/bold] [dim]›[/dim] ")
        except EOFError:
            console.print()
            return
        command = text.strip()
        if not command:
            continue
        if command in {"/quit", "/exit"}:
            return
        if command == "/help":
            console.print("Ask in plain language. /notes lists memory, /forget <id> deletes a note, /quit leaves.")
            continue
        if command == "/notes":
            _print_notes(steward, record.id)
            continue
        if command.startswith("/forget"):
            parts = command.split(maxsplit=1)
            if len(parts) != 2 or not parts[1].isdigit():
                console.print("Usage: /forget <id>")
                continue
            if steward.store.delete_note(record.id, int(parts[1])):
                console.print(f"Forgot note {parts[1]}.")
            else:
                console.print("No note with that id.")
            continue
        _run_turn(steward, record.id, command)


@app.command("ask")
@guarded
def ask(
    prompt: str = typer.Argument(..., help="What the agent should do."),
    agent: str = typer.Option(..., "--agent", "-a", help="Agent name."),
) -> None:
    """One turn: send a prompt, approve any actions, print the answer."""
    steward = open_steward()
    record = steward.store.require_agent(agent)
    if not steward.store.list_machines():
        console.print(NO_MACHINES_MESSAGE)
    _run_turn(steward, record.id, prompt)


@agents_app.command("list")
@guarded
def agents_list() -> None:
    """List agents."""
    steward = open_steward()
    rows = steward.store.list_agents()
    if not rows:
        console.print(NO_AGENTS_MESSAGE)
        return
    table = Table(title="Agents")
    table.add_column("Name")
    table.add_column("Backend")
    table.add_column("Model")
    table.add_column("Approval")
    table.add_column("Steps", justify="right")
    for agent in rows:
        table.add_row(
            agent.name,
            BACKEND_LABELS.get(agent.backend, agent.backend),
            agent.model,
            APPROVAL_LABELS.get(agent.approval_mode, agent.approval_mode),
            str(agent.max_steps),
        )
    console.print(table)


@agents_app.command("create")
@guarded
def agents_create(
    name: str = typer.Option(..., "--name", "-n", prompt="Agent name"),
    model: str = typer.Option(..., "--model", "-m", prompt="Model name"),
    backend: str = typer.Option("ollama", "--backend", "-b", help="ollama or llama_cpp."),
    persona: str = typer.Option("", "--persona", "-p", help="Who this agent is."),
    approval: str = typer.Option("ask", "--approval", help="ask, auto_readonly, or full_auto."),
    protocol: str = typer.Option("auto", "--protocol", help="auto, native, or text."),
    max_steps: int = typer.Option(0, "--max-steps", help="Tool steps per turn. 0 uses the default."),
) -> None:
    """Create an agent with its own model, persona, and memory."""
    steward = open_steward()
    steps = None if max_steps == 0 else max_steps
    agent = steward.store.create_agent(
        name,
        persona=persona,
        backend=backend,
        model=model,
        approval_mode=approval,
        tool_protocol=protocol,
        max_steps=steps,
    )
    console.print(f"Created [bold]{agent.name}[/bold] on {agent.backend} / {agent.model}.")


@agents_app.command("edit")
@guarded
def agents_edit(
    name: str = typer.Argument(..., help="Agent name."),
    model: str | None = typer.Option(None, "--model", "-m"),
    backend: str | None = typer.Option(None, "--backend", "-b"),
    persona: str | None = typer.Option(None, "--persona", "-p"),
    approval: str | None = typer.Option(None, "--approval"),
    protocol: str | None = typer.Option(None, "--protocol"),
    max_steps: int | None = typer.Option(None, "--max-steps"),
    rename: str | None = typer.Option(None, "--rename"),
) -> None:
    """Change an agent's model, persona, or approval mode."""
    steward = open_steward()
    agent = steward.store.require_agent(name)
    fields = {}
    if rename:
        fields["name"] = rename
    if model:
        fields["model"] = model
    if backend:
        fields["backend"] = backend
    if persona is not None:
        fields["persona"] = persona
    if approval:
        fields["approval_mode"] = approval
    if protocol:
        fields["tool_protocol"] = protocol
    if max_steps is not None:
        fields["max_steps"] = max_steps
    if not fields:
        raise StewardError("Pass at least one of --model, --backend, --persona, --approval, --protocol, --max-steps, --rename.")
    updated = steward.store.update_agent(agent.id, **fields)
    console.print(f"Updated [bold]{updated.name}[/bold].")


@agents_app.command("delete")
@guarded
def agents_delete(
    name: str = typer.Argument(..., help="Agent name."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Delete without asking."),
) -> None:
    """Delete an agent, its transcript, and its notes."""
    if not yes:
        typer.confirm(f"Delete {name}, including notes and conversation?", abort=True)
    open_steward().store.delete_agent(name)
    console.print(f"Deleted {name}.")


@agents_app.command("clear-history")
@guarded
def agents_clear_history(
    name: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Delete the conversation and tool log. Notes are kept."""
    if not yes:
        typer.confirm(f"Clear conversation history for {name}? Notes are kept.", abort=True)
    open_steward().store.clear_history(name)
    console.print(f"Cleared history for {name}.")


@machines_app.command("list")
@guarded
def machines_list() -> None:
    """List machines you have added. Nothing is included automatically."""
    steward = open_steward()
    if not steward.store.list_machines():
        console.print(NO_MACHINES_MESSAGE)
        return
    table = Table(title="Machines")
    table.add_column("Name")
    table.add_column("Target")
    table.add_column("Key")
    table.add_column("Facts")
    table.add_column("Sudo notes")
    for machine in steward.store.list_machines():
        if machine.is_local:
            target = "this computer"
            key = "—"
        else:
            target = f"{machine.user}@{machine.host}:{machine.port}"
            key = machine.identity_file or ("ssh-agent" if machine.use_agent else "default keys")
        table.add_row(
            machine.name,
            target,
            key,
            _cached_facts(steward, machine.name),
            machine.sudo_notes,
        )
    console.print(table)


@machines_app.command("add")
@guarded
def machines_add(
    name: str = typer.Argument(..., help="Name you choose."),
    local: bool = typer.Option(False, "--local", help="This computer. No SSH host."),
    host: str = typer.Option("", "--host"),
    user: str = typer.Option("", "--user"),
    port: int = typer.Option(22, "--port"),
    identity_file: str = typer.Option("", "--identity-file", help="Private key path. Leave empty to use ssh-agent."),
    sudo_notes: str = typer.Option("", "--sudo-notes", help="How sudo works here. Do not put the password."),
    no_agent: bool = typer.Option(False, "--no-agent", help="Do not use ssh-agent."),
) -> None:
    """Add or update a machine. Use --local for this computer."""
    machine = Machine(
        name=name,
        host=host,
        user=user,
        port=port,
        identity_file=identity_file or None,
        sudo_notes=sudo_notes,
        use_agent=not no_agent,
        is_local=local,
    )
    stored = open_steward().store.upsert_machine(machine)
    if stored.is_local:
        console.print(f"Saved [bold]{stored.name}[/bold] (this computer).")
    else:
        console.print(f"Saved [bold]{stored.name}[/bold] ({stored.user}@{stored.host}:{stored.port}).")


@machines_app.command("remove")
@guarded
def machines_remove(
    name: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Remove a machine from the registry. This does not touch the machine itself."""
    if not yes:
        typer.confirm(f"Remove {name} from Steward?", abort=True)
    open_steward().store.delete_machine(name)
    console.print(f"Removed {name}.")


@machines_app.command("refresh")
@guarded
def machines_refresh(
    name: str | None = typer.Argument(None, help="Machine name. Omit to refresh every saved machine."),
) -> None:
    """Re-read distro, package manager, and sudo facts. They are cached until the next refresh."""
    found = refresh_machine_facts(open_steward().store, name)
    for item in found:
        console.print(describe(item))


def _cached_facts(steward, name: str) -> str:
    facts = steward.store.get_facts(name)
    if facts is None:
        return "not gathered"
    return fact_line(facts)


@notes_app.command("list")
@guarded
def notes_list(agent: str = typer.Option(..., "--agent", "-a")) -> None:
    """List an agent's notes."""
    steward = open_steward()
    record = steward.store.require_agent(agent)
    _print_notes(steward, record.id)


@notes_app.command("add")
@guarded
def notes_add(
    agent: str = typer.Option(..., "--agent", "-a"),
    title: str = typer.Option(..., "--title", "-t", prompt=True),
    content: str = typer.Option(..., "--content", "-c", prompt=True),
    tags: str = typer.Option("", "--tags"),
) -> None:
    """Add a note, or update one that already has this title."""
    steward = open_steward()
    record = steward.store.require_agent(agent)
    note = steward.store.save_note(record.id, title, content, tags)
    console.print(f"Saved note #{note.id} ({note.title}).")


@notes_app.command("edit")
@guarded
def notes_edit(
    note_id: int = typer.Argument(..., help="Note id."),
    agent: str = typer.Option(..., "--agent", "-a"),
    title: str | None = typer.Option(None, "--title", "-t"),
    content: str | None = typer.Option(None, "--content", "-c"),
    tags: str | None = typer.Option(None, "--tags"),
) -> None:
    """Edit a note by id."""
    steward = open_steward()
    record = steward.store.require_agent(agent)
    current = steward.store.get_note(record.id, note_id)
    if current is None:
        raise StewardError(f"No note {note_id} for {record.name}.")
    updated = steward.store.update_note(
        record.id,
        note_id,
        title=title if title is not None else current.title,
        content=content if content is not None else current.content,
        tags=tags if tags is not None else current.tags,
    )
    console.print(f"Updated note #{updated.id}.")


@notes_app.command("search")
@guarded
def notes_search(
    query: str = typer.Argument(...),
    agent: str = typer.Option(..., "--agent", "-a"),
) -> None:
    """Search an agent's notes."""
    steward = open_steward()
    record = steward.store.require_agent(agent)
    notes = steward.store.relevant_notes(record.id, query, limit=10)
    if not notes:
        console.print("No notes yet.")
        return
    for note in notes:
        console.print(f"[bold]#{note.id} {note.title}[/bold]  [dim]{note.tags}[/dim]\n{note.content}\n")


@notes_app.command("delete")
@guarded
def notes_delete(
    note_id: int = typer.Argument(...),
    agent: str = typer.Option(..., "--agent", "-a"),
    yes: bool = typer.Option(False, "--yes", "-y"),
) -> None:
    """Delete one note."""
    steward = open_steward()
    record = steward.store.require_agent(agent)
    if not yes:
        typer.confirm(f"Delete note {note_id}?", abort=True)
    if not steward.store.delete_note(record.id, note_id):
        raise StewardError(f"No note {note_id} for {record.name}.")
    console.print(f"Deleted note {note_id}.")


@app.command("models")
@guarded
def models(backend: str = typer.Option("ollama", "--backend", "-b", help="ollama or llama_cpp.")) -> None:
    """List models the configured server currently has."""
    steward = open_steward()
    if backend not in {"ollama", "llama_cpp"}:
        raise StewardError("Backend must be ollama or llama_cpp.")
    names = list_backend_models(steward.settings, backend)
    if not names:
        console.print("The server answered, but it has no models.")
        return
    for name in names:
        console.print(name)


@app.command("doctor")
@guarded
def doctor() -> None:
    """Check config paths and whether Ollama or llama-server answers."""
    steward = open_steward()
    console.print(f"Config   {config_path()}")
    console.print(f"Database {database_path()}")
    agents = steward.store.list_agents()
    machines = steward.store.list_machines()
    console.print(f"Agents   {len(agents)}")
    if not agents:
        console.print(NO_AGENTS_MESSAGE)
    console.print(f"Machines {len(machines)}")
    if not machines:
        console.print(NO_MACHINES_MESSAGE)
    _probe("Ollama", steward.settings.ollama_base_url, lambda: list_backend_models(steward.settings, "ollama"))
    _probe("llama.cpp", steward.settings.llama_cpp_base_url, lambda: list_backend_models(steward.settings, "llama_cpp"))


@app.command("install-desktop")
@guarded
def install_desktop() -> None:
    """Install a launcher into ~/.local/share/applications."""
    from importlib.resources import as_file, files

    applications = Path.home() / ".local" / "share" / "applications"
    icon_dir = Path.home() / ".local" / "share" / "icons" / "hicolor" / "scalable" / "apps"
    applications.mkdir(parents=True, exist_ok=True)
    icon_dir.mkdir(parents=True, exist_ok=True)
    with as_file(files("steward.assets").joinpath("steward.svg")) as svg_src:
        icon_target = icon_dir / "steward.svg"
        shutil.copyfile(svg_src, icon_target)
    with as_file(files("steward.assets").joinpath("steward.desktop")) as desktop_src:
        text = desktop_src.read_text(encoding="utf-8")
    gui_bin = shutil.which("steward-gui") or "steward-gui"
    text = text.replace("Exec=steward-gui", f"Exec={gui_bin}")
    text = text.replace("Icon=steward", f"Icon={icon_dir / 'steward.svg'}")
    target = applications / "steward.desktop"
    target.write_text(text, encoding="utf-8")
    console.print(f"Installed {target}")
    console.print("If the launcher does not appear, log out or run: update-desktop-database ~/.local/share/applications")


def _probe(label: str, url: str, fn) -> None:
    try:
        names = fn()
    except StewardError as exc:
        console.print(f"[yellow]{label}[/yellow]  {url}  unreachable ({exc})")
        return
    preview = ", ".join(names[:5]) if names else "no models"
    extra = f" (+{len(names) - 5} more)" if len(names) > 5 else ""
    console.print(f"[green]{label}[/green]  {url}  {preview}{extra}")


def _print_notes(steward, agent_id: str) -> None:
    notes = steward.store.list_notes(agent_id)
    if not notes:
        console.print("No notes yet. The agent writes them with save_note, or add one with [bold]steward notes add[/bold].")
        return
    for note in notes:
        tags = f"  [dim]{note.tags}[/dim]" if note.tags else ""
        console.print(f"[bold]#{note.id} {note.title}[/bold]{tags}\n{note.content}\n")


def _run_turn(steward, agent_id: str, prompt: str) -> None:
    state = {"inline": False}

    def on_token(token: str) -> None:
        console.print(token, end="", soft_wrap=True)
        state["inline"] = True

    def on_segment(_text: str) -> None:
        if state["inline"]:
            console.print()
            state["inline"] = False

    def on_status(text: str) -> None:
        if state["inline"]:
            console.print()
            state["inline"] = False
        console.print(f"[dim]{text}[/dim]")

    def on_tool(event) -> None:
        if state["inline"]:
            console.print()
            state["inline"] = False
        border = "red" if event.destructive else ("yellow" if not event.approved else "green")
        label = "denied" if not event.approved else "ran"
        body = Text()
        command = event.arguments.get("command") or event.arguments.get("path") or ""
        if not command and event.tool_name == "save_note":
            command = f"{event.arguments.get('title')}: {event.arguments.get('content')}"
        if command:
            body.append(str(command)[:2000] + "\n", style="bold")
        shown = event.result if len(event.result) <= 8000 else event.result[:8000] + "\n…[output truncated for the terminal]"
        body.append(shown)
        console.print(Panel(body, title=f"{event.tool_name} · {event.machine} · {label}", border_style=border))

    result = steward.runner.run_turn(
        agent_id,
        prompt,
        on_token=on_token,
        on_segment=on_segment,
        on_tool=on_tool,
        on_status=on_status,
        approval_handler=approve_on_cli,
    )
    if state["inline"]:
        console.print()
    if result.cancelled:
        console.print("[dim]Stopped.[/dim]")


def approve_on_cli(request: ApprovalRequest) -> ApprovalDecision:
    """Prompt on the terminal. Destructive commands default to no."""
    title = "Sudo password" if request.password_only and not request.destructive else "Approve action"
    border = "red" if request.destructive else "yellow"
    body = Text()
    body.append(f"{request.tool_name}  on  {request.machine}\n\n", style="bold")
    body.append(request.summary[:4000])
    body.append(f"\n\n{request.reason}")
    console.print(Panel(body, title=title, border_style=border))
    if request.destructive:
        console.print("[bold red]This looks destructive. Steward asks even when the agent is in full-auto.[/bold red]")
    try:
        approved = typer.confirm("Approve?", default=bool(request.password_only and not request.destructive))
    except (typer.Abort, typer.Exit, KeyboardInterrupt):
        console.print("Denied.")
        return ApprovalDecision(approved=False)
    if not approved:
        return ApprovalDecision(approved=False)
    password = None
    if request.needs_sudo:
        try:
            entered = getpass.getpass("Sudo password (empty to skip, it is not stored): ")
        except (EOFError, KeyboardInterrupt):
            return ApprovalDecision(approved=False)
        password = entered or None
    return ApprovalDecision(approved=True, sudo_password=password)


def _print_empty_state(steward) -> None:
    """First-run text. Printed only when `steward` is started with no command."""
    agents = steward.store.list_agents()
    machines = steward.store.list_machines()
    if not agents:
        console.print(NO_AGENTS_MESSAGE)
    if not machines:
        console.print(NO_MACHINES_MESSAGE)
    console.print("Use steward --help to see commands.")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
