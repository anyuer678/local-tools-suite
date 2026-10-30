from pathlib import Path

from quickdeck.plugin_host import build_runner_command


def test_frozen_uses_own_executable_with_runner_flag():
    command = build_runner_command(Path("runner.py"), Path("plug"), frozen=True)
    assert len(command) == 3
    assert command[1] == "--plugin-runner"
    assert command[2] == "plug"


def test_source_uses_isolated_python_runner():
    command = build_runner_command("runner.py", "plug", frozen=False)
    assert command[1] == "-I"
    assert command[2] == "runner.py"
    assert command[3] == "plug"
