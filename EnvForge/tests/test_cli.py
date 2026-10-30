from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from envforge.cli import EXIT_CONFIRM, EXIT_ERROR, EXIT_OK, EXIT_USAGE, main


@pytest.fixture
def cli(tmp_path):
    db_path = tmp_path / "cli.db"

    def run(*args: str) -> int:
        return main(["--db", str(db_path), *args])

    return run


def test_runtime_list_empty_json(cli, capsys):
    assert cli("runtime", "list", "--json") == EXIT_OK
    assert json.loads(capsys.readouterr().out) == []


def test_runtime_add_and_list(cli, capsys):
    assert cli("runtime", "add", "--kind", "python", "--path", sys.executable, "--version", "3.12.0") == EXIT_OK
    out = capsys.readouterr().out
    assert "已登记" in out
    assert cli("runtime", "list") == EXIT_OK
    assert "python" in capsys.readouterr().out


def test_runtime_add_missing_path_usage_error(cli, capsys):
    code = cli("runtime", "add", "--kind", "python", "--path", r"C:\nope\python.exe")
    assert code == EXIT_USAGE
    assert "输入错误" in capsys.readouterr().err


def test_runtime_remove_requires_confirmation(cli):
    assert cli("runtime", "add", "--kind", "python", "--path", sys.executable) == EXIT_OK
    assert cli("runtime", "remove", "1") == EXIT_CONFIRM
    assert cli("runtime", "remove", "1", "--yes") == EXIT_OK


def test_runtime_remove_missing_record(cli, capsys):
    assert cli("runtime", "remove", "999", "--yes") == EXIT_ERROR
    assert "未找到" in capsys.readouterr().err


def test_env_db_check_and_backup(cli, capsys):
    assert cli("env", "db-check") == EXIT_OK
    assert "通过" in capsys.readouterr().out
    assert cli("env", "db-backup") == EXIT_OK
    assert "已备份" in capsys.readouterr().out


def test_audit_list_shows_actions_without_values(cli, tmp_path, capsys):
    project = tmp_path / "audit-proj"
    project.mkdir()
    assert cli("project", "add", "--name", "demo-audit", "--path", str(project)) == EXIT_OK
    assert cli("profile", "set", "demo-audit", "dev", "DB_PASSWORD=topsecret") == EXIT_OK
    capsys.readouterr()
    assert cli("audit", "list") == EXIT_OK
    out = capsys.readouterr().out
    assert "设置档案变量" in out
    assert "DB_PASSWORD" in out
    assert "topsecret" not in out


