"""扫描引擎：os.scandir 递归、排除规则、后台线程友好、取消与批处理写入。

安全与正确性约定：
- 默认不跟随符号链接 / 目录联接，记录为 skipped，而不是忽略。
- 权限错误 / 路径过长 / stat 失败记录为 failed，并保留原因。
- 循环引用通过 (st_dev, st_ino) 已访问集合检测。
- 每个文件的 stat 前后各取一次，变化则标记 volatile。
- 每 batch_size 条写入 SQLite 一次，内存中不保留全部条目。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .duplicates import file_sha256
from .models import (
    DirectoryRecord,
    EntryKind,
    EntryStatus,
    FileRecord,
    ScanResult,
    ScanStats,
)
from .policy import ExcludeRules
from .snapshot_store import SnapshotStore, utc_now


class ScanError(RuntimeError):
    """扫描无法开始（根目录不存在等）。"""


@dataclass
class ScanOptions:
    rules: ExcludeRules = field(default_factory=ExcludeRules)
    follow_symlinks: bool = False
    batch_size: int = 2000
    progress_every: int = 200
    hash_files: bool = False
    hash_max_size: int = 64 * 1024 * 1024

    def to_dict(self) -> dict[str, Any]:
        return {
            "rules": self.rules.to_dict(),
            "follow_symlinks": self.follow_symlinks,
            "batch_size": self.batch_size,
            "progress_every": self.progress_every,
            "hash_files": self.hash_files,
            "hash_max_size": self.hash_max_size,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScanOptions":
        return cls(
            rules=ExcludeRules.from_dict(data.get("rules", {})),
            follow_symlinks=bool(data.get("follow_symlinks", False)),
            batch_size=int(data.get("batch_size", 2000)),
            progress_every=int(data.get("progress_every", 200)),
            hash_files=bool(data.get("hash_files", False)),
            hash_max_size=int(data.get("hash_max_size", 64 * 1024 * 1024)),
        )


ProgressCallback = Callable[[dict[str, Any]], None]


class _Frame:
    """目录遍历栈帧，保存当前目录的聚合值。"""

    __slots__ = (
        "path",
        "rel",
        "depth",
        "entries",
        "index",
        "opened",
        "size",
        "files",
        "failed",
        "skipped",
        "is_root",
    )

    def __init__(self, path: str, rel: str, depth: int, is_root: bool = False) -> None:
        self.path = path
        self.rel = rel
        self.depth = depth
        self.entries: list[os.DirEntry[str]] = []
        self.index = 0
        self.opened = False
        self.size = 0
        self.files = 0
        self.failed = 0
        self.skipped = 0
        self.is_root = is_root


def _join_rel(parent_rel: str, name: str) -> str:
    return f"{parent_rel}/{name}" if parent_rel else name


def _extended_path(path: str) -> str:
    if os.name != "nt":
        return path
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\?\\") or absolute.startswith("\\\\.\\"):
        return absolute
    if absolute.startswith("\\\\"):
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def _is_long_path_error(exc: OSError) -> bool:
    winerror = getattr(exc, "winerror", None)
    errno = getattr(exc, "errno", None)
    return winerror == 206 or errno in (36, 206)


def _open_scandir(path: str):
    try:
        return os.scandir(path)
    except OSError as exc:
        if os.name == "nt" and _is_long_path_error(exc):
            return os.scandir(_extended_path(path))
        raise


def _stat_path(path: str, follow_symlinks: bool = False) -> os.stat_result:
    try:
        return os.stat(path, follow_symlinks=follow_symlinks)
    except OSError as exc:
        if os.name == "nt" and _is_long_path_error(exc):
            return os.stat(_extended_path(path), follow_symlinks=follow_symlinks)
        raise


def _is_junction(path: str) -> bool:
    checker = getattr(os.path, "isjunction", None)
    if checker is None:
        return False
    try:
        return bool(checker(path))
    except OSError:
        return False


def _dir_key(st: os.stat_result, path: str) -> tuple[int, int]:
    if st.st_ino:
        return (st.st_dev, st.st_ino)
    return (st.st_dev, abs(hash(os.path.normcase(path))))


def _db_int(value: int) -> int:
    """Windows 的 st_dev 可能超过 SQLite INTEGER 的 63 位范围，落库前折叠。"""
    if value >= 1 << 63 or value < -(1 << 63):
        return value & ((1 << 63) - 1)
    return value


class Scanner:
    """可在线程中运行的目录扫描器。"""

    def __init__(
        self,
        root: str | os.PathLike[str],
        store: SnapshotStore,
        options: Optional[ScanOptions] = None,
        cancel_event: Optional["threading.Event"] = None,  # type: ignore[name-defined]
        progress_callback: Optional[ProgressCallback] = None,
    ) -> None:
        self.root = str(root)
        self.store = store
        self.options = options or ScanOptions()
        self.cancel_event = cancel_event
        self.progress_callback = progress_callback

        self._entry_buffer: list[FileRecord] = []
        self._dir_buffer: list[DirectoryRecord] = []
        self._processed = 0
        self._last_emit_count = 0
        self._last_emit_time = 0.0
        self._stats = ScanStats()
        self._scan_id: Optional[str] = None
        self._started_perf = 0.0
        self._current_path = ""

    # ------------------------------------------------------------------ 对外接口
    def scan(self) -> ScanResult:
        root_abs = os.path.abspath(os.path.expanduser(self.root))
        if not os.path.exists(root_abs):
            raise ScanError(f"根目录不存在: {root_abs}")
        if not os.path.isdir(root_abs):
            raise ScanError(f"根路径不是目录: {root_abs}")

        self._stats.started_at = utc_now()
        self._scan_id = self.store.create_scan(root_abs, self.options.to_dict())
        self._started_perf = time.perf_counter()
        self._stats = ScanStats(started_at=self._stats.started_at)

        visited: set[tuple[int, int]] = set()
        try:
            root_stat = os.stat(root_abs, follow_symlinks=True)
            visited.add(_dir_key(root_stat, root_abs))
        except OSError:
            root_stat = None

        stack: list[_Frame] = [_Frame(root_abs, "", 0, is_root=True)]
        try:
            while stack:
                if self._is_cancelled():
                    self._stats.cancelled = True
                    break
                frame = stack[-1]
                if not frame.opened:
                    self._open_frame(frame, stack, visited)
                    continue

                if frame.index < len(frame.entries):
                    entry = frame.entries[frame.index]
                    frame.index += 1
                    self._consume_entry(entry, frame, stack, visited)
                    self._processed += 1
                    if len(self._entry_buffer) >= self.options.batch_size:
                        self._flush()
                    self._emit_progress()
                    continue

                self._finalize_frame(frame, stack)
                self._emit_progress()

            if self._stats.cancelled:
                self._finalize_remaining(stack, visited)
        finally:
            self._flush()
            elapsed = time.perf_counter() - self._started_perf
            self._stats.finished_at = utc_now()
            self._stats.duration_seconds = elapsed
            self._stats.complete = not self._stats.cancelled and self._stats.failed == 0
            self.store.finalize_scan(self._scan_id, self._stats)
            self._emit_progress(force=True)

        return ScanResult(
            scan_id=self._scan_id,
            root=root_abs,
            stats=self._stats,
            options=self.options.to_dict(),
        )

    # ------------------------------------------------------------------ 目录
    def _open_frame(
        self,
        frame: _Frame,
        stack: list[_Frame],
        visited: set[tuple[int, int]],
    ) -> None:
        frame.opened = True
        self._current_path = frame.path
        try:
            with _open_scandir(frame.path) as iterator:
                frame.entries = list(iterator)
        except OSError as exc:
            stack.pop()
            self._record_dir_failure(frame, exc)
            if stack:
                stack[-1].failed += 1
            self._emit_progress()
            return
        self._stats.dirs_scanned += 1
        self._emit_progress()

    def _record_dir_failure(self, frame: _Frame, exc: OSError) -> None:
        reason = f"{type(exc).__name__}: {exc}"
        self._stats.failed += 1
        self._entry_buffer.append(
            FileRecord(
                path=frame.path,
                rel_path=frame.rel,
                name=os.path.basename(frame.path) or frame.path,
                suffix="",
                size=None,
                kind=EntryKind.DIR.value,
                status=EntryStatus.FAILED.value,
                reason=reason,
            )
        )
        self._dir_buffer.append(
            DirectoryRecord(
                path=frame.path,
                rel_path=frame.rel,
                size=0,
                file_count=0,
                failed_count=1,
                skipped_count=0,
                status=EntryStatus.FAILED.value,
            )
        )

    def _finalize_frame(self, frame: _Frame, stack: list[_Frame]) -> None:
        stack.pop()
        self._dir_buffer.append(
            DirectoryRecord(
                path=frame.path,
                rel_path=frame.rel,
                size=frame.size,
                file_count=frame.files,
                failed_count=frame.failed,
                skipped_count=frame.skipped,
                status=EntryStatus.SCANNED.value,
            )
        )
        if stack:
            parent = stack[-1]
            parent.size += frame.size
            parent.files += frame.files
            parent.failed += frame.failed
            parent.skipped += frame.skipped
        if len(self._dir_buffer) >= self.options.batch_size:
            self._flush()

    def _finalize_remaining(self, stack: list[_Frame], visited: set[tuple[int, int]]) -> None:
        """取消后把仍在栈中的目录以“部分结果”落库。"""
        while stack:
            frame = stack.pop()
            if not frame.opened and not frame.entries:
                continue
            self._dir_buffer.append(
                DirectoryRecord(
                    path=frame.path,
                    rel_path=frame.rel,
                    size=frame.size,
                    file_count=frame.files,
                    failed_count=frame.failed,
                    skipped_count=frame.skipped,
                    status=EntryStatus.SCANNED.value,
                )
            )
            if stack:
                stack[-1].size += frame.size
                stack[-1].files += frame.files
                stack[-1].failed += frame.failed
                stack[-1].skipped += frame.skipped
            if len(self._dir_buffer) >= self.options.batch_size:
                self._flush()

    # ------------------------------------------------------------------ 条目
    def _consume_entry(
        self,
        entry: os.DirEntry[str],
        frame: _Frame,
        stack: list[_Frame],
        visited: set[tuple[int, int]],
    ) -> None:
        self._current_path = entry.path
        try:
            is_link = entry.is_symlink() or _is_junction(entry.path)
        except OSError:
            is_link = False

        if is_link:
            reason = "symlink:not_followed"
            if self.options.follow_symlinks:
                _, reason = self._follow_link(entry, frame, stack, visited)
            self._record_skipped(entry, frame, reason=reason)
            return

        try:
            if entry.is_dir(follow_symlinks=False):
                self._handle_dir(entry, frame, stack, visited)
                return
            if entry.is_file(follow_symlinks=False):
                self._handle_file(entry, frame)
                return
        except OSError as exc:
            self._record_failure(entry, frame, exc)
            return

        self._record_skipped(entry, frame, reason="other:not_file_or_dir")

    def _follow_link(
        self,
        entry: os.DirEntry[str],
        frame: _Frame,
        stack: list[_Frame],
        visited: set[tuple[int, int]],
    ) -> tuple[bool, str]:
        """跟随符号链接到目录；返回 (是否已入栈, 记录原因)。"""
        try:
            if not entry.is_dir(follow_symlinks=True):
                return False, "symlink:file"
            target_stat = entry.stat(follow_symlinks=True)
        except OSError:
            return False, "symlink:target_unavailable"
        key = _dir_key(target_stat, entry.path)
        if key in visited:
            return False, "symlink:cycle"
        visited.add(key)
        stack.append(_Frame(entry.path, _join_rel(frame.rel, entry.name), frame.depth + 1))
        return True, "symlink:dir_followed"

    def _handle_dir(
        self,
        entry: os.DirEntry[str],
        frame: _Frame,
        stack: list[_Frame],
        visited: set[tuple[int, int]],
    ) -> None:
        if self.options.rules.should_exclude_dir(entry.name):
            self._record_skipped(entry, frame, reason="excluded:dir_name")
            return
        try:
            st = _stat_path(entry.path, follow_symlinks=False)
        except OSError as exc:
            self._record_failure(entry, frame, exc, kind=EntryKind.DIR.value)
            return
        key = _dir_key(st, entry.path)
        if key in visited:
            self._record_skipped(entry, frame, reason="cycle:inode_revisit")
            return
        visited.add(key)
        stack.append(_Frame(entry.path, _join_rel(frame.rel, entry.name), frame.depth + 1))

    def _handle_file(self, entry: os.DirEntry[str], frame: _Frame) -> None:
        try:
            st = _stat_path(entry.path, follow_symlinks=False)
        except OSError as exc:
            self._record_failure(entry, frame, exc, kind=EntryKind.FILE.value)
            return

        size = int(st.st_size)
        name = entry.name
        suffix = os.path.splitext(name)[1].lower()
        excluded, reason = self.options.rules.should_exclude_file(name, size)
        if excluded:
            self._stats.files_skipped += 1
            frame.skipped += 1
            self._entry_buffer.append(
                FileRecord(
                    path=entry.path,
                    rel_path=_join_rel(frame.rel, name),
                    name=name,
                    suffix=suffix,
                    size=size,
                    mtime_ns=st.st_mtime_ns,
                    ctime_ns=st.st_ctime_ns,
                    inode=_db_int(st.st_ino),
                    device=_db_int(st.st_dev),
                    nlink=st.st_nlink,
                    status=EntryStatus.SKIPPED.value,
                    reason=reason,
                )
            )
            return

        volatile = False
        try:
            st_after = _stat_path(entry.path, follow_symlinks=False)
            if st_after.st_size != st.st_size or st_after.st_mtime_ns != st.st_mtime_ns:
                volatile = True
        except OSError:
            volatile = True

        digest: Optional[str] = None
        if self.options.hash_files and size <= self.options.hash_max_size:
            try:
                digest = file_sha256(entry.path)
            except OSError:
                digest = None
                volatile = True

        record = FileRecord(
            path=entry.path,
            rel_path=_join_rel(frame.rel, name),
            name=name,
            suffix=suffix,
            size=size,
            mtime_ns=st.st_mtime_ns,
            ctime_ns=st.st_ctime_ns,
            inode=_db_int(st.st_ino),
            device=_db_int(st.st_dev),
            nlink=st.st_nlink,
            status=EntryStatus.SCANNED.value,
            volatile=volatile,
            sha256=digest,
        )
        self._entry_buffer.append(record)
        self._stats.files_scanned += 1
        self._stats.total_size += size
        if size == 0:
            self._stats.zero_byte += 1
        if volatile:
            self._stats.volatile += 1
        frame.size += size
        frame.files += 1

    def _record_failure(
        self,
        entry: os.DirEntry[str],
        frame: _Frame,
        exc: OSError,
        kind: str = EntryKind.FILE.value,
    ) -> None:
        self._stats.failed += 1
        frame.failed += 1
        if kind == EntryKind.FILE.value:
            self._stats.unknown_size += 1
        self._entry_buffer.append(
            FileRecord(
                path=entry.path,
                rel_path=_join_rel(frame.rel, entry.name),
                name=entry.name,
                suffix=os.path.splitext(entry.name)[1].lower(),
                size=None,
                kind=kind,
                status=EntryStatus.FAILED.value,
                reason=f"{type(exc).__name__}: {exc}",
            )
        )
        if kind == EntryKind.DIR.value:
            self._dir_buffer.append(
                DirectoryRecord(
                    path=entry.path,
                    rel_path=_join_rel(frame.rel, entry.name),
                    status=EntryStatus.FAILED.value,
                    failed_count=1,
                )
            )

    def _record_skipped(self, entry: os.DirEntry[str], frame: _Frame, reason: str) -> None:
        try:
            is_dir = entry.is_dir(follow_symlinks=False)
            is_link = entry.is_symlink() or _is_junction(entry.path)
        except OSError:
            is_dir, is_link = False, False
        if is_link:
            kind = EntryKind.SYMLINK.value
            self._stats.symlinks_skipped += 1
        elif is_dir:
            kind = EntryKind.DIR.value
            self._stats.dirs_skipped += 1
        else:
            kind = EntryKind.OTHER.value
            self._stats.files_skipped += 1
        frame.skipped += 1
        try:
            st: Optional[os.stat_result] = entry.stat(follow_symlinks=False)
        except OSError:
            st = None
        self._entry_buffer.append(
            FileRecord(
                path=entry.path,
                rel_path=_join_rel(frame.rel, entry.name),
                name=entry.name,
                suffix=os.path.splitext(entry.name)[1].lower(),
                size=int(st.st_size) if st else None,
                mtime_ns=st.st_mtime_ns if st else 0,
                ctime_ns=st.st_ctime_ns if st else 0,
                inode=_db_int(st.st_ino) if st else 0,
                device=_db_int(st.st_dev) if st else 0,
                nlink=st.st_nlink if st else 1,
                kind=kind,
                status=EntryStatus.SKIPPED.value,
                reason=reason,
            )
        )

    # ------------------------------------------------------------------ 辅助
    def _flush(self) -> None:
        if self._entry_buffer:
            self.store.insert_entries(self._scan_id, self._entry_buffer)
            self._entry_buffer = []
        if self._dir_buffer:
            self.store.insert_dirs(self._scan_id, self._dir_buffer)
            self._dir_buffer = []

    def _is_cancelled(self) -> bool:
        return self.cancel_event is not None and self.cancel_event.is_set()

    def _emit_progress(self, force: bool = False) -> None:
        if self.progress_callback is None:
            return
        now = time.perf_counter()
        if not force:
            if self._processed - self._last_emit_count < max(1, self.options.progress_every):
                if now - self._last_emit_time < 0.25:
                    return
        self._last_emit_count = self._processed
        self._last_emit_time = now
        payload = {
            "phase": "scanning",
            "scan_id": self._scan_id,
            "root": self.root,
            "current": self._current_path,
            "files_scanned": self._stats.files_scanned,
            "dirs_scanned": self._stats.dirs_scanned,
            "total_size": self._stats.total_size,
            "skipped": self._stats.skipped,
            "failed": self._stats.failed,
            "processed": self._processed,
            "cancelled": self._stats.cancelled,
        }
        try:
            self.progress_callback(payload)
        except Exception:
            # 进度回调绝不能让扫描崩溃。
            pass


def scan_directory(
    root: str | os.PathLike[str],
    store: SnapshotStore,
    options: Optional[ScanOptions] = None,
    cancel_event: Optional["threading.Event"] = None,  # type: ignore[name-defined]
    progress_callback: Optional[ProgressCallback] = None,
) -> ScanResult:
    """便捷函数：同步扫描一个目录。"""
    return Scanner(root, store, options=options, cancel_event=cancel_event, progress_callback=progress_callback).scan()
