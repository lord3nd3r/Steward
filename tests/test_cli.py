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
        ["machines", "add", "SER", "--host", "ser.local", "--user", "ada", "--sudo-notes", "password required"],
    )
    assert added.exit_code == 0, added.stdout
    machines = runner.invoke(app, ["machines", "list"])
    assert "SER" in machines.stdout
    assert "local" in machines.stdout

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
