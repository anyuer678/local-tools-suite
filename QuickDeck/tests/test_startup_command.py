from __future__ import annotations

import sys

from quickdeck.launcher_ui import _startup_command


def test_frozen_cli_prefers_gui_executable(tmp_path, monkeypatch):
    cli_dir = tmp_path / "cli"
    cli_dir.mkdir()
    (cli_dir / "QuickDeckCLI.exe").write_text("", encoding="utf-8")
    (tmp_path / "QuickDeck.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(cli_dir / "QuickDeckCLI.exe"))

    command = _startup_command()
    assert "QuickDeck.exe" in command
    assert "QuickDeckCLI" not in command
    assert command.endswith("--minimized")


def test_frozen_cli_portable_layout(tmp_path, monkeypatch):
    base = tmp_path / "dist"
    gui_dir = base / "QuickDeck"
    cli_dir = base / "QuickDeckCLI"
    gui_dir.mkdir(parents=True)
    cli_dir.mkdir(parents=True)
    (gui_dir / "QuickDeck.exe").write_text("", encoding="utf-8")
    (cli_dir / "QuickDeckCLI.exe").write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(cli_dir / "QuickDeckCLI.exe"))

    command = _startup_command()
    assert str(gui_dir / "QuickDeck.exe") in command


def test_source_mode_uses_module_entry(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    command = _startup_command()
    assert "-m quickdeck --minimized" in command
