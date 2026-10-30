"""索引调度：多根目录、增量扫描、增删改移识别、暂停/继续/取消、重建与完整性检查。

安全约束：只读取源文件；符号链接默认不跟随，跟随也必须位于授权根目录内。
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Sequence

from .content_chunker import (
    Chunk,
    Symbol,
    chunk_pages,
    chunk_text,
    extract_symbols,
    metadata_chunk,
)
from .document_parser import DEFAULT_MAX_BYTES, DEFAULT_MAX_PDF_PAGES, parse_document
from .filesystem_watcher import (
    FileCandidate,
    FilesystemWatcher,
    ScanRules,
    hash_file,
    normalize_path,
)
from .fulltext_index import IndexDatabase


class IndexCancelled(RuntimeError):
    """用户取消了索引任务。"""


@dataclass
class IndexStats:
    roots: int = 0
    files_scanned: int = 0
    added: int = 0
    modified: int = 0
    deleted: int = 0
    moved: int = 0
    unchanged: int = 0
    skipped: int = 0
    skipped_reasons: dict[str, int] = field(default_factory=dict)
    parse_errors: int = 0
    unsupported: int = 0
    incomplete: int = 0
    chunks: int = 0
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False
    duration_seconds: float = 0.0
    started_at: float = 0.0
    finished_at: float = 0.0

    def merge_skip(self, reasons: dict[str, int]) -> None:
        for reason, count in reasons.items():
            self.skipped_reasons[reason] = self.skipped_reasons.get(reason, 0) + count

    def to_dict(self) -> dict[str, Any]:
        return {
            "roots": self.roots,
            "files_scanned": self.files_scanned,
            "added": self.added,
            "modified": self.modified,
            "deleted": self.deleted,
            "moved": self.moved,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
            "skipped_reasons": dict(self.skipped_reasons),
            "parse_errors": self.parse_errors,
            "unsupported": self.unsupported,
            "incomplete": self.incomplete,
            "chunks": self.chunks,
            "errors": list(self.errors),
            "cancelled": self.cancelled,
            "duration_seconds": round(self.duration_seconds, 3),
        }


@dataclass
class RebuildStats:
    roots: int = 0
    removed_files: int = 0
    index: IndexStats | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "roots": self.roots,
            "removed_files": self.removed_files,
            "index": self.index.to_dict() if self.index else None,
        }


class IndexScheduler:
    def __init__(
        self,
        db: IndexDatabase,
        *,
        rules: ScanRules | None = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_pdf_pages: int = DEFAULT_MAX_PDF_PAGES,
        hash_limit: int = 16 * 1024 * 1024,
        chunk_max_lines: int = 40,
        chunk_max_chars: int = 1200,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.db = db
        self.rules = rules or ScanRules()
        db_path = str(db.path)
        extra_excluded = (db_path, db_path + "-wal", db_path + "-shm")
        existing = tuple(self.rules.excluded_paths)
        self.rules = replace(
            self.rules, excluded_paths=tuple(dict.fromkeys(existing + extra_excluded))
        )
        self.watcher = FilesystemWatcher(self.rules)
        self.max_bytes = max_bytes
        self.max_pdf_pages = max_pdf_pages
        self.hash_limit = hash_limit
        self.chunk_max_lines = chunk_max_lines
        self.chunk_max_chars = chunk_max_chars
        self.on_event = on_event
        self._pause_event = threading.Event()
        self._cancel_event = threading.Event()
        self._status_lock = threading.Lock()
        self._status: dict[str, Any] = {
            "state": "idle",
            "current_file": None,
            "processed": 0,
            "estimated_total": 0,
            "message": "",
            "last_stats": None,
        }
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # 状态 / 生命周期
    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        with self._status_lock:
            return dict(self._status)

    def _set_status(self, **values: Any) -> None:
        with self._status_lock:
            self._status.update(values)
        self._emit("status", self.status())

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self.on_event:
            try:
                self.on_event(kind, payload)
            except Exception:
                pass

    def pause(self) -> None:
        self._pause_event.set()
        if self.is_running():
            self._set_status(state="paused", message="已暂停")

    def resume(self) -> None:
        self._pause_event.clear()
        if self.is_running():
            self._set_status(state="running", message="已继续")

    def cancel(self) -> None:
        self._cancel_event.set()
        self._pause_event.clear()
        if self.is_running():
            self._set_status(state="cancelling", message="正在取消")

    def is_paused(self) -> bool:
        return self._pause_event.is_set()

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def wait(self, timeout: float | None = None) -> bool:
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _checkpoint(self) -> None:
        was_paused = False
        while self._pause_event.is_set():
            if self._cancel_event.is_set():
                raise IndexCancelled("索引已取消")
            was_paused = True
            self._set_status(state="paused")
            time.sleep(0.03)
        if self._cancel_event.is_set():
            raise IndexCancelled("索引已取消")
        if was_paused:
            self._set_status(state="running", message="已继续")

    def start_async(self, roots: Sequence[str] | None = None) -> threading.Thread:
        if self.is_running():
            raise RuntimeError("索引任务已在运行")
        self._cancel_event.clear()
        self._pause_event.clear()
        self._set_status(state="running", current_file=None, processed=0, estimated_total=0, message="开始索引")
        self._thread = threading.Thread(target=self._run_async, args=(roots,), name="recallfs-index", daemon=True)
        self._thread.start()
        return self._thread

    def _run_async(self, roots: Sequence[str] | None) -> None:
        try:
            stats = self.scan(roots)
            if stats.cancelled:
                self._set_status(state="cancelled", message="索引已取消", last_stats=stats.to_dict())
            else:
                self._set_status(state="completed", message="索引完成", last_stats=stats.to_dict())
        except IndexCancelled:
            self._set_status(state="cancelled", message="索引已取消")
        except Exception as exc:  # pragma: no cover - 防御性
            self._set_status(state="error", message=f"索引失败: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # 根目录
    # ------------------------------------------------------------------
    def add_root(self, path: str | os.PathLike[str], label: str | None = None) -> int:
        normalized = normalize_path(path)
        if not os.path.isdir(normalized):
            raise NotADirectoryError(f"根目录不存在或不是目录: {normalized}")
        return self.db.add_root(normalized, label)

    def _resolve_roots(self, roots: Sequence[str] | None) -> list[Any]:
        if roots is None:
            return self.db.list_roots()
        resolved = []
        for root in roots:
            normalized = normalize_path(root)
            row = self.db.get_root(normalized)
            if row is None:
                if not os.path.isdir(normalized):
                    raise NotADirectoryError(f"根目录不存在或不是目录: {normalized}")
                self.db.add_root(normalized)
                row = self.db.get_root(normalized)
            if row is not None:
                resolved.append(row)
        return resolved

    # ------------------------------------------------------------------
    # 扫描
    # ------------------------------------------------------------------
    def scan(self, roots: Sequence[str] | None = None) -> IndexStats:
        self._cancel_event.clear()
        self._set_status(state="running", current_file=None, processed=0, message="开始索引")
        stats = IndexStats(started_at=time.time())
        try:
            root_rows = self._resolve_roots(roots)
            stats.roots = len(root_rows)
            processed = 0
            for root_row in root_rows:
                self._checkpoint()
                root_path = root_row["path"]
                self._set_status(current_file=root_path, message=f"扫描 {root_path}")
                snapshot = self.watcher.snapshot(root_path)
                stats.files_scanned += len(snapshot.files)
                stats.skipped += snapshot.skipped
                stats.merge_skip(snapshot.skipped_reasons)
                for error in snapshot.errors:
                    stats.errors.append(error)
                self._set_status(estimated_total=stats.files_scanned, message=f"同步 {root_path}")
                processed = self._sync_root(root_row, snapshot, stats, processed)
        except IndexCancelled:
            stats.cancelled = True
        finally:
            stats.finished_at = time.time()
            stats.duration_seconds = stats.finished_at - stats.started_at
            self.db.touch_index_time()
            self._set_status(
                current_file=None,
                last_stats=stats.to_dict(),
                state="cancelled" if stats.cancelled else "completed",
                message="索引已取消" if stats.cancelled else "索引完成",
            )
            self._emit("finished", stats.to_dict())
        return stats

    def _sync_root(self, root_row: Any, snapshot: Any, stats: IndexStats, processed: int) -> int:
        root_id = int(root_row["id"])
        db_files = {row["path_key"]: row for row in self.db.files_for_root(root_id)}
        seen: set[str] = set()
        pending_new: list[FileCandidate] = []
        for key, candidate in snapshot.files.items():
            self._checkpoint()
            seen.add(key)
            existing = db_files.get(key)
            if existing is None:
                pending_new.append(candidate)
                continue
            if self._is_unchanged(existing, candidate):
                stats.unchanged += 1
                continue
            self._set_status(current_file=candidate.path, processed=processed)
            content_hash, hash_status = self._file_hash(candidate.path, candidate.size)
            self._index_candidate(candidate, existing, stats, content_hash, hash_status, root_id)
            stats.modified += 1
            processed += 1
            self._set_status(processed=processed)

        deleted_rows = [row for key, row in db_files.items() if key not in seen]

        # 移动识别：size 相同 + 哈希相同
        deleted_by_size: dict[int, list[Any]] = {}
        for row in deleted_rows:
            deleted_by_size.setdefault(int(row["size"]), []).append(row)
        moved_ids: set[int] = set()
        for candidate in pending_new:
            self._checkpoint()
            self._set_status(current_file=candidate.path, processed=processed)
            content_hash, hash_status = self._file_hash(candidate.path, candidate.size)
            match = None
            if deleted_by_size.get(candidate.size):
                for row in deleted_by_size[candidate.size]:
                    if int(row["id"]) in moved_ids:
                        continue
                    if row["content_hash"] and row["content_hash"] == content_hash:
                        if (row["hash_status"] or "none") == hash_status:
                            match = row
                            break
            if match is not None:
                self._move_file(match, candidate)
                moved_ids.add(int(match["id"]))
                stats.moved += 1
            else:
                self._index_candidate(candidate, None, stats, content_hash, hash_status, root_id)
                stats.added += 1
            processed += 1
            self._set_status(processed=processed)

        for row in deleted_rows:
            if int(row["id"]) in moved_ids:
                continue
            self.db.delete_file(int(row["id"]))
            stats.deleted += 1
        return processed

    @staticmethod
    def _is_unchanged(row: Any, candidate: FileCandidate) -> bool:
        if row["size"] != candidate.size:
            return False
        if abs(float(row["mtime"]) - candidate.mtime) > 1e-6:
            return False
        return row["parse_status"] not in ("pending", "incomplete")

    def _file_hash(self, path: str, size: int) -> tuple[str | None, str]:
        try:
            return hash_file(path, self.hash_limit)
        except OSError:
            return None, "none"

    def _move_file(self, row: Any, candidate: FileCandidate) -> None:
        name = candidate.name
        self.db.update_file(
            int(row["id"]),
            {
                "path": candidate.path,
                "path_key": candidate.key,
                "rel_path": candidate.rel_path,
                "name": name,
                "name_key": name.casefold(),
                "ext": candidate.ext,
                "size": candidate.size,
                "mtime": candidate.mtime,
                "updated_at": time.time(),
            },
        )
        self.db.update_fts_location(int(row["id"]), name, candidate.rel_path)

    # ------------------------------------------------------------------
    # 单文件索引
    # ------------------------------------------------------------------
    def _index_candidate(
        self,
        candidate: FileCandidate,
        existing: Any,
        stats: IndexStats,
        content_hash: str | None,
        hash_status: str,
        root_id: int,
    ) -> None:
        parse_result = None
        attempts = 0
        while True:
            self._checkpoint()
            try:
                before = os.stat(candidate.path)
            except OSError as exc:
                stats.errors.append(f"{candidate.path}: {exc}")
                return
            parse_result = parse_document(
                candidate.path, max_bytes=self.max_bytes, max_pdf_pages=self.max_pdf_pages
            )
            try:
                after = os.stat(candidate.path)
            except OSError as exc:
                stats.errors.append(f"{candidate.path}: {exc}")
                return
            changed = after.st_size != before.st_size or abs(after.st_mtime - before.st_mtime) > 1e-6
            if not changed:
                break
            attempts += 1
            if attempts >= 2:
                parse_result.status = "incomplete"
                parse_result.error = (
                    (parse_result.error + "；" if parse_result.error else "")
                    + "文件在索引过程中持续变化，已按读取到的内容标记为 incomplete"
                )
                break
            time.sleep(0.05)

        if parse_result is None:  # pragma: no cover - 理论不可达
            return

        size = after.st_size
        mtime = after.st_mtime

        symbols: list[Symbol] = []
        chunks: list[Chunk] = []
        if parse_result.status == "ok" and parse_result.text:
            ext = candidate.ext
            symbols = extract_symbols(parse_result.text, ext) if self._is_code(ext) else []
            pages = list(parse_result.pages)
            has_pages = any(page is not None for page, _text in pages)
            if has_pages:
                chunks = chunk_pages(
                    pages, max_lines=self.chunk_max_lines, max_chars=self.chunk_max_chars
                )
            else:
                chunks = chunk_text(
                    parse_result.text,
                    max_lines=self.chunk_max_lines,
                    max_chars=self.chunk_max_chars,
                    symbols=symbols,
                )

        metadata = metadata_chunk(
            name=candidate.name,
            rel_path=candidate.rel_path,
            ext=candidate.ext,
            size=size,
            mtime=mtime,
            parse_status=parse_result.status,
            parser=parse_result.parser,
            parse_error=parse_result.error,
            truncated=parse_result.truncated,
        )
        chunks = [metadata] + chunks

        values = {
            "root_id": root_id,
            "path": candidate.path,
            "path_key": candidate.key,
            "rel_path": candidate.rel_path,
            "name": candidate.name,
            "name_key": candidate.name.casefold(),
            "ext": candidate.ext,
            "size": size,
            "mtime": mtime,
            "content_hash": content_hash,
            "hash_status": hash_status,
            "parse_status": parse_result.status,
            "parse_error": parse_result.error,
            "parser": parse_result.parser,
            "truncated": int(parse_result.truncated),
            "kind": parse_result.kind,
            "indexed_at": time.time(),
            "updated_at": time.time(),
        }
        file_id = int(existing["id"]) if existing is not None else None
        self.db.save_file(values, chunks, symbols, file_id=file_id)
        stats.chunks += len(chunks)
        if parse_result.status == "error":
            stats.parse_errors += 1
        elif parse_result.status == "unsupported":
            stats.unsupported += 1
        elif parse_result.status == "incomplete":
            stats.incomplete += 1
        self._emit(
            "file_indexed",
            {
                "path": candidate.path,
                "status": parse_result.status,
                "chunks": len(chunks),
                "error": parse_result.error,
            },
        )

    @staticmethod
    def _is_code(ext: str) -> bool:
        return ext.casefold() in {
            ".py",
            ".pyi",
            ".js",
            ".jsx",
            ".ts",
            ".tsx",
            ".java",
            ".c",
            ".h",
            ".cpp",
            ".cc",
            ".cxx",
            ".hpp",
            ".sql",
            ".sh",
            ".bash",
            ".md",
        }

    # ------------------------------------------------------------------
    # 维护操作
    # ------------------------------------------------------------------
    def reindex_file(self, path: str | os.PathLike[str]) -> dict[str, Any] | None:
        """强制重新解析单个文件（用于解析错误重试）。"""
        normalized = normalize_path(path)
        row = self.db.get_file_by_path(normalized)
        if row is None:
            return None
        stat = os.stat(normalized)
        candidate = FileCandidate(
            path=normalized,
            rel_path=row["rel_path"],
            name=row["name"],
            ext=row["ext"],
            size=stat.st_size,
            mtime=stat.st_mtime,
            root=normalized,
        )
        stats = IndexStats()
        content_hash, hash_status = self._file_hash(normalized, candidate.size)
        self._index_candidate(candidate, row, stats, content_hash, hash_status, int(row["root_id"] or 0))
        return stats.to_dict()

    def retry_failed(self, limit: int | None = None) -> dict[str, Any]:
        rows = self.db.query(
            "SELECT * FROM files WHERE parse_status IN ('error','unsupported','incomplete') ORDER BY id"
        )
        if limit is not None:
            rows = rows[:limit]
        attempted = 0
        changed: list[dict[str, Any]] = []
        for row in rows:
            self._checkpoint()
            if not os.path.exists(row["path"]):
                continue
            before = row["parse_status"]
            result = self.reindex_file(row["path"])
            attempted += 1
            if result:
                after = self.db.get_file(int(row["id"]))
                changed.append(
                    {
                        "path": row["path"],
                        "before": before,
                        "after": after["parse_status"] if after else "missing",
                    }
                )
        return {"attempted": attempted, "results": changed}

    def clear_root(self, root_path: str | os.PathLike[str]) -> int:
        root = self.db.get_root(root_path)
        if root is None:
            return 0
        return self.db.delete_files_in_root(int(root["id"]))

    def clear_all(self) -> None:
        self.db.drop_all_index_data()

    def rebuild(self, roots: Sequence[str] | None = None) -> RebuildStats:
        """删除索引后全量重建（不触碰源文件）。"""
        root_rows = self._resolve_roots(roots)
        result = RebuildStats(roots=len(root_rows), removed_files=0)
        for row in root_rows:
            result.removed_files += self.db.delete_files_in_root(int(row["id"]))
        self.db.set_meta("last_rebuild_at", str(time.time()))
        result.index = self.scan([row["path"] for row in root_rows])
        return result

    def verify_integrity(self) -> dict[str, Any]:
        report = self.db.integrity_check()
        missing: list[str] = []
        changed: list[str] = []
        for row in self.db.all_files():
            try:
                stat = os.stat(row["path"])
            except OSError:
                missing.append(row["path"])
                continue
            if stat.st_size != row["size"] or abs(stat.st_mtime - row["mtime"]) > 1e-6:
                changed.append(row["path"])
        payload = report.to_dict()
        payload["files_missing_on_disk"] = len(missing)
        payload["files_changed_on_disk"] = len(changed)
        payload["missing_examples"] = missing[:10]
        payload["changed_examples"] = changed[:10]
        payload["ok"] = report.ok and not missing and not changed
        return payload
