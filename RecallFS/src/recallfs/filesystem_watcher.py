"""文件系统扫描层：目录遍历、排除规则、隐藏文件、符号链接约束、移动比对。

本模块只读文件，不修改任何被索引内容；不进行网络访问。
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

# ---------------------------------------------------------------------------
# 路径工具（其余模块复用，避免重复实现）
# ---------------------------------------------------------------------------

WINDOWS = os.name == "nt"


def normalize_path(path: str | os.PathLike[str]) -> str:
    """返回规范化的绝对路径（保留原始大小写用于展示）。"""
    return os.path.normpath(os.path.abspath(os.fspath(path)))


def path_key(path: str | os.PathLike[str]) -> str:
    """用于比较/唯一键的路径：规范化后做大小写折叠（Windows 不区分大小写）。"""
    return os.path.normcase(normalize_path(path))


def is_within(root: str | os.PathLike[str], target: str | os.PathLike[str]) -> bool:
    """target 是否位于 root 之内（按真实路径比较，防止 ``..`` 与符号链接逃逸）。"""
    root_real = path_key(os.path.realpath(os.fspath(root)))
    target_real = path_key(os.path.realpath(os.fspath(target)))
    if root_real == target_real:
        return True
    if not target_real.startswith(root_real):
        return False
    return target_real[len(root_real) : len(root_real) + 1] in ("\\", "/")


def hash_file(path: str | os.PathLike[str], limit: int | None = None, chunk_size: int = 1 << 20) -> tuple[str, str]:
    """流式计算文件哈希。

    返回 ``(hash_hex, status)``，status 为 ``"full"`` 或 ``"partial"``（超过 limit 只哈希前 limit 字节）。
    """
    digest = hashlib.sha256()
    remaining = limit if limit is not None else None
    with open(path, "rb") as handle:
        while True:
            if remaining is not None and remaining <= 0:
                break
            size = chunk_size if remaining is None else min(chunk_size, remaining)
            block = handle.read(size)
            if not block:
                break
            digest.update(block)
            if remaining is not None:
                remaining -= len(block)
    return digest.hexdigest(), ("full" if limit is None or os.path.getsize(path) <= limit else "partial")


def read_limited(path: str | os.PathLike[str], limit: int) -> tuple[bytes, bool]:
    """读取文件前 limit 字节，返回 ``(data, truncated)``。"""
    with open(path, "rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        return data[:limit], True
    return data, False


# ---------------------------------------------------------------------------
# 扫描规则
# ---------------------------------------------------------------------------

DEFAULT_EXCLUDED_DIRS: tuple[str, ...] = (
    ".git",
    ".svn",
    ".hg",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    "env",
    ".idea",
    ".vscode",
    "$RECYCLE.BIN",
    "System Volume Information",
)

DEFAULT_EXCLUDED_SUFFIXES: tuple[str, ...] = (
    ".pyc",
    ".pyo",
    ".tmp",
    ".temp",
    ".swp",
    ".lock",
    ".part",
    ".crdownload",
    ".exe",
    ".dll",
    ".pyd",
    ".so",
    ".dylib",
    ".class",
    ".o",
    ".obj",
    ".zip",
    ".7z",
    ".rar",
    ".tar",
    ".gz",
    ".mp4",
    ".mkv",
    ".mp3",
)


@dataclass
class ScanRules:
    """扫描约束。所有约束都不修改磁盘内容。"""

    excluded_dirs: tuple[str, ...] = DEFAULT_EXCLUDED_DIRS
    excluded_suffixes: tuple[str, ...] = DEFAULT_EXCLUDED_SUFFIXES
    excluded_paths: tuple[str, ...] = ()  # 精确排除的文件（例如索引库自身）
    include_hidden: bool = False
    max_file_size: int = 50 * 1024 * 1024  # 超过此大小不索引（计入 skipped）
    follow_symlinks: bool = False  # 默认不跟随符号链接，避免逃逸授权根目录
    max_files: int | None = None

    def excluded_dir(self, name: str) -> bool:
        return name.casefold() in {d.casefold() for d in self.excluded_dirs}

    def excluded_suffix(self, name: str) -> bool:
        suffix = Path(name).suffix.casefold()
        return suffix in {s.casefold() for s in self.excluded_suffixes}


@dataclass
class FileCandidate:
    path: str
    rel_path: str
    name: str
    ext: str
    size: int
    mtime: float
    root: str
    is_symlink: bool = False

    @property
    def key(self) -> str:
        return path_key(self.path)


@dataclass
class ScanSnapshot:
    files: dict[str, FileCandidate] = field(default_factory=dict)
    skipped: int = 0
    skipped_reasons: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def skip(self, reason: str, count: int = 1) -> None:
        self.skipped += count
        self.skipped_reasons[reason] = self.skipped_reasons.get(reason, 0) + count


def _is_hidden(entry: os.DirEntry) -> bool:
    name = entry.name
    if name.startswith("."):
        return True
    if not WINDOWS:
        return False
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return False
    attributes = getattr(st, "st_file_attributes", 0)
    return bool(attributes & stat.FILE_ATTRIBUTE_HIDDEN)


class DirectoryWalker:
    """基于 os.scandir 的递归遍历，遵守 ScanRules。"""

    def __init__(self, rules: ScanRules | None = None) -> None:
        self.rules = rules or ScanRules()

    def walk(self, root: str | os.PathLike[str]) -> ScanSnapshot:
        root_path = normalize_path(root)
        snapshot = ScanSnapshot()
        if not os.path.isdir(root_path):
            snapshot.errors.append(f"根目录不存在或不是目录: {root_path}")
            return snapshot
        excluded_keys = {path_key(p) for p in self.rules.excluded_paths}
        seen = 0
        stack: list[tuple[str, str]] = [(root_path, "")]
        while stack:
            current, rel_base = stack.pop()
            try:
                entries = list(os.scandir(current))
            except OSError as exc:
                snapshot.errors.append(f"无法读取目录 {current}: {exc}")
                continue
            for entry in entries:
                rel = f"{rel_base}/{entry.name}" if rel_base else entry.name
                try:
                    if entry.is_symlink():
                        if not self.rules.follow_symlinks:
                            snapshot.skip("symlink")
                            continue
                        if not is_within(root_path, entry.path):
                            snapshot.skip("symlink_escape")
                            continue
                    if entry.is_dir(follow_symlinks=self.rules.follow_symlinks):
                        if self.rules.excluded_dir(entry.name):
                            snapshot.skip("excluded_dir")
                            continue
                        if not self.rules.include_hidden and _is_hidden(entry):
                            snapshot.skip("hidden")
                            continue
                        stack.append((entry.path, rel))
                        continue
                    if not entry.is_file(follow_symlinks=self.rules.follow_symlinks):
                        snapshot.skip("not_regular_file")
                        continue
                    if not self.rules.include_hidden and _is_hidden(entry):
                        snapshot.skip("hidden")
                        continue
                    if self.rules.excluded_suffix(entry.name):
                        snapshot.skip("excluded_suffix")
                        continue
                    if path_key(entry.path) in excluded_keys:
                        snapshot.skip("excluded_path")
                        continue
                    st = entry.stat(follow_symlinks=self.rules.follow_symlinks)
                    if st.st_size > self.rules.max_file_size:
                        snapshot.skip("too_large")
                        continue
                    if self.rules.max_files is not None and seen >= self.rules.max_files:
                        snapshot.skip("max_files")
                        continue
                    candidate = FileCandidate(
                        path=normalize_path(entry.path),
                        rel_path=rel.replace("\\", "/"),
                        name=entry.name,
                        ext=Path(entry.name).suffix.casefold(),
                        size=st.st_size,
                        mtime=st.st_mtime,
                        root=root_path,
                        is_symlink=entry.is_symlink(),
                    )
                    snapshot.files[candidate.key] = candidate
                    seen += 1
                except OSError as exc:
                    snapshot.errors.append(f"无法读取 {entry.path}: {exc}")
        return snapshot


@dataclass
class ScanDelta:
    added: list[FileCandidate] = field(default_factory=list)
    modified: list[FileCandidate] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)  # path_key

    @property
    def total_changes(self) -> int:
        return len(self.added) + len(self.modified) + len(self.deleted)


class FilesystemWatcher:
    """快照 + 差量对比。增量索引按 path + mtime + size 判断变化。

    移动识别需要内容哈希，由 IndexScheduler 结合数据库完成（size + hash 匹配）。
    """

    def __init__(self, rules: ScanRules | None = None) -> None:
        self.rules = rules or ScanRules()
        self.walker = DirectoryWalker(self.rules)

    def snapshot(self, root: str | os.PathLike[str]) -> ScanSnapshot:
        return self.walker.walk(root)

    def snapshot_many(self, roots: Iterable[str | os.PathLike[str]]) -> dict[str, ScanSnapshot]:
        return {normalize_path(root): self.snapshot(root) for root in roots}

    def diff(
        self,
        old: dict[str, tuple[int, float]],
        new: ScanSnapshot,
    ) -> ScanDelta:
        """old 为 ``{path_key: (size, mtime)}``。"""
        delta = ScanDelta()
        for key, cand in new.files.items():
            previous = old.get(key)
            if previous is None:
                delta.added.append(cand)
            elif previous[0] != cand.size or abs(previous[1] - cand.mtime) > 1e-6:
                delta.modified.append(cand)
        for key in old:
            if key not in new.files:
                delta.deleted.append(key)
        return delta
