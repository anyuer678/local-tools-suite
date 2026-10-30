"""安全执行器测试：未确认绝不修改、隔离可回滚、移动、删除保护、令牌校验。"""

from __future__ import annotations

from pathlib import Path

import pytest

from spacepilot.executor import (
    ConfirmationRequired,
    Executor,
    UnsafeOperation,
    build_delete_plan,
    build_move_plan,
    build_plan,
    build_quarantine_plan,
    load_plan,
    save_plan,
)
from spacepilot.models import Operation


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


@pytest.fixture()
def file_tree(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    write_bytes(root / "data" / "payload.bin", b"payload-0123456789")
    write_bytes(root / "other.txt", b"other")
    return root


def test_quarantine_requires_confirm_and_token(file_tree: Path, tmp_path: Path) -> None:
    source = file_tree / "data" / "payload.bin"
    qroot = tmp_path / "quarantine"
    log = tmp_path / "logs" / "executions.jsonl"
    plan = build_quarantine_plan([source], quarantine_root=qroot, root=file_tree)
    executor = Executor(quarantine_root=qroot, log_path=log)

    with pytest.raises(ConfirmationRequired):
        executor.execute(plan, confirm=False)
    assert source.exists()
    assert not qroot.exists()
    assert not log.exists()

    with pytest.raises(ConfirmationRequired):
        executor.execute(plan, confirm=True, token="wrong-token")
    assert source.exists()
    assert not log.exists()


def test_quarantine_copy_verify_and_rollback(file_tree: Path, tmp_path: Path) -> None:
    source = file_tree / "data" / "payload.bin"
    original_hash = source.read_bytes()
    qroot = tmp_path / "quarantine"
    log = tmp_path / "logs" / "executions.jsonl"
    plan = build_quarantine_plan(
        [source],
        quarantine_root=qroot,
        root=file_tree,
        reasons={str(source): "测试隔离"},
    )
    assert plan.token in "".join(plan.preview_lines())
    plan_file = tmp_path / "plan.json"
    save_plan(plan, plan_file)
    loaded = load_plan(plan_file)
    assert loaded.token == plan.token

    executor = Executor(quarantine_root=qroot, log_path=log)
    result = executor.execute(loaded, confirm=True, token=loaded.token)
    assert result.succeeded == 1
    assert result.failed == 0
    assert source.exists()
    assert source.read_bytes() == original_hash

    target = Path(result.results[0].target)
    assert target.exists()
    assert target.read_bytes() == original_hash
    assert result.results[0].verified is True
    assert result.results[0].sha256

    records = executor.read_log()
    assert any(record.get("type") == "operation" for record in records)
    assert any(record.get("type") == "execution_complete" for record in records)

    rollback = executor.rollback(result.execution_id)
    assert rollback.restored == 1
    assert rollback.failed == 0
    assert source.exists()
    assert not target.exists()


def test_move_plan_and_rollback_not_promised(file_tree: Path, tmp_path: Path) -> None:
    source = file_tree / "other.txt"
    destination = tmp_path / "dest" / "moved.txt"
    plan = build_move_plan([(str(source), str(destination))])
    log = tmp_path / "logs" / "move.jsonl"
    executor = Executor(quarantine_root=tmp_path / "q", log_path=log)
    result = executor.execute(plan, confirm=True, token=plan.token)
    assert result.succeeded == 1
    assert not source.exists()
    assert destination.exists()
    assert destination.read_bytes() == b"other"

    rollback = executor.rollback(result.execution_id)
    assert rollback.restored == 0
    assert rollback.skipped == 1
    assert destination.exists()  # 移动不承诺回滚，目标仍存在


def test_delete_requires_explicit_allow(file_tree: Path, tmp_path: Path) -> None:
    source = file_tree / "other.txt"
    plan = build_delete_plan([source], reason="测试删除")
    log = tmp_path / "logs" / "delete.jsonl"
    executor = Executor(log_path=log, allow_delete=False)

    with pytest.raises(UnsafeOperation):
        executor.execute(plan, confirm=True, token=plan.token)
    assert source.exists()

    executor = Executor(log_path=log, allow_delete=True)
    result = executor.execute(plan, confirm=True, token=plan.token)
    assert result.succeeded == 1
    assert not source.exists()


def test_delete_refuses_high_risk_unknown(file_tree: Path, tmp_path: Path) -> None:
    source = file_tree / "other.txt"
    operation = Operation(
        op_id="risk-1",
        kind="delete",
        source=str(source),
        expected_size=source.stat().st_size,
        reason="未知文件",
        risk="high",
        irreversible=True,
    )
    plan = build_plan([operation])
    executor = Executor(allow_delete=True, log_path=tmp_path / "logs" / "risk.jsonl")
    with pytest.raises(UnsafeOperation):
        executor.execute(plan, confirm=True, token=plan.token)
    assert source.exists()


def test_delete_refuses_critical_path(file_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = file_tree / "other.txt"
    plan = build_delete_plan([source], reason="测试关键路径")
    executor = Executor(allow_delete=True, log_path=tmp_path / "logs" / "critical.jsonl")
    monkeypatch.setattr("spacepilot.executor.is_critical_path", lambda path: True)
    with pytest.raises(UnsafeOperation):
        executor.execute(plan, confirm=True, token=plan.token)
    assert source.exists()


def test_plan_detects_file_change_after_preview(file_tree: Path, tmp_path: Path) -> None:
    source = file_tree / "other.txt"
    plan = build_quarantine_plan([source], quarantine_root=tmp_path / "q", root=file_tree)
    source.write_bytes(b"changed-after-plan")
    executor = Executor(quarantine_root=tmp_path / "q", log_path=tmp_path / "logs" / "changed.jsonl")
    with pytest.raises(UnsafeOperation):
        executor.execute(plan, confirm=True, token=plan.token)
    assert source.exists()
    assert not (tmp_path / "logs" / "changed.jsonl").exists()


def test_quarantine_target_must_stay_inside_root(file_tree: Path, tmp_path: Path) -> None:
    source = file_tree / "other.txt"
    operation = Operation(
        op_id="outside-1",
        kind="quarantine",
        source=str(source),
        target=str(tmp_path / "outside" / "other.txt"),
        expected_size=source.stat().st_size,
        reason="越界目标",
        risk="medium",
    )
    plan = build_plan([operation])
    executor = Executor(quarantine_root=tmp_path / "q", log_path=tmp_path / "logs" / "outside.jsonl")
    with pytest.raises(UnsafeOperation):
        executor.execute(plan, confirm=True, token=plan.token)
    assert source.exists()
    assert not (tmp_path / "outside" / "other.txt").exists()


def test_null_confirm_never_modifies_even_with_valid_token(file_tree: Path, tmp_path: Path) -> None:
    source = file_tree / "other.txt"
    plan = build_delete_plan([source], reason="不应执行", risk="low")
    executor = Executor(allow_delete=True, log_path=tmp_path / "logs" / "noconfirm.jsonl")
    with pytest.raises(ConfirmationRequired):
        executor.execute(plan, confirm=False, token=plan.token)
    assert source.exists()
