import json
import zipfile
from pathlib import Path

from runtrace.run_store import RunStore, recover_interrupted_runs


def _seed(store: RunStore, *, name="demo", started_at=None, status="running"):
    rid = store.create_run(
        name=name,
        command="python",
        args=["-c", "print('x')"],
        cwd="C:/work/project",
        env={"PATH": "C:/bin", "MY_SECRET": "super-secret-value", "NORMAL": "ok"},
        env_overrides={"API_TOKEN": "override-secret"},
        shell=False,
        timeout=10,
        status=status,
        started_at=started_at,
        project_path="C:/work/project",
    )
    return rid


def test_create_finish_and_fetch(store):
    rid = _seed(store)
    assert store.get_run(rid)["status"] == "running"
    assert store.finish_run(rid, exit_code=0, status="success") is True
    run = store.get_run(rid)
    assert run["status"] == "success"
    assert run["exit_code"] == 0
    assert run["duration_ms"] is not None
    assert run["args"] == ["-c", "print('x')"]
    assert run["env"]["MY_SECRET"] == "***"
    assert run["env_overrides"]["API_TOKEN"] == "***"
    assert "super-secret-value" not in json.dumps(run, ensure_ascii=False)
    assert run["metadata"]["redacted_env_keys"]


def test_unique_ids_and_transactional_logs(store):
    first = _seed(store, name="a")
    second = _seed(store, name="b")
    assert first != second
    store.append_logs(first, "stdout", "alpha")
    store.append_logs(first, "stderr", "err-1")
    store.append_logs(first, "stdout", "beta")
    assert store.get_logs(first, "stdout") == "alphabeta"
    assert store.get_logs(first, "stderr") == "err-1"
    chunks = store.get_log_chunks(first)
    assert len(chunks) == 3
    assert chunks[0]["content"] == "alpha"


def test_search_filters_and_time_range(store):
    old = _seed(store, name="旧构建", started_at="2025-01-01T00:00:00+00:00")
    new = _seed(store, name="新测试", started_at="2026-05-01T12:00:00+00:00")
    store.finish_run(old, exit_code=0, status="success")
    store.finish_run(new, exit_code=3, status="failed")
    store.append_logs(new, "stdout", "unique-log-marker-中文")
    store.append_logs(new, "stderr", "Traceback failure")

    assert [run["id"] for run in store.list_runs(search="unique-log-marker")] == [new]
    assert [run["id"] for run in store.list_runs(search="旧构建")] == [old]
    assert [run["id"] for run in store.list_runs(status="failed")] == [new]
    assert [run["id"] for run in store.list_runs(status=["success", "failed"])] == [new, old]
    assert [run["id"] for run in store.list_runs(project="C:/work/project")] == [new, old]
    assert [run["id"] for run in store.list_runs(since="2026-01-01")] == [new]
    assert [run["id"] for run in store.list_runs(until="2025-12-31")] == [old]
    assert store.list_runs(limit=1) == [store.get_run(new)]


def test_export_json_text_and_zip_do_not_leak_environment_secret(store, tmp_path):
    rid = _seed(store, name="导出")
    store.append_logs(rid, "stdout", "hello 中文\n")
    store.append_logs(rid, "stderr", "warn\n")
    store.finish_run(rid, exit_code=0, status="success")
    json_path = tmp_path / "export.json"
    result = store.export_run_json(rid, json_path)
    text = json_path.read_text(encoding="utf-8")
    payload = json.loads(text)
    assert payload["schema"] == "runtrace.run.v1"
    assert payload["logs"]["stdout"] == "hello 中文\n"
    assert payload["integrity"]["stdout_sha256"]
    assert "super-secret-value" not in text
    assert "override-secret" not in text
    assert payload["security"]["environment_redacted"] is True
    assert Path(result["path"]).exists()

    text_path = tmp_path / "export.txt"
    store.export_run_text(rid, text_path)
    exported_text = text_path.read_text(encoding="utf-8")
    assert "STDOUT:" in exported_text
    assert "hello 中文" in exported_text
    assert "super-secret-value" not in exported_text

    zip_path = tmp_path / "export.zip"
    store.export_run_archive(rid, zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        assert {"run.json", "stdout.log", "stderr.log", "checksums.json"} <= names
        assert "super-secret-value" not in zf.read("run.json").decode("utf-8")
        checksums = json.loads(zf.read("checksums.json"))
        assert checksums["stdout.log"]


def test_recovery_marks_orphan_running_as_interrupted(store):
    rid = _seed(store, name="遗留")
    count = recover_interrupted_runs(store)
    assert count == 1
    run = store.get_run(rid)
    assert run["status"] == "interrupted"
    assert run["ended_at"]
    # 已结束的记录不会再次被恢复。
    assert store.recover_interrupted_runs() == 0


def test_templates_save_copy_and_redact(store, tmp_path):
    store.save_template(
        name="构建",
        cwd=str(tmp_path),
        command="python",
        args=["-m", "pytest"],
        env_overrides={"MY_TOKEN": "secret-value", "MODE": "ci"},
        timeout=30,
        shell=False,
    )
    tpl = store.get_template("构建")
    assert tpl is not None
    assert tpl["env_overrides"]["MY_TOKEN"] == "***"
    assert tpl["env_overrides"]["MODE"] == "ci"
    config = store.config_from_template("构建")
    assert "MY_TOKEN" not in config.env_overrides
    assert config.env_overrides["MODE"] == "ci"
    assert config.args == ["-m", "pytest"]

    new_id = store.copy_template("构建", "构建副本")
    assert new_id > 0
    assert store.get_template("构建副本")["command"] == "python"
    assert {item["name"] for item in store.list_templates()} == {"构建副本", "构建"}
    assert store.delete_template("构建副本") is True
    assert store.get_template("构建副本") is None
