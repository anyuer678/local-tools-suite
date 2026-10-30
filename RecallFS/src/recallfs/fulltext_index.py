"""SQLite / FTS5 索引存储层。

设计要点
--------
* 单一连接 + RLock，所有读写串行化，索引线程与 GUI 查询线程安全共享；
* 每个“文件级”写入使用显式事务，失败不影响已有索引；
* 库中记录 schema_version（索引版本），版本不匹配时拒绝打开并提示重建；
* FTS5 使用 unicode61；中文通过“单字 + 双字 bigram”展开实现子串检索；
* 删除索引只删除 SQLite 中的记录，绝不触碰原始文件。
"""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .filesystem_watcher import normalize_path, path_key

SCHEMA_VERSION = 3
DB_FILENAME = "recallfs.sqlite3"


def default_db_path() -> Path:
    home = os.environ.get("RECALLFS_HOME")
    base = Path(home) if home else Path.home() / ".recallfs"
    return base / DB_FILENAME


class IndexVersionError(RuntimeError):
    """索引库版本与当前程序不一致。"""


_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]+")


def expand_for_fts(text: str) -> str:
    """把中文文本展开为 单字 + 双字 bigram，交给 unicode61 分词。

    "中文检索" -> "中 中文 文 文检 检 检索 索"
    这样任意 >=2 字的中文子串查询都可以用短语匹配，单字查询也能命中。
    """
    if not text:
        return ""
    out: list[str] = []
    pos = 0
    for match in _CJK_RE.finditer(text):
        out.append(text[pos : match.start()])
        run = match.group(0)
        tokens: list[str] = []
        for index, char in enumerate(run):
            tokens.append(char)
            if index + 1 < len(run):
                tokens.append(run[index : index + 2])
        out.append(" " + " ".join(tokens) + " ")
        pos = match.end()
    out.append(text[pos:])
    return re.sub(r"\s+", " ", "".join(out)).strip()


def _now() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# 数据库
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS roots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    path_key TEXT NOT NULL UNIQUE,
    label TEXT,
    added_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_id INTEGER REFERENCES roots(id) ON DELETE SET NULL,
    path TEXT NOT NULL,
    path_key TEXT NOT NULL UNIQUE,
    rel_path TEXT NOT NULL,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    ext TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    content_hash TEXT,
    hash_status TEXT NOT NULL DEFAULT 'none',
    parse_status TEXT NOT NULL DEFAULT 'pending',
    parse_error TEXT,
    parser TEXT,
    truncated INTEGER NOT NULL DEFAULT 0,
    kind TEXT,
    indexed_at REAL,
    updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_files_root ON files(root_id);
CREATE INDEX IF NOT EXISTS idx_files_name_key ON files(name_key);
CREATE INDEX IF NOT EXISTS idx_files_ext ON files(ext);
CREATE INDEX IF NOT EXISTS idx_files_mtime ON files(mtime);
CREATE INDEX IF NOT EXISTS idx_files_size ON files(size);
CREATE INDEX IF NOT EXISTS idx_files_hash ON files(content_hash);
CREATE INDEX IF NOT EXISTS idx_files_status ON files(parse_status);

CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL,
    start_line INTEGER,
    end_line INTEGER,
    page INTEGER,
    text TEXT NOT NULL,
    symbol TEXT,
    char_count INTEGER NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    body,
    name,
    path,
    symbols,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    chunk_id INTEGER REFERENCES chunks(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    symbol_kind TEXT,
    line INTEGER
);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_id);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name_key);

CREATE TABLE IF NOT EXISTS collections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'custom',
    description TEXT DEFAULT '',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS collection_roots (
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    root_id INTEGER NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
    PRIMARY KEY (collection_id, root_id)
);

CREATE TABLE IF NOT EXISTS collection_files (
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    PRIMARY KEY (collection_id, file_id)
);

