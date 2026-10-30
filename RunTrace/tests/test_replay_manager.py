import json
import sys
import zipfile

import pytest

from runtrace.command_builder import RunConfig
from runtrace.process_runner import ProcessRunner
from runtrace.replay_manager import (
    ReplayConfirmationRequired,
    ReplayError,
    ReplayManager,
)
from runtrace.run_store import RunStore


def _make_run(store: RunStore, tmp_path) -> str:
    runner = ProcessRunner(store)
    outcome = runner.run(
        RunConfig(
            name="可复现任务",
            cwd=str(tmp_path),
            command=sys.executable,
            args=["-c", "print('replay-output'); print('replay-err', file=__import__('sys').stderr)"],
            shell=False,
            timeout=10,
        )
    )
    assert outcome.status == "success"
    return outcome.run_id


def test_build_and_inspect_replay_package(store, tmp_path):
    run_id = _make_run(store, tmp_path)
    package = tmp_path / "replay.zip"
    manager = ReplayManager(store)
    info = manager.build_package(run_id, package)
    assert package.exists()
    assert info.run_id == run_id
    assert info.warnings
    assert any("脱敏" in gap for gap in info.environment_gaps)
    assert any(dep["name"] == sys.executable or dep["name"] == "python" for dep in info.external_dependencies)
    # 生成包不会自动执行，不会新增 run。
    assert len(store.list_runs()) == 1

    inspected = manager.inspect_package(package)
    assert inspected["checksums_valid"] is True
    manifest = inspected["manifest"]
    assert manifest["schema"] == "runtrace.replay.v1"
    assert manifest["confirmation_required"] is True
    assert manifest["run_id"] == run_id
    assert manifest["config"]["command"] == sys.executable
    assert manifest["checksums"]["stdout.log"]
    with zipfile.ZipFile(package) as zf:
        assert set(zf.namelist()) == {
            "manifest.json",
            "run.json",
            "config.json",
            "stdout.log",
            "stderr.log",
            "checksums.json",
            "REPRODUCE.md",
        }
        assert "replay-output" in zf.read("stdout.log").decode("utf-8")
        assert "确认" in zf.read("REPRODUCE.md").decode("utf-8")


def test_execute_requires_explicit_confirmation(store, tmp_path):
    run_id = _make_run(store, tmp_path)
    package = tmp_path / "replay.zip"
    manager = ReplayManager(store)
    manager.build_package(run_id, package)
    with pytest.raises(ReplayConfirmationRequired):
        manager.execute_package(package)
    assert len(store.list_runs()) == 1


def test_confirmed_execute_creates_new_run(store, tmp_path):
    run_id = _make_run(store, tmp_path)
    package = tmp_path / "replay.zip"
    manager = ReplayManager(store)
    manager.build_package(run_id, package)
    outcome = manager.execute_package(package, confirmed=True)
    assert outcome.status == "success"
    assert outcome.exit_code == 0
    assert outcome.run_id != run_id
    assert len(store.list_runs()) == 2
    new_run = store.get_run(outcome.run_id)
    assert new_run["name"].startswith("复现")
    assert "replay-output" in store.get_logs(outcome.run_id, "stdout")


def test_tampered_package_is_rejected(store, tmp_path):
    run_id = _make_run(store, tmp_path)
    package = tmp_path / "replay.zip"
    manager = ReplayManager(store)
    manager.build_package(run_id, package)
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(package) as source, zipfile.ZipFile(tampered, "w") as target:
        for name in source.namelist():
            data = source.read(name)
            if name == "stdout.log":
                data = b"tampered log"
            target.writestr(name, data)
    with pytest.raises(ReplayError, match="校验失败"):
        manager.inspect_package(tampered)


def test_invalid_or_missing_package_is_rejected(store, tmp_path):
    manager = ReplayManager(store)
    with pytest.raises(ReplayError):
        manager.inspect_package(tmp_path / "missing.zip")
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    with pytest.raises(ReplayError):
        manager.inspect_package(bad)
    incomplete = tmp_path / "incomplete.zip"
    with zipfile.ZipFile(incomplete, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"schema": "runtrace.replay.v1"}))
    with pytest.raises(ReplayError, match="缺少"):
        manager.inspect_package(incomplete)
