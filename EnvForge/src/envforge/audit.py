"""本地审计日志与崩溃日志。

设计约束：
- 只记录“做了什么”：动作、作用域、变量名、结果、快照号、失败键名。
- 绝不记录变量值；detail 只允许写入固定的说明文字。
- 按天一个文件，追加写入 JSONL；审计保留 30 天，崩溃日志保留 90 天。
- 审计失败不能影响主流程，因此 append/记录异常全部吞掉并返回 None。
"""

from __future__ import annotations

import json
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

ACTION_LABELS = {
    "env.apply": "写入环境变量",
    "env.restore": "回滚环境快照",
    "env.db-check": "数据库完整性自检",
    "env.db-backup": "数据库备份",
    "profile.set": "设置档案变量",
    "profile.unset": "删除档案变量",
    "profile.remove": "删除环境档案",
    "profile.activate": "激活环境档案",
    "project.add": "登记项目",
    "project.remove": "删除项目",
    "gui.start": "启动界面",
}


def action_label(action: str) -> str:
    return ACTION_LABELS.get(action, action)


def default_log_dir() -> Path:
    from .db import default_data_dir

    return default_data_dir() / "logs"


class AuditLog:
    """JSONL 审计日志。目录延迟创建，append 失败不影响业务。"""

    def __init__(self, directory: str | Path | None = None, *, retention_days: int = 30) -> None:
        self.dir = Path(directory) if directory is not None else default_log_dir()
        self.retention_days = retention_days
        self._lock = threading.Lock()
        self._pruned_on = ""
        self.last_error = ""

    @classmethod
    def for_database(cls, db_path: str | Path, **kwargs: Any) -> AuditLog:
        """数据库旁的 logs 目录：自定义 --db 时日志跟随数据库，便于整体迁移。"""
        text = str(db_path)
        if text == ":memory:":
            return cls(None, **kwargs)
        return cls(Path(text).parent / "logs", **kwargs)

    # ------------------------------------------------------------------ 写入
    def append(
        self,
        action: str,
        *,
        scope: str = "",
        keys: Iterable[str] = (),
        result: str = "ok",
        snapshot_id: int | None = None,
        detail: str = "",
    ) -> dict[str, Any] | None:
        key_list = [str(item) for item in keys]
        entry: dict[str, Any] = {
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "action": str(action),
            "scope": scope,
            "keys": key_list[:50],
            "key_count": len(key_list),
            "result": result,
            "snapshot_id": snapshot_id,
            "detail": detail[:200],
        }
        with self._lock:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                with self._path("audit").open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
                self._prune()
            except OSError as exc:
                self.last_error = str(exc)
                return None
        self.last_error = ""
        return entry

    # ------------------------------------------------------------------ 读取
    def recent(self, limit: int = 50, action: str | None = None) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        if limit <= 0:
            return entries
        try:
            files = sorted(self.dir.glob("audit-*.jsonl"), reverse=True)
        except OSError:
            return entries
        for path in files:
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict):
                    continue
                if action and item.get("action") != action:
                    continue
                entries.append(item)
                if len(entries) >= limit:
                    return entries
        return entries

    def format_lines(self, entries: list[dict[str, Any]]) -> str:
        if not entries:
            return "暂无操作记录。"
        lines = []
        for item in entries:
            ts = str(item.get("ts", "")).replace("T", " ")[:19]
            keys = "、".join(item.get("keys") or []) or "-"
            snapshot = item.get("snapshot_id")
            snapshot_text = f"  快照 #{snapshot}" if snapshot else ""
            detail = item.get("detail") or ""
            lines.append(
                f"{ts}  {action_label(str(item.get('action', ''))):<8}  "
                f"{item.get('scope') or '-':<8}  {item.get('result') or '-':<10}  "
                f"{keys}{snapshot_text}"
                + (f"  {detail}" if detail else "")
            )
        return "\n".join(lines)

    # ------------------------------------------------------------------ 内部
    def _path(self, prefix: str) -> Path:
        return self.dir / f"{prefix}-{datetime.now():%Y%m%d}.jsonl"

    def _prune(self) -> None:
        today = datetime.now().strftime("%Y%m%d")
        if self._pruned_on == today:
            return
        self._pruned_on = today
        self._prune_dir("audit-*.jsonl", self.retention_days)

    def _prune_dir(self, pattern: str, days: int) -> None:
        if days <= 0:
            return
        cutoff = datetime.now().timestamp() - days * 86400
        for path in self.dir.glob(pattern):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                continue


def write_crash(exc_type, exc, tb, *, directory: str | Path | None = None) -> Path | None:
    """把未捕获异常写入 crash-YYYYMMDD.log，返回文件路径（失败返回 None）。"""
    folder = Path(directory) if directory is not None else default_log_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"crash-{datetime.now():%Y%m%d}.log"
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                f"\n[{datetime.now().astimezone().isoformat(timespec='seconds')}] "
                f"未捕获异常\n{text}"
            )
        return path
    except OSError:
        return None
    finally:
        _prune_crash(folder, 90)


def _prune_crash(folder: Path, days: int) -> None:
    cutoff = datetime.now().timestamp() - days * 86400
    try:
        files = folder.glob("crash-*.log")
    except OSError:
        return
    for path in files:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue


def install_crash_hook(directory: str | Path | None = None) -> None:
    """安装全局异常钩子：写崩溃日志后再交给原钩子（不改变退出行为）。"""
    previous = sys.excepthook
    folder = Path(directory) if directory is not None else default_log_dir()

    def hook(exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        write_crash(exc_type, exc, tb, directory=folder)
        previous(exc_type, exc, tb)

    sys.excepthook = hook