CREATE TABLE IF NOT EXISTS favorites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fav_type TEXT NOT NULL,
    file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
    chunk_id INTEGER REFERENCES chunks(id) ON DELETE SET NULL,
    query_text TEXT,
    label TEXT,
    note TEXT DEFAULT '',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS recent_searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    filters_json TEXT DEFAULT '{}',
    result_count INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS semantic_vectors (
    chunk_id INTEGER PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    version TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL,
    norm REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_semantic_model ON semantic_vectors(model);
"""

FILE_FIELDS = {
    "root_id",
    "path",
    "path_key",
    "rel_path",
    "name",
    "name_key",
    "ext",
    "size",
    "mtime",
    "content_hash",
    "hash_status",
    "parse_status",
    "parse_error",
    "parser",
    "truncated",
    "kind",
    "indexed_at",
    "updated_at",
}


@dataclass
class IntegrityReport:
    ok: bool
    checks: dict[str, bool] = field(default_factory=dict)
    details: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": self.checks,
            "details": self.details,
            "stats": self.stats,
        }


class IndexDatabase:
    def __init__(self, path: str | os.PathLike[str] | None = None, *, allow_version_reset: bool = False) -> None:
        self.path = Path(path) if path is not None else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), timeout=15.0, check_same_thread=False, isolation_level=None)
        self._closed = False
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._initialize(allow_version_reset=allow_version_reset)

    # -- 生命周期 ---------------------------------------------------------
    @property
    def connection(self) -> sqlite3.Connection:
        return self._conn

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def __enter__(self) -> "IndexDatabase":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # 不在 __del__ 中关闭 SQLite 连接：解释器退出或 GC 期间获取锁并关闭 C 资源
    # 曾在 Windows 上触发访问违例。所有调用方必须显式 close 或使用 with。

    def _initialize(self, *, allow_version_reset: bool) -> None:
        with self._lock:
            existing_version = None
            has_meta = self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
            ).fetchone()
            if has_meta:
                row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                existing_version = row["value"] if row else None
            if existing_version is not None and existing_version != str(SCHEMA_VERSION):
                if not allow_version_reset:
                    raise IndexVersionError(
                        f"索引库版本 {existing_version} 与程序版本 {SCHEMA_VERSION} 不一致；"
                        "请使用 rebuild --reset 或在新建库上重建索引。"
                    )
                self._conn.executescript(
                    "".join(
                        f"DROP TABLE IF EXISTS {name};"
                        for name in (
                            "chunks_fts",
                            "semantic_vectors",
                            "symbols",
                            "chunks",
                            "collection_files",
                            "collection_roots",
                            "collections",
                            "favorites",
                            "recent_searches",
                            "files",
                            "roots",
                            "meta",
                        )
                    )
                )
            self._conn.executescript(_SCHEMA)
            with self.transaction() as conn:
                conn.execute(
                    "INSERT INTO meta(key, value) VALUES ('schema_version', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(SCHEMA_VERSION),),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES ('created_at', ?)", (str(_now()),)
                )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # -- 基础查询 ---------------------------------------------------------
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)).fetchall())

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self._lock:
            self._conn.execute(sql, tuple(params))

    def scalar(self, sql: str, params: Sequence[Any] = ()) -> Any:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
        return None if row is None else row[0]

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else default

    @property
    def schema_version(self) -> int:
        return int(self.get_meta("schema_version", "0") or 0)

    # -- 根目录 -----------------------------------------------------------
    def add_root(self, path: str | os.PathLike[str], label: str | None = None) -> int:
        normalized = normalize_path(path)
        key = path_key(normalized)
        existing = self.query_one("SELECT id FROM roots WHERE path_key=?", (key,))
        if existing:
            if label:
                self.execute("UPDATE roots SET label=? WHERE id=?", (label, existing["id"]))
            return int(existing["id"])
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO roots(path, path_key, label, added_at) VALUES (?,?,?,?)",
                (normalized, key, label, _now()),
            )
            root_id = int(cursor.lastrowid)
        return root_id

    def list_roots(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM roots ORDER BY path_key")

    def get_root(self, path: str | os.PathLike[str]) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM roots WHERE path_key=?", (path_key(path),))

    def remove_root(self, path: str | os.PathLike[str], delete_files: bool = True) -> int:
        root = self.get_root(path)
        if not root:
            return 0
        removed = 0
        if delete_files:
            removed = self.delete_files_in_root(int(root["id"]))
        with self.transaction() as conn:
            conn.execute("DELETE FROM roots WHERE id=?", (root["id"],))
        return removed

    # -- 文件 -------------------------------------------------------------
    def get_file(self, file_id: int) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM files WHERE id=?", (file_id,))

    def get_file_by_path(self, path: str | os.PathLike[str]) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM files WHERE path_key=?", (path_key(path),))

    def files_for_root(self, root_id: int) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM files WHERE root_id=?", (root_id,))

    def all_files(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM files ORDER BY path_key")

    def file_count(self) -> int:
        return int(self.scalar("SELECT COUNT(*) FROM files") or 0)

    def chunks_for_file(self, file_id: int) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM chunks WHERE file_id=? ORDER BY ordinal", (file_id,))

    def insert_file(self, values: dict[str, Any]) -> int:
        payload = {k: v for k, v in values.items() if k in FILE_FIELDS}
        payload.setdefault("name_key", str(payload.get("name", "")).casefold())
        columns = ", ".join(payload)
        placeholders = ", ".join("?" for _ in payload)
        with self.transaction() as conn:
            cursor = conn.execute(f"INSERT INTO files({columns}) VALUES ({placeholders})", tuple(payload.values()))
            return int(cursor.lastrowid)

    def update_file(self, file_id: int, values: dict[str, Any]) -> None:
        payload = {k: v for k, v in values.items() if k in FILE_FIELDS}
        if not payload:
            return
        assignments = ", ".join(f"{key}=?" for key in payload)
        with self.transaction() as conn:
            conn.execute(f"UPDATE files SET {assignments} WHERE id=?", (*payload.values(), file_id))

    def touch_index_time(self) -> None:
        self.set_meta("last_index_at", str(_now()))

    def _delete_chunks(self, conn: sqlite3.Connection, file_id: int) -> None:
        rows = conn.execute("SELECT id FROM chunks WHERE file_id=?", (file_id,)).fetchall()
        for row in rows:
            conn.execute("DELETE FROM chunks_fts WHERE rowid=?", (int(row["id"]),))
        conn.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))
        conn.execute("DELETE FROM symbols WHERE file_id=?", (file_id,))

    def delete_file(self, file_id: int) -> None:
        with self.transaction() as conn:
            self._delete_chunks(conn, file_id)
            conn.execute("DELETE FROM files WHERE id=?", (file_id,))

    def delete_file_by_path(self, path: str | os.PathLike[str]) -> bool:
        row = self.get_file_by_path(path)
        if row is None:
            return False
        self.delete_file(int(row["id"]))
        return True

    def delete_files_in_root(self, root_id: int) -> int:
        rows = self.query("SELECT id FROM files WHERE root_id=?", (root_id,))
        with self.transaction() as conn:
            for row in rows:
                self._delete_chunks(conn, int(row["id"]))
                conn.execute("DELETE FROM files WHERE id=?", (int(row["id"]),))
        return len(rows)

    def _write_chunks(
        self,
        conn: sqlite3.Connection,
        file_id: int,
        file_name: str,
        file_rel_path: str,
        chunks: Iterable[Any],
        symbols: Iterable[Any],
    ) -> int:
        chunk_list = list(chunks)
        symbol_list = list(symbols)
        name_fts = expand_for_fts(file_name)
        path_fts = expand_for_fts(file_rel_path)
        self._delete_chunks(conn, file_id)
        chunk_ranges: list[tuple[int, int | None, int | None, int | None]] = []
        for ordinal, chunk in enumerate(chunk_list):
            cursor = conn.execute(
                "INSERT INTO chunks(file_id, ordinal, kind, start_line, end_line, page, text, symbol, char_count, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    file_id,
                    ordinal,
                    chunk.kind,
                    chunk.start_line,
                    chunk.end_line,
                    chunk.page,
                    chunk.text,
                    chunk.symbol,
                    len(chunk.text),
                    _now(),
                ),
            )
            chunk_id = int(cursor.lastrowid)
            conn.execute(
                "INSERT INTO chunks_fts(rowid, body, name, path, symbols) VALUES (?,?,?,?,?)",
                (chunk_id, expand_for_fts(chunk.text), name_fts, path_fts, expand_for_fts(chunk.symbol or "")),
            )
            chunk_ranges.append((chunk_id, chunk.start_line, chunk.end_line, chunk.page))
        for symbol in symbol_list:
            chunk_id = self._find_chunk_for_line(chunk_ranges, getattr(symbol, "line", None))
            conn.execute(
                "INSERT INTO symbols(file_id, chunk_id, name, name_key, symbol_kind, line) VALUES (?,?,?,?,?,?)",
                (
                    file_id,
                    chunk_id,
                    symbol.name,
                    symbol.name.casefold(),
                    getattr(symbol, "kind", None),
                    getattr(symbol, "line", None),
                ),
            )
        return len(chunk_list)

    def replace_chunks(
        self,
        file_id: int,
        file_name: str,
        file_rel_path: str,
        chunks: Iterable[Any],
        symbols: Iterable[Any] = (),
    ) -> int:
        """原子替换某文件的所有 chunk / FTS 行 / 符号。返回写入的 chunk 数量。"""
        with self.transaction() as conn:
            return self._write_chunks(conn, file_id, file_name, file_rel_path, chunks, symbols)

    def save_file(
        self,
        values: dict[str, Any],
        chunks: Iterable[Any],
        symbols: Iterable[Any] = (),
        *,
        file_id: int | None = None,
    ) -> int:
        """在一个事务中写入文件记录 + chunk + FTS + 符号，避免半成品状态。"""
        payload = {k: v for k, v in values.items() if k in FILE_FIELDS}
        payload.setdefault("name_key", str(payload.get("name", "")).casefold())
        with self.transaction() as conn:
            if file_id is None:
                columns = ", ".join(payload)
                placeholders = ", ".join("?" for _ in payload)
                cursor = conn.execute(f"INSERT INTO files({columns}) VALUES ({placeholders})", tuple(payload.values()))
                file_id = int(cursor.lastrowid)
            else:
                if payload:
                    assignments = ", ".join(f"{key}=?" for key in payload)
                    conn.execute(f"UPDATE files SET {assignments} WHERE id=?", (*payload.values(), file_id))
            self._write_chunks(
                conn,
                file_id,
                str(payload.get("name", "")),
                str(payload.get("rel_path", "")),
                chunks,
                symbols,
            )
        return file_id

    @staticmethod
    def _find_chunk_for_line(
        ranges: Sequence[tuple[int, int | None, int | None, int | None]],
        line: int | None,
    ) -> int | None:
        if line is None:
            return ranges[0][0] if ranges else None
        for chunk_id, start, end, _page in ranges:
            if start is not None and end is not None and start <= line <= end:
                return chunk_id
        return ranges[0][0] if ranges else None

    def update_fts_location(self, file_id: int, file_name: str, file_rel_path: str) -> None:
        """文件移动/改名后，同步 FTS 中的 name/path 列（chunk 内容保持不变）。"""
        name_fts = expand_for_fts(file_name)
        path_fts = expand_for_fts(file_rel_path)
        with self.transaction() as conn:
            rows = conn.execute("SELECT id FROM chunks WHERE file_id=?", (file_id,)).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE chunks_fts SET name=?, path=? WHERE rowid=?",
                    (name_fts, path_fts, int(row["id"])),
                )

    # -- 统计 / 完整性 ----------------------------------------------------
    def stats(self) -> dict[str, Any]:
        status_rows = self.query("SELECT parse_status, COUNT(*) AS n FROM files GROUP BY parse_status")
        return {
            "database": str(self.path),
            "db_size_bytes": self.db_size_bytes(),
            "roots": int(self.scalar("SELECT COUNT(*) FROM roots") or 0),
            "files": self.file_count(),
            "chunks": int(self.scalar("SELECT COUNT(*) FROM chunks") or 0),
            "symbols": int(self.scalar("SELECT COUNT(*) FROM symbols") or 0),
            "collections": int(self.scalar("SELECT COUNT(*) FROM collections") or 0),
            "favorites": int(self.scalar("SELECT COUNT(*) FROM favorites") or 0),
            "recent_searches": int(self.scalar("SELECT COUNT(*) FROM recent_searches") or 0),
            "parse_status": {row["parse_status"]: int(row["n"]) for row in status_rows},
            "schema_version": self.schema_version,
            "last_index_at": self.get_meta("last_index_at"),
        }

    def db_size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(self.path) + suffix)
            if candidate.exists():
                total += candidate.stat().st_size
        return total

    def integrity_check(self) -> IntegrityReport:
        report = IntegrityReport(ok=True)
        quick = [row[0] for row in self.query("PRAGMA quick_check")]
        checks = {
            "sqlite_quick_check": quick == ["ok"],
            "schema_version": self.schema_version == SCHEMA_VERSION,
            "chunks_without_file": False,
            "fts_without_chunk": False,
            "chunk_without_fts": False,
            "symbols_without_file": False,
        }
        checks["chunks_without_file"] = (
            int(
                self.scalar(
                    "SELECT COUNT(*) FROM chunks WHERE file_id NOT IN (SELECT id FROM files)"
                )
                or 0
            )
            == 0
        )
        checks["fts_without_chunk"] = (
            int(self.scalar("SELECT COUNT(*) FROM chunks_fts WHERE rowid NOT IN (SELECT id FROM chunks)") or 0) == 0
        )
        checks["chunk_without_fts"] = (
            int(self.scalar("SELECT COUNT(*) FROM chunks WHERE id NOT IN (SELECT rowid FROM chunks_fts)") or 0)
            == 0
        )
        checks["symbols_without_file"] = (
            int(self.scalar("SELECT COUNT(*) FROM symbols WHERE file_id NOT IN (SELECT id FROM files)") or 0)
            == 0
        )
        report.checks = checks
        report.ok = all(checks.values())
        report.details = [f"{name}: {'通过' if value else '失败'}" for name, value in checks.items()]
        if quick != ["ok"]:
            report.details.extend(quick)
        report.stats = self.stats()
        return report

    def list_stale_files(self) -> list[sqlite3.Row]:
        """磁盘上已不存在或 mtime/size 变化的文件（只读检查，不修改索引）。"""
        stale: list[sqlite3.Row] = []
        for row in self.all_files():
            try:
                stat = os.stat(row["path"])
            except OSError:
                stale.append(row)
                continue
            if stat.st_size != row["size"] or abs(stat.st_mtime - row["mtime"]) > 1e-6:
                stale.append(row)
        return stale

    def drop_all_index_data(self) -> None:
        """清空索引数据（保留根目录/收藏等配置；不触碰原文件）。"""
        with self.transaction() as conn:
            conn.execute("DELETE FROM chunks_fts")
            conn.execute("DELETE FROM semantic_vectors")
            conn.execute("DELETE FROM symbols")
            conn.execute("DELETE FROM chunks")
            conn.execute("DELETE FROM collection_files")
            conn.execute("DELETE FROM files")
