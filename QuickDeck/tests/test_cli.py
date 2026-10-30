from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import quickdeck.cli as cli

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _cli(tmp_path: Path, *args: str, timeout: float = 20) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "quickdeck",
            "--data-dir",
            str(tmp_path),
            *args,
        ],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def test_help_runs() -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, "-m", "quickdeck", "--help"],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    assert result.returncode == 0
    assert "QuickDeck" in result.stdout
    assert "plugins" in result.stdout
    assert "actions" in result.stdout


def test_cli_forced_mode_without_gui(tmp_path: Path) -> None:
    result = _cli(tmp_path, "--cli")
    assert result.returncode == 0
    assert "CLI 模式" in result.stdout
    assert "search" in result.stdout


def test_fallback_when_pyside_unavailable(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "qt_available", lambda: False)
    code = cli.main([])
    output = capsys.readouterr().out
    assert code == 0
    assert "CLI 模式" in output
    assert "usage:" in output


def test_cli_search_and_calc(tmp_path: Path) -> None:
    search = _cli(tmp_path, "search", "calc")
    assert search.returncode == 0
    assert "calc.eval" in search.stdout

    search_json = _cli(tmp_path, "search", "calc", "--json")
    data = json.loads(search_json.stdout)
    assert any(item["id"] == "calc.eval" for item in data)

    calc = _cli(tmp_path, "calc", "2 ** 8")
    assert calc.returncode == 0
    assert calc.stdout.strip() == "256"


def test_cli_run_builtin_and_file_input(tmp_path: Path) -> None:
    result = _cli(tmp_path, "run", "encoding.base64_encode", "-i", "hello")
    assert result.returncode == 0
    assert result.stdout.strip() == "aGVsbG8="

    source = tmp_path / "input.txt"
    source.write_text("a\nb\na", encoding="utf-8")
    result = _cli(tmp_path, "run", "text.dedupe", "--file", str(source))
    assert result.stdout.strip() == "a\nb"


def test_cli_unit_and_time(tmp_path: Path) -> None:
    result = _cli(tmp_path, "unit", "1", "GB", "MB")
    assert result.returncode == 0
    assert result.stdout.strip() == "1024 MB"

    result = _cli(tmp_path, "time", "record", "阅读")
    assert result.returncode == 0
    assert "已记录" in result.stdout
    result = _cli(tmp_path, "time", "history", "--json")
    data = json.loads(result.stdout)
    assert data[0]["name"] == "阅读"


def test_cli_plugins_consent_and_execution(tmp_path: Path) -> None:
    no_consent = _cli(tmp_path, "plugins", "enable", "example")
    assert no_consent.returncode == 2
    assert "--consent" in no_consent.stderr

    enabled = _cli(tmp_path, "plugins", "enable", "example", "--consent")
    assert enabled.returncode == 0
    assert "plugin:example:char_count" in enabled.stdout

    listed = _cli(tmp_path, "plugins", "list", "--json")
    data = json.loads(listed.stdout)
    assert any(item["id"] == "example" and item["enabled"] for item in data)

    executed = _cli(tmp_path, "run", "plugin:example:reverse", "-i", "abc")
    assert executed.returncode == 0
    assert executed.stdout.strip() == "cba"

    disabled = _cli(tmp_path, "plugins", "disable", "example")
    assert disabled.returncode == 0
    after = _cli(tmp_path, "run", "plugin:example:reverse", "-i", "abc")
    assert after.returncode == 1
    assert "不存在" in after.stderr or "禁用" in after.stderr


def test_cli_external_command_requires_yes(tmp_path: Path) -> None:
    marker = tmp_path / "executed.txt"
    code = f"open({str(marker)!r}, 'w', encoding='utf-8').write('ran')"
    common = [
        "exec",
        "--program",
        sys.executable,
        "--arg=-c",
        "--arg=" + code,
    ]
    blocked = _cli(tmp_path, *common)
    assert blocked.returncode == 2
    assert marker.exists() is False
    assert "--yes" in blocked.stderr

    allowed = _cli(tmp_path, *common, "--yes")
    assert allowed.returncode == 0
    assert marker.read_text(encoding="utf-8") == "ran"


def test_cli_actions_add_and_run(tmp_path: Path) -> None:
    action = {
        "id": "upper",
        "name": "转大写",
        "aliases": ["upper"],
        "steps": [
            {"operation": "text.trim"},
            {"operation": "text.upper"},
        ],
        "output_method": "display",
    }
    source = tmp_path / "action.json"
    source.write_text(json.dumps(action, ensure_ascii=False), encoding="utf-8")
    added = _cli(tmp_path, "actions", "add", "--file", str(source))
    assert added.returncode == 0
    listed = _cli(tmp_path, "actions", "list", "--json")
    assert any(item["id"] == "upper" for item in json.loads(listed.stdout))
    executed = _cli(tmp_path, "run", "action.upper", "-i", "  abc  ")
    assert executed.returncode == 0
    assert executed.stdout.strip() == "ABC"
    removed = _cli(tmp_path, "actions", "delete", "upper")
    assert removed.returncode == 0


def test_cli_settings_and_diagnose(tmp_path: Path) -> None:
    exported = _cli(tmp_path, "settings", "export", str(tmp_path / "settings-backup.json"))
    assert exported.returncode == 0
    assert (tmp_path / "settings-backup.json").exists()

    diagnosed = _cli(tmp_path, "diagnose")
    assert diagnosed.returncode == 0
    data = json.loads(diagnosed.stdout)
    assert "python" in data
    assert data["data_dir"] == str(tmp_path.resolve())

    clipboard = _cli(tmp_path, "clipboard", "status")
    assert clipboard.returncode == 0
    status = json.loads(clipboard.stdout)
    assert status["enabled"] is False


def test_gui_minimized_flag_is_forwarded(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_launch(context, *, start_minimized: bool = False) -> int:
        captured["start_minimized"] = start_minimized
        captured["data_dir"] = str(context.data_dir)
        return 0

    monkeypatch.setattr(cli, "launch_gui", fake_launch)
    assert cli.main(["--data-dir", str(tmp_path), "gui", "--minimized"]) == 0
    assert captured == {
        "start_minimized": True,
        "data_dir": str(tmp_path.resolve()),
    }
