"""环境快照、差异、备份导出与回滚。

安全默认值：任何写入都要求显式确认；写入前自动创建“回滚前备份”快照；
只允许写入当前用户作用域（system 只读，session 只影响 EnvForge 进程）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .db import Database
from .envvars import EnvProvider, analyze_path, provider_for, split_path
from .errors import (
    ConfirmationRequired,
    EnvAccessError,
    EnvWriteFailure,
    NotFoundError,
    ValidationError,
)
from .lock import file_lock
from .models import EnvSnapshot, looks_sensitive_key, now_iso
from .secrets import REDACTED


def _path_key(values: Mapping[str, str]) -> str | None:
    for key in values:
        if key.upper() == "PATH":
            return key
    return None


def _checksum(values: Mapping[str, str], label: str) -> str:
    payload = json.dumps(
        {"label": label, "values": dict(sorted(values.items()))},
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _masked(value: str, sensitive: bool, reveal: bool) -> str:
    if sensitive and not reveal:
        return REDACTED
    return value


class EnvService:
    def __init__(
        self,
        db: Database,
        providers: Mapping[str, EnvProvider] | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        self.db = db
        self._providers = dict(providers or {})
        self.audit = audit
        self._lock_dir = (
            Path(db.path).parent if str(db.path) != ":memory:" else None
        )

    # ------------------------------------------------------------------ 读取
    def provider(self, scope: str) -> EnvProvider:
        return provider_for(scope, self._providers)

    def current(self, scope: str) -> EnvSnapshot:
        provider = self.provider(scope)
        values = provider.list_vars()
        path_key = _path_key(values)
        return EnvSnapshot(
            scope=scope,
            values=values,
            path_entries=split_path(values.get(path_key, "")) if path_key else [],
        )

    def path_report(self, scope: str) -> list[dict[str, Any]]:
        provider = self.provider(scope)
        values = provider.list_vars()
        path_value = next((value for key, value in values.items() if key.upper() == "PATH"), "")
        report = [entry.to_dict() for entry in analyze_path(path_value)]
        for item in report:
            if not item["raw"]:
                item["status"] = "empty"
            elif item["duplicate_of"] is not None:
                item["status"] = "duplicate"
            elif not item["exists"]:
                item["status"] = "missing"
            else:
                item["status"] = "ok"
        return report

    # ------------------------------------------------------------------ 快照
    def create(self, scope: str, label: str = "") -> EnvSnapshot:
        current = self.current(scope)
        if scope not in ("user", "system", "session"):
            raise ValidationError(f"未知作用域 {scope!r}。")
        created = now_iso()
        values = current.values
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO env_snapshots(scope, label, created_at, values_json, path_json, checksum)"
                " VALUES(?,?,?,?,?,?)",
                (
                    scope,
                    label,
                    created,
                    json.dumps(values, ensure_ascii=False, sort_keys=True),
                    json.dumps(current.path_entries, ensure_ascii=False),
                    _checksum(values, label),
                ),
            )
            snapshot_id = int(cursor.lastrowid)
        return EnvSnapshot(
            scope=scope,
            values=values,
            path_entries=current.path_entries,
            label=label,
            id=snapshot_id,
            created_at=created,
        )

    def list(self, scope: str | None = None, limit: int = 100) -> list[EnvSnapshot]:
        if scope:
            rows = self.db.query(
                "SELECT * FROM env_snapshots WHERE scope = ? ORDER BY id DESC LIMIT ?",
                (scope, int(limit)),
            )
        else:
            rows = self.db.query(
                "SELECT * FROM env_snapshots ORDER BY id DESC LIMIT ?", (int(limit),)
            )
        return [self._from_row(row) for row in rows]

    def get(self, snapshot_id: int) -> EnvSnapshot:
        row = self.db.query_one("SELECT * FROM env_snapshots WHERE id = ?", (snapshot_id,))
        if not row:
            raise NotFoundError(f"快照 {snapshot_id} 不存在。")
        return self._from_row(row)

    def delete(self, snapshot_id: int) -> None:
        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM env_snapshots WHERE id = ?", (snapshot_id,))
        if cursor.rowcount == 0:
            raise NotFoundError(f"快照 {snapshot_id} 不存在。")

    @staticmethod
    def _from_row(row: Mapping[str, Any]) -> EnvSnapshot:
        try:
            values = json.loads(row["values_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            values = {}
        try:
            path_entries = json.loads(row["path_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            path_entries = []
        return EnvSnapshot(
            scope=row["scope"],
            values={str(k): str(v) for k, v in values.items()},
            path_entries=[str(item) for item in path_entries],
            label=row["label"],
            id=row["id"],
            created_at=row["created_at"],
        )

    # ------------------------------------------------------------------ 差异
    def diff(self, before_id: int, after_id: int, reveal: bool = False) -> dict[str, Any]:
        before = self.get(before_id)
        after = self.get(after_id)
        added = {
            key: _masked(value, looks_sensitive_key(key), reveal)
            for key, value in after.values.items()
            if key not in before.values
        }
        removed = {
            key: _masked(value, looks_sensitive_key(key), reveal)
            for key, value in before.values.items()
            if key not in after.values
        }
        changed: list[dict[str, Any]] = []
        for key in sorted(set(before.values) & set(after.values)):
            if before.values[key] == after.values[key]:
                continue
            sensitive = looks_sensitive_key(key)
            changed.append(
                {
                    "key": key,
                    "before": _masked(before.values[key], sensitive, reveal),
                    "after": _masked(after.values[key], sensitive, reveal),
                }
            )
        return {
            "before": before.to_dict(reveal=reveal),
            "after": after.to_dict(reveal=reveal),
            "added": added,
            "removed": removed,
            "changed": changed,
            "path_removed": [item for item in before.path_entries if item not in after.path_entries],
            "path_added": [item for item in after.path_entries if item not in before.path_entries],
        }

    def export(self, snapshot_id: int, reveal: bool = False) -> dict[str, Any]:
        snapshot = self.get(snapshot_id)
        data = snapshot.to_dict(reveal=reveal)
        data["format"] = "envforge-snapshot-v1"
        data["sensitive_values_masked"] = not reveal
        return data

    # ------------------------------------------------------------------ 写入
    def apply(
        self,
        scope: str,
        updates: Mapping[str, str | None],
        *,
        confirm: bool = False,
        label: str = "",
        lock_timeout: float = 8.0,
    ) -> dict[str, Any]:
        """写入并校验：写前快照 → 跨进程锁 → 写入 → 回读比对 →（失败则回滚）。"""
        if not updates:
            raise ValidationError("没有需要写入的变更。")
        if not confirm:
            raise ConfirmationRequired(
                f"将修改{scope}环境变量，这是可影响其他程序的系统级操作。"
                "请加 --yes 显式确认；程序会先自动创建回滚快照。"
            )
        provider = self.provider(scope)
        if not provider.writable:
            raise EnvAccessError(
                f"{scope} 作用域在当前版本中只读；请选择 user 作用域，"
                "或手动在系统设置中修改。"
            )
        pre = self.create(scope, label=label or "写入前自动备份")
        changed = [key for key, value in updates.items() if value is not None]
        deleted = [key for key, value in updates.items() if value is None]

        with file_lock(f"env-{scope}", lock_dir=self._lock_dir, timeout=lock_timeout):
            try:
                provider.write(updates)
                failed = self._readback_mismatches(provider, updates)
            except Exception as exc:
                rollback_ok, rolled = self._rollback(provider, pre, updates)
                message = (
                    f"写入{scope}环境变量时出错：{exc}。"
                    + self._rollback_note(rollback_ok, rolled, pre.id)
                )
                self._audit(
                    scope,
                    updates,
                    "rolled_back" if rollback_ok else "failed",
                    pre.id,
                    f"写入异常：{type(exc).__name__}",
                )
                raise EnvWriteFailure(
                    message,
                    scope=scope,
                    pre_snapshot_id=pre.id,
                    failed_keys=changed + deleted,
                    rollback_ok=rollback_ok,
                    partial=not rollback_ok,
                ) from exc
            if failed:
                rollback_ok, rolled = self._rollback(provider, pre, updates)
                message = (
                    f"写入后回读校验失败：{'、'.join(failed)} 的实际值与预期不一致。"
                    + self._rollback_note(rollback_ok, rolled, pre.id)
                )
                self._audit(
                    scope,
                    updates,
                    "rolled_back" if rollback_ok else "failed",
                    pre.id,
                    "回读校验失败",
                )
                raise EnvWriteFailure(
                    message,
                    scope=scope,
                    pre_snapshot_id=pre.id,
                    failed_keys=failed,
                    rollback_ok=rollback_ok,
                    partial=not rollback_ok,
                )

        self._audit(scope, updates, "ok", pre.id, "已回读校验")
        return {
            "scope": scope,
            "changed": changed,
            "deleted": deleted,
            "pre_snapshot_id": pre.id,
            "pre_snapshot_created_at": pre.created_at,
            "verified": True,
            "rollback_ok": None,
        }

    def _readback_mismatches(
        self, provider: EnvProvider, updates: Mapping[str, str | None]
    ) -> list[str]:
        """写入后回读：返回与预期不一致的键名（空列表表示全部通过）。"""
        current = provider.list_vars()
        actual = {key.upper(): value for key, value in current.items()}
        failed: list[str] = []
        for key, value in updates.items():
            got = actual.get(key.upper())
            if value is None:
                if got is not None:
                    failed.append(key)
            elif got != value:
                failed.append(key)
        return failed

    def _rollback(
        self,
        provider: EnvProvider,
        pre: EnvSnapshot,
        updates: Mapping[str, str | None],
    ) -> tuple[bool, dict[str, str | None]]:
        """把写入前快照恢复回去；仅撤销本次涉及的键，不动其他变量。"""
        try:
            current = provider.list_vars()
            current_names = {key.upper(): key for key in current}
            snapshot_names = {key.upper(): key for key in pre.values}
            rollback: dict[str, str | None] = {}
            for key, value in pre.values.items():
                name = current_names.get(key.upper(), key)
                if current.get(name) != value:
                    rollback[name] = value
            for key in updates:
                upper = key.upper()
                if upper not in snapshot_names and upper in current_names:
                    rollback[current_names[upper]] = None
            if rollback:
                provider.write(rollback)
            return True, rollback
        except Exception:
            return False, {}

    @staticmethod
    def _rollback_note(ok: bool, rolled: Mapping[str, str | None], snapshot_id: int | None) -> str:
        if ok:
            return f"已自动回滚 {len(rolled)} 个变量到写入前快照 #{snapshot_id}。"
        return f"自动回滚失败，环境可能处于不一致状态；请手动执行 `envforge env restore {snapshot_id} --yes`。"

    def _audit(
        self,
        scope: str,
        updates: Mapping[str, str | None],
        result: str,
        snapshot_id: int | None,
        detail: str,
        *,
        action: str = "env.apply",
    ) -> None:
        if self.audit is None:
            return
        self.audit.append(
            action,
            scope=scope,
            keys=list(updates),
            result=result,
            snapshot_id=snapshot_id,
            detail=detail,
        )

    def restore(
        self,
        snapshot_id: int,
        *,
        confirm: bool = False,
        delete_extras: bool = False,
    ) -> dict[str, Any]:
        snapshot = self.get(snapshot_id)
        provider = self.provider(snapshot.scope)
        if not provider.writable:
            raise EnvAccessError(
                f"快照作用域 {snapshot.scope} 只读，无法回滚；"
                "本版本仅支持回滚 user 作用域快照。"
            )
        current = provider.list_vars()
        updates: dict[str, str | None] = {}
        for key, value in snapshot.values.items():
            if current.get(key) != value:
                updates[key] = value
        if delete_extras:
            for key in current:
                if key not in snapshot.values:
                    updates[key] = None
        if not updates:
            return {"scope": snapshot.scope, "changed": [], "deleted": [], "pre_snapshot_id": None}
        result = self.apply(
            snapshot.scope,
            updates,
            confirm=confirm,
            label=f"回滚到快照 #{snapshot_id} 前自动备份",
        )
        result["restored_snapshot_id"] = snapshot_id
        self._audit(
            snapshot.scope,
            updates,
            "ok",
            result["pre_snapshot_id"],
            f"回滚到快照 #{snapshot_id}",
            action="env.restore",
        )
        return result
