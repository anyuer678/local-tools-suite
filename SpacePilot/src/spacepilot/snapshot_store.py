"""SQLite 快照存储：事务、索引、schema_version 迁移与查询聚合。

设计要点：
- 扫描按批写入 entries / dirs，内存只保留聚合结果。
- 每次扫描一个 scan_id，可保存多个历史快照。
- 所有查询都带 scan_id 索引，避免全表扫描。
"""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from .models import (
    DirectoryRecord,
    FileRecord,
    ScanInfo,
    ScanStats,
    WatchTarget,
    human_size,
)


SCHEMA_VERSION = 2

SCHEMA_V1_SQL = """
CREATE TABLE scans (
    id TEXT PRIMARY KEY,
    root TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    files_scanned INTEGER NOT NULL DEFAULT 0,
    dirs_scanned INTEGER NOT NULL DEFAULT 0,
    total_size INTEGER NOT NULL DEFAULT 0,
    files_skipped INTEGER NOT NULL DEFAULT 0,
    dirs_skipped INTEGER NOT NULL DEFAULT 0,
    symlinks_skipped INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    unknown_size INTEGER NOT NULL DEFAULT 0,
    volatile INTEGER NOT NULL DEFAULT 0,
    zero_byte INTEGER NOT NULL DEFAULT 0,
    cancelled INTEGER NOT NULL DEFAULT 0,
    complete INTEGER NOT NULL DEFAULT 0,
    options_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    suffix TEXT NOT NULL DEFAULT '',
    size INTEGER,
    mtime_ns INTEGER NOT NULL DEFAULT 0,
    ctime_ns INTEGER NOT NULL DEFAULT 0,
    inode INTEGER NOT NULL DEFAULT 0,
    device INTEGER NOT NULL DEFAULT 0,
    nlink INTEGER NOT NULL DEFAULT 1,
    kind TEXT NOT NULL DEFAULT 'file',
    status TEXT NOT NULL DEFAULT 'scanned',
    reason TEXT NOT NULL DEFAULT '',
    volatile INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE dirs (
    scan_id TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    file_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'scanned'
);
CREATE INDEX idx_entries_scan_rel ON entries(scan_id, rel_path);
CREATE INDEX idx_entries_scan_status ON entries(scan_id, status);
CREATE INDEX idx_entries_scan_size ON entries(scan_id, size);
CREATE INDEX idx_entries_scan_suffix ON entries(scan_id, suffix);
CREATE INDEX idx_entries_scan_inode ON entries(scan_id, device, inode);
CREATE INDEX idx_dirs_scan_rel ON dirs(scan_id, rel_path);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _row_to_entry(row: sqlite3.Row) -> FileRecord:
    return FileRecord(
        path=row["path"],
        rel_path=row["rel_path"],
        name=row["name"],
        suffix=row["suffix"],
        size=row["size"],
        mtime_ns=row["mtime_ns"],
        ctime_ns=row["ctime_ns"],
        inode=row["inode"],
        device=row["device"],
        nlink=row["nlink"],
        kind=row["kind"],
        status=row["status"],
        reason=row["reason"],
        volatile=bool(row["volatile"]),
        sha256=row["hash"] if "hash" in row.keys() else None,
    )


def _row_to_stats(row: sqlite3.Row) -> ScanStats:
    return ScanStats(
        files_scanned=row["files_scanned"],
        dirs_scanned=row["dirs_scanned"],
        total_size=row["total_size"],
        files_skipped=row["files_skipped"],
        dirs_skipped=row["dirs_skipped"],
        symlinks_skipped=row["symlinks_skipped"],
        failed=row["failed"],
        unknown_size=row["unknown_size"],
        volatile=row["volatile"],
        zero_byte=row["zero_byte"],
        cancelled=bool(row["cancelled"]),
        complete=bool(row["complete"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"] or "",
    )


class SnapshotStore:
    """快照数据库封装。可在多线程间共享（内部使用 RLock + 串行化写）。"""

    def __init__(self, db_path: str | os.PathLike[str] = ":memory:") -> None:
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        self._conn: Optional[sqlite3.Connection] = None
        self.open()

    # ------------------------------------------------------------------ 连接
    def open(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is not None:
                return self._conn
            if self.db_path != ":memory:":
                Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass
            self._conn = conn
            self._migrate(conn)
            return conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> "SnapshotStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def connection(self) -> sqlite3.Connection:
        if self._conn is None:
            return self.open()
        return self._conn

    def schema_version(self) -> int:
        return int(self.connection.execute("PRAGMA user_version").fetchone()[0])

    # ------------------------------------------------------------------ 迁移
    def _migrate(self, conn: sqlite3.Connection) -> None:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"数据库 schema_version={version} 高于程序支持的 {SCHEMA_VERSION}，请升级 SpacePilot"
            )

        if version == 0:
            existing = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "entries" in existing:
                # 有表但没有版本号：补齐 v2 字段并设置版本。
                self._upgrade_entries_hash(conn, existing)
                conn.execute("CREATE TABLE IF NOT EXISTS watch_targets("
                             "rel_path TEXT PRIMARY KEY COLLATE NOCASE,"
                             "threshold_bytes INTEGER NOT NULL DEFAULT 0,"
                             "baseline_bytes INTEGER NOT NULL DEFAULT 0,"
                             "created_at TEXT NOT NULL DEFAULT '')")
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                conn.commit()
                return
            with conn:
                conn.executescript(SCHEMA_V1_SQL)
                self._upgrade_entries_hash(conn, existing | {"entries"})
                conn.execute("CREATE TABLE IF NOT EXISTS watch_targets("
                             "rel_path TEXT PRIMARY KEY COLLATE NOCASE,"
                             "threshold_bytes INTEGER NOT NULL DEFAULT 0,"
                             "baseline_bytes INTEGER NOT NULL DEFAULT 0,"
                             "created_at TEXT NOT NULL DEFAULT '')")
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            return

        if version == 1:
            with conn:
                self._upgrade_entries_hash(conn, {"entries"})
                conn.execute("CREATE TABLE IF NOT EXISTS watch_targets("
                             "rel_path TEXT PRIMARY KEY COLLATE NOCASE,"
                             "threshold_bytes INTEGER NOT NULL DEFAULT 0,"
                             "baseline_bytes INTEGER NOT NULL DEFAULT 0,"
                             "created_at TEXT NOT NULL DEFAULT '')")
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            return

        self._create_indexes(conn)
        conn.execute("CREATE TABLE IF NOT EXISTS watch_targets("
                     "rel_path TEXT PRIMARY KEY COLLATE NOCASE,"
                     "threshold_bytes INTEGER NOT NULL DEFAULT 0,"
                     "baseline_bytes INTEGER NOT NULL DEFAULT 0,"
                     "created_at TEXT NOT NULL DEFAULT '')")
        conn.commit()

    def _upgrade_entries_hash(self, conn: sqlite3.Connection, tables: set[str]) -> None:
        if "entries" not in tables:
            return
        columns = {row[1] for row in conn.execute("PRAGMA table_info(entries)").fetchall()}
        if "hash" not in columns:
            conn.execute("ALTER TABLE entries ADD COLUMN hash TEXT")
        self._create_indexes(conn)

    def _create_indexes(self, conn: sqlite3.Connection) -> None:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_scan_rel ON entries(scan_id, rel_path)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_scan_status ON entries(scan_id, status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_scan_size ON entries(scan_id, size)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_scan_suffix ON entries(scan_id, suffix)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_scan_inode ON entries(scan_id, device, inode)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_scan_hash ON entries(scan_id, hash)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dirs_scan_rel ON dirs(scan_id, rel_path)")

    # ------------------------------------------------------------------ 写入
    def create_scan(self, root: str, options: Optional[dict[str, Any]] = None) -> str:
        scan_id = uuid.uuid4().hex
        started = utc_now()
        payload = json.dumps(options or {}, ensure_ascii=False, sort_keys=True)
        with self._lock, self.connection as conn:
            conn.execute(
                "INSERT INTO scans(id, root, started_at, options_json) VALUES (?, ?, ?, ?)",
                (scan_id, str(root), started, payload),
            )
        return scan_id

    def insert_entries(self, scan_id: str, records: Iterable[FileRecord]) -> int:
        rows = [
            (
                scan_id,
                record.rel_path,
                record.path,
                record.name,
                record.suffix,
                record.size,
                record.mtime_ns,
                record.ctime_ns,
                record.inode,
                record.device,
                record.nlink,
                record.kind,
                record.status,
                record.reason,
                1 if record.volatile else 0,
                record.sha256,
            )
            for record in records
        ]
        if not rows:
            return 0
        with self._lock, self.connection as conn:
            conn.executemany(
                "INSERT INTO entries(scan_id, rel_path, path, name, suffix, size, mtime_ns, "
                "ctime_ns, inode, device, nlink, kind, status, reason, volatile, hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def insert_dirs(self, scan_id: str, records: Iterable[DirectoryRecord]) -> int:
        rows = [
            (
                scan_id,
                record.rel_path,
                record.path,
                record.size,
                record.file_count,
                record.failed_count,
                record.skipped_count,
                record.status,
            )
            for record in records
        ]
        if not rows:
            return 0
        with self._lock, self.connection as conn:
            conn.executemany(
                "INSERT INTO dirs(scan_id, rel_path, path, size, file_count, failed_count, "
                "skipped_count, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)

    def finalize_scan(self, scan_id: str, stats: ScanStats) -> None:
        finished = stats.finished_at or utc_now()
        stats.finished_at = finished
        with self._lock, self.connection as conn:
            conn.execute(
                "UPDATE scans SET finished_at=?, files_scanned=?, dirs_scanned=?, total_size=?, "
                "files_skipped=?, dirs_skipped=?, symlinks_skipped=?, failed=?, unknown_size=?, "
                "volatile=?, zero_byte=?, cancelled=?, complete=? WHERE id=?",
                (
                    finished,
                    stats.files_scanned,
                    stats.dirs_scanned,
                    stats.total_size,
                    stats.files_skipped,
                    stats.dirs_skipped,
                    stats.symlinks_skipped,
                    stats.failed,
                    stats.unknown_size,
                    stats.volatile,
                    stats.zero_byte,
                    1 if stats.cancelled else 0,
                    1 if stats.complete else 0,
                    scan_id,
                ),
            )

    def set_entry_hash(self, scan_id: str, rel_path: str, sha256: str) -> None:
        with self._lock, self.connection as conn:
            conn.execute(
                "UPDATE entries SET hash=? WHERE scan_id=? AND rel_path=?",
                (sha256, scan_id, rel_path),
            )

    def delete_scan(self, scan_id: str) -> None:
        with self._lock, self.connection as conn:
            conn.execute("DELETE FROM entries WHERE scan_id=?", (scan_id,))
            conn.execute("DELETE FROM dirs WHERE scan_id=?", (scan_id,))
            conn.execute("DELETE FROM scans WHERE id=?", (scan_id,))

    # ------------------------------------------------------------------ 读取
    def get_scan(self, scan_id: str) -> Optional[ScanInfo]:
        row = self.connection.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        if row is None:
            return None
        return self._scan_from_row(row)

    def latest_scan(self) -> Optional[ScanInfo]:
        row = self.connection.execute(
            "SELECT * FROM scans ORDER BY started_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return self._scan_from_row(row) if row else None

    def list_scans(self, limit: int = 50) -> list[ScanInfo]:
        rows = self.connection.execute(
            "SELECT * FROM scans ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._scan_from_row(row) for row in rows]

    def resolve_scan_id(self, prefix: str) -> str:
        prefix = prefix.strip()
        if not prefix:
            raise ValueError("扫描 ID 不能为空")
        exact = self.connection.execute("SELECT id FROM scans WHERE id=?", (prefix,)).fetchone()
        if exact:
            return exact["id"]
        rows = self.connection.execute("SELECT id FROM scans WHERE id LIKE ?", (prefix + "%",)).fetchall()
        if not rows:
            raise ValueError(f"未找到扫描: {prefix}")
        if len(rows) > 1:
            raise ValueError(f"扫描 ID 前缀不唯一: {prefix}")
        return rows[0]["id"]

    def _scan_from_row(self, row: sqlite3.Row) -> ScanInfo:
        options: dict[str, Any] = {}
        try:
            options = json.loads(row["options_json"] or "{}")
        except json.JSONDecodeError:
            options = {}
        stats = _row_to_stats(row)
        if stats.started_at and stats.finished_at:
            try:
                start = datetime.fromisoformat(stats.started_at)
                end = datetime.fromisoformat(stats.finished_at)
                stats.duration_seconds = max(0.0, (end - start).total_seconds())
            except ValueError:
                pass
        return ScanInfo(
            scan_id=row["id"],
            root=row["root"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            stats=stats,
            options=options,
        )

    def iter_entries(
        self,
        scan_id: str,
        status: Optional[str] = None,
        kind: Optional[str] = None,
        min_size: Optional[int] = None,
    ) -> Iterator[FileRecord]:
        query = "SELECT * FROM entries WHERE scan_id=?"
        params: list[Any] = [scan_id]
        if status:
            query += " AND status=?"
            params.append(status)
        if kind:
            query += " AND kind=?"
            params.append(kind)
        if min_size is not None:
            query += " AND size>=?"
            params.append(int(min_size))
        query += " ORDER BY id"
        cursor = self.connection.execute(query, params)
        for row in cursor:
            yield _row_to_entry(row)

    def count_entries(self, scan_id: str, status: Optional[str] = None, kind: Optional[str] = None) -> int:
        query = "SELECT COUNT(*) AS n FROM entries WHERE scan_id=?"
        params: list[Any] = [scan_id]
        if status:
            query += " AND status=?"
            params.append(status)
        if kind:
            query += " AND kind=?"
            params.append(kind)
        return int(self.connection.execute(query, params).fetchone()["n"])

    def get_entry(self, scan_id: str, rel_path: str) -> Optional[FileRecord]:
        row = self.connection.execute(
            "SELECT * FROM entries WHERE scan_id=? AND rel_path=? ORDER BY id LIMIT 1",
            (scan_id, rel_path),
        ).fetchone()
        return _row_to_entry(row) if row else None

    def stored_hash(self, scan_id: str, rel_path: str) -> Optional[str]:
        row = self.connection.execute(
            "SELECT hash FROM entries WHERE scan_id=? AND rel_path=? AND hash IS NOT NULL LIMIT 1",
            (scan_id, rel_path),
        ).fetchone()
        return row["hash"] if row else None

    def extension_stats(self, scan_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT suffix, COUNT(*) AS file_count, COALESCE(SUM(size), 0) AS total_size "
            "FROM entries WHERE scan_id=? AND status='scanned' AND kind='file' "
            "GROUP BY suffix ORDER BY total_size DESC, file_count DESC LIMIT ?",
            (scan_id, limit),
        ).fetchall()
        return [
            {
                "suffix": row["suffix"] or "(无后缀)",
                "file_count": row["file_count"],
                "total_size": row["total_size"],
                "total_size_human": human_size(row["total_size"]),
            }
            for row in rows
        ]

    def largest_files(self, scan_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT rel_path, path, size, mtime_ns, volatile, hash FROM entries "
            "WHERE scan_id=? AND status='scanned' AND kind='file' "
            "ORDER BY size DESC LIMIT ?",
            (scan_id, limit),
        ).fetchall()
        return [
            {
                "rel_path": row["rel_path"],
                "path": row["path"],
                "size": row["size"],
                "size_human": human_size(row["size"]),
                "mtime_ns": row["mtime_ns"],
                "volatile": bool(row["volatile"]),
                "sha256": row["hash"],
            }
            for row in rows
        ]

    def query_entries(
        self,
        scan_id: str,
        min_size: Optional[int] = None,
        max_size: Optional[int] = None,
        suffix: Optional[str] = None,
        contains: Optional[str] = None,
        sort_by: str = "size",
        descending: bool = True,
        limit: int = 100,
        status: Optional[str] = "scanned",
    ) -> list[FileRecord]:
        """按条件排序 / 过滤文件记录，供 CLI 与 GUI 复用。"""
        sort_columns = {
            "size": "size",
            "mtime": "mtime_ns",
            "path": "rel_path",
            "name": "name",
        }
        if sort_by not in sort_columns:
            raise ValueError(f"不支持的排序字段: {sort_by}")
        query = "SELECT * FROM entries WHERE scan_id=? AND kind='file'"
        params: list[Any] = [scan_id]
        if status:
            query += " AND status=?"
            params.append(status)
        if min_size is not None:
            query += " AND size>=?"
            params.append(int(min_size))
        if max_size is not None:
            query += " AND size<=?"
            params.append(int(max_size))
        if suffix:
            query += " AND suffix=?"
            params.append("" if suffix == "(无后缀)" else suffix.lower())
        if contains:
            query += " AND path LIKE ?"
            params.append(f"%{contains}%")
        order = "DESC" if descending else "ASC"
        query += f" ORDER BY {sort_columns[sort_by]} {order}, rel_path ASC LIMIT ?"
        params.append(max(1, int(limit)))
        rows = self.connection.execute(query, params).fetchall()
        return [_row_to_entry(row) for row in rows]

    def largest_dirs(self, scan_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT rel_path, path, size, file_count FROM dirs "
            "WHERE scan_id=? AND status='scanned' ORDER BY size DESC LIMIT ?",
            (scan_id, limit),
        ).fetchall()
        return [
            {
                "rel_path": row["rel_path"],
                "path": row["path"],
                "size": row["size"],
                "size_human": human_size(row["size"]),
                "file_count": row["file_count"],
            }
            for row in rows
        ]

    def iter_dirs(self, scan_id: str) -> Iterator[DirectoryRecord]:
        cursor = self.connection.execute(
            "SELECT * FROM dirs WHERE scan_id=? ORDER BY length(rel_path), rel_path", (scan_id,)
        )
        for row in cursor:
            yield DirectoryRecord(
                path=row["path"],
                rel_path=row["rel_path"],
                size=row["size"],
                file_count=row["file_count"],
                failed_count=row["failed_count"],
                skipped_count=row["skipped_count"],
                status=row["status"],
            )

    def get_directory_size(self, scan_id: str, rel_path: str) -> Optional[int]:
        rel_path = rel_path.replace("\\", "/").strip("/")
        row = self.connection.execute(
            "SELECT size FROM dirs WHERE scan_id=? AND rel_path=? COLLATE NOCASE LIMIT 1",
            (scan_id, rel_path),
        ).fetchone()
        return int(row["size"]) if row else None

    def count_failed(self, scan_id: str) -> int:
        return self.count_entries(scan_id, status="failed")

    def failed_entries(self, scan_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT rel_path, path, kind, reason FROM entries WHERE scan_id=? AND status='failed' "
            "ORDER BY id LIMIT ?",
            (scan_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ 观察目标
    def upsert_watch_target(
        self, rel_path: str, threshold_bytes: int, baseline_bytes: int, created_at: str
    ) -> None:
        with self._lock, self.connection as conn:
            conn.execute(
                "INSERT INTO watch_targets(rel_path, threshold_bytes, baseline_bytes, created_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(rel_path) DO UPDATE SET threshold_bytes=excluded.threshold_bytes, "
                "baseline_bytes=excluded.baseline_bytes, created_at=excluded.created_at",
                (rel_path, int(threshold_bytes), int(baseline_bytes), created_at),
            )

    def list_watch_targets(self) -> list[WatchTarget]:
        rows = self.connection.execute(
            "SELECT * FROM watch_targets ORDER BY rel_path"
        ).fetchall()
        return [
            WatchTarget(
                rel_path=row["rel_path"],
                threshold_bytes=row["threshold_bytes"],
                baseline_bytes=row["baseline_bytes"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------ 导出
    def scan_summary(self, scan_id: str, top: int = 20) -> dict[str, Any]:
        scan = self.get_scan(scan_id)
        if scan is None:
            raise ValueError(f"扫描不存在: {scan_id}")
        return {
            "scan": scan.to_dict(),
            "extensions": self.extension_stats(scan_id, limit=top),
            "largest_files": self.largest_files(scan_id, limit=top),
            "largest_dirs": self.largest_dirs(scan_id, limit=top),
            "failed_entries": self.failed_entries(scan_id, limit=top),
        }

    def export_json(self, scan_id: str, out_path: str | os.PathLike[str], top: int = 20) -> Path:
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.scan_summary(scan_id, top=top)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        return path

    def export_entries_csv(self, scan_id: str, out_path: str | os.PathLike[str]) -> Path:
        path = Path(out_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["rel_path", "path", "name", "suffix", "size", "mtime_ns", "kind", "status", "reason", "volatile", "sha256"]
            )
            for entry in self.iter_entries(scan_id):
                writer.writerow(
                    [
                        entry.rel_path,
                        entry.path,
                        entry.name,
                        entry.suffix,
                        "" if entry.size is None else entry.size,
                        entry.mtime_ns,
                        entry.kind,
                        entry.status,
                        entry.reason,
                        1 if entry.volatile else 0,
                        entry.sha256 or "",
                    ]
                )
        return path

    def integrity_ok(self) -> bool:
        return self.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