def test_project_and_profile_flow(cli, tmp_path, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    assert cli("project", "add", "--name", "demo", "--path", str(project)) == EXIT_OK
    assert cli("profile", "set", "demo", "dev", "API_URL=http://localhost") == EXIT_OK
    assert cli("profile", "set", "demo", "dev", "DB_PASSWORD=hunter2") == EXIT_OK
    assert cli("profile", "show", "demo", "dev") == EXIT_OK
    out = capsys.readouterr().out
    assert "hunter2" not in out
    assert "DB_PASSWORD=***" in out

    export = tmp_path / "dev.env"
    assert cli("profile", "export", "demo", "dev", "--output", str(export)) == EXIT_OK
    text = export.read_text(encoding="utf-8")
    assert "DB_PASSWORD=***" in text and "hunter2" not in text


def test_profile_set_invalid_pair(cli, capsys):
    assert cli("profile", "set", "demo", "dev", "NO_EQUALS") == EXIT_USAGE
    assert "输入错误" in capsys.readouterr().err


def test_profile_activate_requires_yes(cli, tmp_path, capsys):
    project = tmp_path / "p"
    project.mkdir()
    cli("project", "add", "--name", "d", "--path", str(project))
    cli("profile", "set", "d", "dev", "A=1")
    assert cli("profile", "activate", "d", "dev") == EXIT_CONFIRM
    assert "需要确认" in capsys.readouterr().err
    assert cli("profile", "activate", "d", "dev", "--yes") == EXIT_OK
    out = capsys.readouterr().out
    assert "$env:A = '1'" in out


def test_profile_unset_requires_yes(cli, tmp_path, capsys):
    project = tmp_path / "p2"
    project.mkdir()
    cli("project", "add", "--name", "d2", "--path", str(project))
    cli("profile", "set", "d2", "dev", "A=1")
    assert cli("profile", "unset", "d2", "dev", "A") == EXIT_CONFIRM
    assert cli("profile", "unset", "d2", "dev", "A", "--yes") == EXIT_OK
    assert cli("profile", "unset", "d2", "dev", "A", "--yes") == EXIT_ERROR


def test_profile_diff_and_project_remove(cli, tmp_path, capsys):
    project = tmp_path / "p3"
    project.mkdir()
    cli("project", "add", "--name", "d3", "--path", str(project))
    cli("profile", "set", "d3", "dev", "A=1")
    cli("profile", "set", "d3", "prod", "A=2")
    assert cli("profile", "diff", "d3", "dev", "prod") == EXIT_OK
    assert "A: 1 -> 2" in capsys.readouterr().out
    assert cli("project", "remove", "d3") == EXIT_CONFIRM
    assert cli("project", "remove", "d3", "--yes") == EXIT_OK
    assert cli("project", "remove", "d3", "--yes") == EXIT_ERROR


def test_env_snapshot_and_show_session(cli, capsys):
    assert cli("env", "snapshot", "--scope", "session", "--label", "base") == EXIT_OK
    assert "已创建快照" in capsys.readouterr().out
    assert cli("env", "show", "--scope", "session", "--json") == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["var_count"] > 0
    assert cli("env", "snapshots", "--json") == EXIT_OK
    items = json.loads(capsys.readouterr().out)
    assert items and items[0]["label"] == "base"


def test_env_path_and_diff(cli, capsys):
    assert cli("env", "path", "--scope", "session", "--json") == EXIT_OK
    report = json.loads(capsys.readouterr().out)
    assert isinstance(report, list) and report[0]["status"] in {"ok", "missing", "duplicate", "empty"}
    cli("env", "snapshot", "--scope", "session")
    capsys.readouterr()
    cli("env", "snapshot", "--scope", "session")
    capsys.readouterr()
    assert cli("env", "diff", "1", "2", "--json") == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["added"] == {} and data["removed"] == {}


def test_env_apply_requires_yes(cli, capsys):
    assert cli("env", "apply", "--set", "X=1") == EXIT_CONFIRM
    assert "需要确认" in capsys.readouterr().err
    assert cli("env", "apply", "--set", "BAD") == EXIT_USAGE


def test_env_restore_missing_snapshot(cli, capsys):
    assert cli("env", "restore", "999", "--yes") == EXIT_ERROR
    assert "未找到" in capsys.readouterr().err


def test_env_dedupe_preview(cli, capsys):
    assert cli("env", "dedupe") in (EXIT_OK,)
    assert "可移除" in capsys.readouterr().out


def test_doctor_json_and_fail_on(cli, capsys):
    code = cli("doctor", "--scope", "session", "--json")
    assert code == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"findings", "summary"}
    assert set(data["summary"]) == {"error", "warn", "info"}


def test_json_flag_position_independent(cli, capsys):
    assert cli("runtime", "list", "--json") == EXIT_OK
    json.loads(capsys.readouterr().out)


def test_unknown_subcommand_usage(cli, capsys):
    with pytest.raises(SystemExit) as exc:
        cli("nope")
    assert exc.value.code == 2


def test_gui_subcommand_fallback(monkeypatch, cli):
    import envforge.cli as cli_module

    monkeypatch.setattr(cli_module, "_launch_gui", lambda: 0)
    assert cli("gui") == 0


def test_library_list_json_has_full_catalog(cli, capsys):
    assert cli("library", "list", "--json") == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["catalog_total"] >= 100
    assert data["total"] == data["catalog_total"]


def test_library_category_filter_and_show(cli, capsys):
    assert cli("library", "list", "--category", "代理与网络", "--json") == EXIT_OK
    data = json.loads(capsys.readouterr().out)
    assert data["items"]
    assert all(item["category"] == "代理与网络" for item in data["items"])

    assert cli("library", "show", "JAVA_HOME", "--json") == EXIT_OK
    preset = json.loads(capsys.readouterr().out)
    assert preset["key"] == "JAVA_HOME"
    assert preset["sensitive"] is False


def test_library_show_unknown_returns_error(cli, capsys):
    assert cli("library", "show", "NOPE_KEY") == EXIT_ERROR
    assert "未找到" in capsys.readouterr().err


def test_library_categories_output(cli, capsys):
    assert cli("library", "categories") == EXIT_OK
    out = capsys.readouterr().out
    assert "内置常用环境变量" in out
    assert "代理与网络" in out
