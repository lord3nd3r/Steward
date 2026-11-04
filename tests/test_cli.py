from typer.testing import CliRunner

from steward.cli.main import app

runner = CliRunner()


def test_cli_agent_machine_and_note_flow():
    created = runner.invoke(
        app,
        [
            "agents", "create",
            "--name", "House",
            "--model", "llama3.1",
            "--persona", "Keeps the house machines tidy.",
            "--approval", "ask",
        ],
    )
    assert created.exit_code == 0, created.stdout
    listed = runner.invoke(app, ["agents", "list"])
    assert listed.exit_code == 0
    assert "House" in listed.stdout

    added = runner.invoke(
        app,
        ["machines", "add", "host", "--host", "host.example", "--user", "user", "--sudo-notes", "password required"],
    )
    assert added.exit_code == 0, added.stdout
    machines = runner.invoke(app, ["machines", "list"])
    assert "host" in machines.stdout
    assert "host.example" in machines.stdout
    here = runner.invoke(app, ["machines", "add", "box", "--local"])
    assert here.exit_code == 0, here.stdout
    listed = runner.invoke(app, ["machines", "list"])
    assert "this computer" in listed.stdout
    second = runner.invoke(app, ["machines", "add", "other", "--local"])
    assert second.exit_code == 1
    assert "already saved" in second.stdout

    note = runner.invoke(
        app,
        ["notes", "add", "--agent", "House", "--title", "Coffee", "--content", "Black, in the morning."],
    )
    assert note.exit_code == 0, note.stdout
    notes = runner.invoke(app, ["notes", "list", "--agent", "House"])
    assert "Black, in the morning." in notes.stdout

    missing = runner.invoke(app, ["ask", "--agent", "Nobody", "hello"])
    assert missing.exit_code == 1
    assert "No agent" in missing.stdout


def test_fresh_cli_invites_the_first_agent_and_this_computer():
    bare = runner.invoke(app, [])
    assert bare.exit_code == 0, bare.stdout
    flat = " ".join(bare.stdout.split())
    assert "Create your first agent with: steward agents create --name <name> --model <model>" in flat
    assert "steward machines add <name> --local" in flat
    agents = runner.invoke(app, ["agents", "list"])
    assert agents.exit_code == 0
    assert "No agents yet" in agents.stdout
    machines = runner.invoke(app, ["machines", "list"])
    assert machines.exit_code == 0
    assert "steward machines add <name> --local" in " ".join(machines.stdout.split())
    assert "box" not in machines.stdout


def test_cli_creates_database_backup(tmp_path):
    destination = tmp_path / "steward-export.db"
    created = runner.invoke(app, ["backup", "--output", str(destination)])
    assert created.exit_code == 0, created.stdout
    assert destination.exists()
    duplicate = runner.invoke(app, ["backup", "--output", str(destination)])
    assert duplicate.exit_code == 1
    replaced = runner.invoke(app, ["backup", "--output", str(destination), "--overwrite"])
    assert replaced.exit_code == 0, replaced.stdout
