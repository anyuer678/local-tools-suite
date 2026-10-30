import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"


def _env(extra_path: Path | None = None) -> dict[str, str]:
    env = os.environ.copy()
    parts = [str(SRC)] + ([str(extra_path)] if extra_path else [])
    existing = env.get("PYTHONPATH")
    if existing:
        parts.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    env.pop("RUNTRACE_DATA_DIR", None)
    return env


def _run_cli(*args: str, extra_path: Path | None = None, timeout: float = 30):
    return subprocess.run(
        [sys.executable, "-m", "runtrace", *args],
        cwd=PROJECT_ROOT,
        env=_env(extra_path),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def test_cli_help_does_not_import_gui():
    result = _run_cli("--help")
    assert result.returncode == 0
    assert "RunTrace" in result.stdout
    assert "run" in result.stdout
    assert "replay" in result.stdout
    assert "PySide6" not in result.stderr


def test_cli_run_list_show_export_smoke(tmp_path):
    db = tmp_path / "cli.db"
    result = _run_cli(
        "--db",
        str(db),
        "run",
        "--name",
        "cli-smoke",
        "--cwd",
        str(tmp_path),
        "--timeout",
        "10",
        "--stream",
        "--",
        sys.executable,
        "-c",
        "print('cli-output-中文')",
    )
    assert result.returncode == 0, result.stderr
    assert "cli-output-中文" in result.stdout
    assert "状态: 成功" in result.stdout

    listed = _run_cli("--db", str(db), "list", "--json")
    assert listed.returncode == 0, listed.stderr
    runs = json.loads(listed.stdout)
    assert len(runs) == 1
    assert runs[0]["name"] == "cli-smoke"
    run_id = runs[0]["id"]

    shown = _run_cli("--db", str(db), "show", run_id)
    assert shown.returncode == 0
    assert "cli-output-中文" in shown.stdout

    export_path = tmp_path / "cli-export.json"
    exported = _run_cli(
        "--db", str(db), "export", run_id, "--format", "json", "--output", str(export_path)
    )
    assert exported.returncode == 0, exported.stderr
    payload = json.loads(export_path.read_text(encoding="utf-8"))
    assert payload["run"]["name"] == "cli-smoke"
    assert payload["logs"]["stdout"] == "cli-output-中文\r\n" or "cli-output-中文" in payload["logs"]["stdout"]


def test_gui_missing_falls_back_to_cli(tmp_path):
    fake = tmp_path / "fake_qt"
    fake_package = fake / "PySide6"
    fake_package.mkdir(parents=True)
    (fake_package / "__init__.py").write_text(
        'raise ImportError("simulated missing PySide6")\n', encoding="utf-8"
    )
    result = _run_cli(extra_path=fake)
    assert result.returncode == 0
    assert "回退到 CLI" in result.stderr
    assert "usage: runtrace" in result.stdout.lower()


def test_qt_app_source_compiles_without_importing_pyside6():
    source = (SRC / "runtrace" / "ui" / "qt_app.py").read_text(encoding="utf-8")
    compile(source, str(SRC / "runtrace" / "ui" / "qt_app.py"), "exec")
