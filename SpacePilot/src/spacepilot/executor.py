"""安全执行器：预览 -> 明确确认 -> 执行 -> 日志 -> 复核 -> 隔离回滚。

默认能力：
- 复制到隔离目录（quarantine，原文件保持不变，可回滚隔离副本）
- 用户明确指定的移动（不可通过本工具回滚）
- 删除（必须显式 allow_delete，且风险非 HIGH、非系统关键路径）
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from .duplicates import file_sha256
from .models import (
    ExecutionResult,
    Operation,
    OperationKind,
    OperationResult,
    Plan,
    RiskLevel,
    RollbackResult,
)
from .policy import default_log_path, default_quarantine_dir, is_critical_path


class ExecutorError(RuntimeError):
    """执行器的基类错误。"""


class ConfirmationRequired(ExecutorError):
    """缺少明确确认或预览令牌不一致。"""


class UnsafeOperation(ExecutorError):
    """操作被安全策略拒绝。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _recompute_token(plan_id: str, operations: Iterable[Operation]) -> str:
    payload = json.dumps(
        {"plan_id": plan_id, "operations": [op.to_dict() for op in operations]},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_plan(operations: Iterable[Operation], notes: Optional[list[str]] = None) -> Plan:
    plan_id = uuid.uuid4().hex[:12]
    ops = list(operations)
    return Plan(
        plan_id=plan_id,
        operations=ops,
        token=_recompute_token(plan_id, ops),
        created_at=_now(),
        notes=notes or [],
    )


def _sanitize_rel(rel_path: str) -> str:
    parts = []
    for part in rel_path.replace("\\", "/").split("/"):
        part = part.strip()
        if not part or part in (".", ".."):
            continue
        parts.append(part.replace(":", "_"))
    return "/".join(parts) if parts else "file"


def build_quarantine_plan(
    sources: Iterable[str | os.PathLike[str]],
    quarantine_root: str | os.PathLike[str] | None = None,
    root: Optional[str] = None,
    reasons: Optional[dict[str, str]] = None,
    risks: Optional[dict[str, str]] = None,
) -> Plan:
    """构建隔离计划；目标目录一次性生成，避免覆盖历史隔离文件。"""
    qroot = Path(quarantine_root or default_quarantine_dir())
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    session = qroot / f"run-{stamp}-{uuid.uuid4().hex[:6]}"
    reasons = reasons or {}
    risks = risks or {}
    operations: list[Operation] = []
    for index, raw in enumerate(sources):
        source = os.path.abspath(str(raw))
        if root:
            try:
                rel = os.path.relpath(source, os.path.abspath(root))
            except ValueError:
                rel = os.path.basename(source)
        else:
            rel = os.path.basename(source)
        rel = _sanitize_rel(rel)
        try:
            size = os.stat(source, follow_symlinks=False).st_size
        except OSError:
            size = None
        operations.append(
            Operation(
                op_id=hashlib.sha1(f"quarantine|{source}|{index}".encode("utf-8")).hexdigest()[:12],
                kind=OperationKind.QUARANTINE.value,
                source=source,
                target=str(session / rel),
                expected_size=size,
                reason=reasons.get(source, "隔离副本，原文件保留"),
                risk=risks.get(source, RiskLevel.MEDIUM.value),
                irreversible=False,
            )
        )
    notes = [
        "隔离只复制文件，原文件保持不变；回滚仅承诺清理隔离副本。",
        "执行前请确认预览与令牌；删除不在本计划中。",
    ]
    return build_plan(operations, notes=notes)


def build_move_plan(
    moves: Iterable[tuple[str, str]],
    reason: str = "用户明确指定的移动",
) -> Plan:
    operations: list[Operation] = []
    for index, (source, target) in enumerate(moves):
        source_abs = os.path.abspath(source)
        target_abs = os.path.abspath(target)
        try:
            size = os.stat(source_abs, follow_symlinks=False).st_size
        except OSError:
            size = None
        operations.append(
            Operation(
                op_id=hashlib.sha1(f"move|{source_abs}|{target_abs}|{index}".encode("utf-8")).hexdigest()[:12],
                kind=OperationKind.MOVE.value,
                source=source_abs,
                target=target_abs,
                expected_size=size,
                reason=reason,
                risk=RiskLevel.MEDIUM.value,
                irreversible=False,
            )
        )
    return build_plan(operations, notes=["移动可由用户手动还原，但本工具不承诺回滚。"])


def build_delete_plan(paths: Iterable[str | os.PathLike[str]], reason: str, risk: str = RiskLevel.LOW.value) -> Plan:
    operations: list[Operation] = []
    for index, raw in enumerate(paths):
        source = os.path.abspath(str(raw))
        try:
            size = os.stat(source, follow_symlinks=False).st_size
        except OSError:
            size = None
        operations.append(
            Operation(
                op_id=hashlib.sha1(f"delete|{source}|{index}".encode("utf-8")).hexdigest()[:12],
                kind=OperationKind.DELETE.value,
                source=source,
                target=None,
                expected_size=size,
                reason=reason,
                risk=risk,
                irreversible=True,
            )
        )
    return build_plan(operations, notes=["删除不可撤销，且仅允许显式开启 + 明确确认后执行。"])


def preview_text(plan: Plan) -> str:
    return "\n".join(plan.preview_lines())


def save_plan(plan: Plan, path: str | os.PathLike[str]) -> Path:
    """把预览后的计划连同令牌保存到 JSON，供后续确认执行。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def load_plan(path: str | os.PathLike[str]) -> Plan:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    operations = [Operation(**op) for op in data.get("operations", [])]
    return Plan(
        plan_id=data["plan_id"],
        operations=operations,
        token=data["token"],
        created_at=data.get("created_at", _now()),
        notes=data.get("notes", []),
    )


class Executor:
    def __init__(
        self,
        quarantine_root: str | os.PathLike[str] | None = None,
        log_path: str | os.PathLike[str] | None = None,
        allow_delete: bool = False,
    ) -> None:
        self.quarantine_root = Path(quarantine_root or default_quarantine_dir())
        self.log_path = Path(log_path or default_log_path())
        self.allow_delete = allow_delete

    # ------------------------------------------------------------------ 执行
    def execute(self, plan: Plan, confirm: bool = False, token: Optional[str] = None) -> ExecutionResult:
        if not confirm:
            raise ConfirmationRequired("必须明确确认（confirm=True）后才能执行任何文件操作")
        if not token or not hmac.compare_digest(str(token), plan.token):
            raise ConfirmationRequired("预览令牌缺失或不匹配，请重新预览并复制令牌")
        if not hmac.compare_digest(_recompute_token(plan.plan_id, plan.operations), plan.token):
            raise ConfirmationRequired("计划内容在预览后被修改，令牌已失效")

        self._prevalidate(plan)

        execution_id = uuid.uuid4().hex[:12]
        result = ExecutionResult(execution_id=execution_id, plan_id=plan.plan_id, log_path=str(self.log_path))
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

        for op in plan.operations:
            try:
                if op.kind == OperationKind.QUARANTINE.value:
                    op_result = self._quarantine(execution_id, op)
                elif op.kind == OperationKind.MOVE.value:
                    op_result = self._move(execution_id, op)
                elif op.kind == OperationKind.DELETE.value:
                    op_result = self._delete(execution_id, op)
                else:
                    raise UnsafeOperation(f"不支持的操作类型: {op.kind}")
            except (ExecutorError, OSError) as exc:
                op_result = OperationResult(
                    op_id=op.op_id,
                    kind=op.kind,
                    source=op.source,
                    target=op.target,
                    status="failed",
                    message=f"{type(exc).__name__}: {exc}",
                )
            result.results.append(op_result)
            self._log(
                {
                    "type": "operation",
                    "execution_id": execution_id,
                    "plan_id": plan.plan_id,
                    "timestamp": _now(),
                    "operation": op.to_dict(),
                    "status": op_result.status,
                    "verified": op_result.verified,
                    "message": op_result.message,
                    "sha256": op_result.sha256,
                }
            )

        self._log(
            {
                "type": "execution_complete",
                "execution_id": execution_id,
                "plan_id": plan.plan_id,
                "timestamp": _now(),
                "succeeded": result.succeeded,
                "failed": result.failed,
            }
        )
        return result

    def _prevalidate(self, plan: Plan) -> None:
        errors: list[str] = []
        if not plan.operations:
            raise UnsafeOperation("计划为空，没有可执行操作")
        for op in plan.operations:
            source = op.source
            if not os.path.exists(source):
                errors.append(f"源文件不存在: {source}")
                continue
            if os.path.islink(source):
                errors.append(f"拒绝处理符号链接: {source}")
                continue
            if not os.path.isfile(source):
                errors.append(f"只允许处理文件（不支持目录）: {source}")
                continue
            try:
                current_size = os.stat(source, follow_symlinks=False).st_size
            except OSError as exc:
                errors.append(f"无法读取文件状态: {source} ({exc})")
                continue
            if op.expected_size is not None and current_size != op.expected_size:
                errors.append(
                    f"文件在计划后发生变化（{op.expected_size} -> {current_size}），请重新预览: {source}"
                )

            if op.kind == OperationKind.QUARANTINE.value:
                if not op.target:
                    errors.append(f"隔离操作缺少目标路径: {source}")
                elif not self._within_quarantine(op.target):
                    errors.append(f"隔离目标不在隔离根目录内: {op.target}")
            elif op.kind == OperationKind.MOVE.value:
                if not op.target:
                    errors.append(f"移动操作缺少目标路径: {source}")
                if is_critical_path(source):
                    errors.append(f"拒绝移动系统关键路径: {source}")
                if op.target and is_critical_path(op.target):
                    errors.append(f"拒绝移动到系统关键路径: {op.target}")
                if op.target and os.path.exists(op.target):
                    errors.append(f"移动目标已存在，拒绝覆盖: {op.target}")
            elif op.kind == OperationKind.DELETE.value:
                if not self.allow_delete:
                    errors.append("删除未显式开启（allow_delete=False）")
                if op.risk == RiskLevel.HIGH.value:
                    errors.append(f"拒绝删除高风险 / 未知文件: {source}")
                if is_critical_path(source):
                    errors.append(f"拒绝删除系统关键路径: {source}")
            else:
                errors.append(f"未知操作类型: {op.kind}")
        if errors:
            raise UnsafeOperation("；".join(errors))

    def _quarantine(self, execution_id: str, op: Operation) -> OperationResult:
        source = Path(op.source)
        target = Path(op.target or "")
        if target.exists():
            raise UnsafeOperation(f"隔离目标已存在，拒绝覆盖: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        try:
            if not target.is_file():
                raise OSError("复制后目标文件不存在")
            size_ok = target.stat().st_size == source.stat().st_size
            digest = file_sha256(target)
            if not size_ok:
                raise OSError("复制后大小不一致")
            if op.expected_hash and digest != op.expected_hash:
                raise OSError("复制后 SHA-256 不一致")
        except (OSError, ExecutorError):
            # 复核失败：清理不完整的隔离副本，原文件未受影响。
            try:
                target.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return OperationResult(
            op_id=op.op_id,
            kind=op.kind,
            source=str(source),
            target=str(target),
            status="succeeded",
            message="已复制到隔离目录，原文件保留；可回滚隔离副本",
            verified=True,
            sha256=digest,
        )

    def _move(self, execution_id: str, op: Operation) -> OperationResult:
        source = Path(op.source)
        target = Path(op.target or "")
        if target.exists():
            raise UnsafeOperation(f"移动目标已存在，拒绝覆盖: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        if not target.exists() or source.exists():
            raise OSError("移动复核失败：源或目标状态不符合预期")
        return OperationResult(
            op_id=op.op_id,
            kind=op.kind,
            source=str(source),
            target=str(target),
            status="succeeded",
            message="移动完成（本工具不承诺回滚移动）",
            verified=True,
        )

    def _delete(self, execution_id: str, op: Operation) -> OperationResult:
        source = Path(op.source)
        os.remove(source)
        if source.exists():
            raise OSError("删除复核失败：文件仍存在")
        return OperationResult(
            op_id=op.op_id,
            kind=op.kind,
            source=str(source),
            target=None,
            status="succeeded",
            message="删除完成（不可撤销）",
            verified=True,
        )

    def _within_quarantine(self, target: str) -> bool:
        try:
            target_abs = os.path.normcase(os.path.abspath(target))
            root_abs = os.path.normcase(os.path.abspath(str(self.quarantine_root)))
            return os.path.commonpath([target_abs, root_abs]) == root_abs
        except ValueError:
            return False

    # ------------------------------------------------------------------ 日志
    def _log(self, payload: dict) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def read_log(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        records = []
        with open(self.log_path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return records

    # ------------------------------------------------------------------ 回滚
    def rollback(self, execution_id: str) -> RollbackResult:
        records = [
            record
            for record in self.read_log()
            if record.get("type") == "operation"
            and record.get("execution_id") == execution_id
            and record.get("status") == "succeeded"
        ]
        if not records:
            raise ExecutorError(f"未找到可回滚的执行记录: {execution_id}")

        result = RollbackResult(execution_id=execution_id)
        for record in records:
            operation = record.get("operation", {})
            kind = operation.get("kind")
            source = operation.get("source") or ""
            target = operation.get("target") or ""
            detail = {"op_id": operation.get("op_id"), "kind": kind, "source": source, "target": target}
            if kind != OperationKind.QUARANTINE.value:
                result.skipped += 1
                detail["status"] = "skipped"
                detail["message"] = "仅隔离副本承诺回滚；移动 / 删除不可由本工具回滚"
                result.details.append(detail)
                continue
            source_path = Path(source)
            target_path = Path(target)
            if not target_path.exists():
                result.skipped += 1
                detail["status"] = "skipped"
                detail["message"] = "隔离副本不存在，无需回滚"
                result.details.append(detail)
                continue
            try:
                if source_path.exists():
                    if not source_path.is_file():
                        raise OSError("原路径不是文件")
                    if file_sha256(source_path) != file_sha256(target_path):
                        raise OSError("原文件与隔离副本哈希不一致，拒绝自动处理")
                else:
                    source_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target_path, source_path)
                    if file_sha256(source_path) != file_sha256(target_path):
                        raise OSError("恢复后哈希不一致")
                target_path.unlink()
                result.restored += 1
                detail["status"] = "restored"
                detail["message"] = "隔离副本已清理，原文件确认存在"
            except OSError as exc:
                result.failed += 1
                detail["status"] = "failed"
                detail["message"] = f"{type(exc).__name__}: {exc}"
            result.details.append(detail)

        self._log(
            {
                "type": "rollback",
                "execution_id": execution_id,
                "timestamp": _now(),
                "restored": result.restored,
                "skipped": result.skipped,
                "failed": result.failed,
            }
        )
        return result
