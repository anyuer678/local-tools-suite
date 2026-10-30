"""SQLite 持久化层：单一数据库文件、显式 schema 版本、事务化写入。

设计约束：
- 所有写操作在进程内可重入锁 + 事务中执行，异常时整体回滚。
- 迁移只允许“升级旧库”，遇到更高版本的库直接拒绝，避免静默破坏数据。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtimes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '',
    exe_path TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (kind, exe_path)
);
CREATE INDEX IF NOT EXISTS idx_runtimes_kind ON runtimes(kind);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL COLLATE NOCASE,
    description TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (project_id, name)
);
CREATE INDEX IF NOT EXISTS idx_profiles_project ON profiles(project_id);

CREATE TABLE IF NOT EXISTS profile_vars (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    key TEXT NOT NULL COLLATE NOCASE,
    value TEXT NOT NULL DEFAULT '',
    sensitive INTEGER NOT NULL DEFAULT 0,
    encrypted INTEGER NOT NULL DEFAULT 0,
    secret_blob BLOB,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (profile_id, key)
);
CREATE INDEX IF NOT EXISTS idx_profile_vars_profile ON profile_vars(profile_id);

CREATE TABLE IF NOT EXISTS env_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    values_json TEXT NOT NULL,
    path_json TEXT NOT NULL DEFAULT '[]',
    checksum TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_scope ON env_snapshots(scope, created_at DESC);
"""


class DatabaseError(RuntimeError):
    """数据库不可用或 schema 不兼容。"""


def default_data_dir() -> Path:
    override = os.environ.get("ENVFORGE_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "EnvForge"
    return Path.home() / ".envforge"


def default_db_path() -> Path:
    return default_data_dir() / "envforge.db"


class Database:
    """线程安全的 SQLite 包装。GUI 后台线程与主线程共享同一实例。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_db_path()
        self._lock = threading.RLock()
        self._closed = False
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._conn = sqlite3.connect(str(self.path), timeout=10, check_same_thread=False)
        except sqlite3.Error as exc:  # pragma: no cover - 依赖真实文件系统故障
            raise DatabaseError(f"无法打开数据库 {self.path}：{exc}") from exc
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()
        # 打开时做一次快速完整性自检；失败不阻断运行，由 CLI/GUI 向用户提示恢复路径。
        self.integrity = self.integrity_check()

    # ------------------------------------------------------------------ 基础
    @property
    def version(self) -> int:
        row = self.query_one("SELECT value FROM meta WHERE key = 'schema_version'")
        return int(row["value"]) if row else 0

    def _migrate(self) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            current = int(row[0]) if row else 0
            if current > SCHEMA_VERSION:
                raise DatabaseError(
                    f"数据库 schema 版本为 {current}，高于本程序支持的 {SCHEMA_VERSION}；"
                    "请升级 EnvForge 或改用新的数据库文件。"
                )
            if current < 1:
                self._conn.executescript(_SCHEMA_V1)
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                self._conn.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES('created_at', datetime('now'))"
                )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock, self._conn:
            yield self._conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock, self._conn:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> int:
        with self._lock, self._conn:
            cursor = self._conn.executemany(sql, rows)
            return cursor.rowcount

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    # ------------------------------------------------------------------ 数据保护
    def integrity_check(self) -> dict[str, Any]:
        """PRAGMA quick_check：返回 {'ok': bool, 'detail': str}。"""
        try:
            with self._lock:
                rows = self._conn.execute("PRAGMA quick_check").fetchall()
        except sqlite3.Error as exc:
            return {"ok": False, "detail": f"无法执行完整性检查：{exc}"}
        problems = [str(row[0]) for row in rows]
        if not problems:
            return {"ok": False, "detail": "完整性检查没有返回结果。"}
        if problems == ["ok"]:
            return {"ok": True, "detail": "ok"}
        return {"ok": False, "detail": "；".join(problems[:5])}

    def backup_to(self, target: str | Path) -> Path:
        """用 SQLite 在线备份 API 生成一致副本（WAL 内容会一并包含）。"""
        destination = Path(target)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DatabaseError(f"无法创建备份目录 {destination.parent}：{exc}") from exc
        with self._lock:
            try:
                dest = sqlite3.connect(str(destination))
            except sqlite3.Error as exc:
                raise DatabaseError(f"无法创建备份文件 {destination}：{exc}") from exc
            try:
                self._conn.backup(dest)
            except sqlite3.Error as exc:
                raise DatabaseError(f"备份到 {destination} 失败：{exc}") from exc
            finally:
                dest.close()
        return destination

    def backup_daily(self, *, keep: int = 7, directory: str | Path | None = None) -> Path | None:
        """每天最多备份一次；返回新备份路径，当天已有或内存库返回 None。"""
        if str(self.path) == ":memory:":
            return None
        folder = Path(directory) if directory is not None else self.path.parent / "backups"
        now = datetime.now()
        today = now.strftime("%Y%m%d")
        try:
            today_files = sorted(folder.glob(f"envforge-{today}-*.db"))
        except OSError:
            today_files = []
        if today_files:
            return None
        target = folder / f"envforge-{today}-{now:%H%M%S}.db"
        self.backup_to(target)
        if keep > 0:
            try:
                backups = sorted(folder.glob("envforge-*.db"))
            except OSError:
                backups = []
            for old in backups[:-keep]:
                try:
                    old.unlink()
                except OSError:
                    continue
        return target

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
