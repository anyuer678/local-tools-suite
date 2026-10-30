"""快照差异引擎：新增、删除、变大、变小、内容修改、改名、移动目录。

关键原则：移动 / 改名必须有“大小 + SHA-256”证据；仅凭文件名相似不做判断。
"""

from __future__ import annotations

import os
from collections import defaultdict
from typing import Optional, TYPE_CHECKING

from .duplicates import file_sha256
from .models import (
    ChangeType,
    DiffChange,
    DiffResult,
    DirGrowth,
    DirMove,
    FileRecord,
    human_size,
)

if TYPE_CHECKING:  # pragma: no cover
    from .snapshot_store import SnapshotStore


def _norm_rel(rel_path: str) -> str:
    return rel_path.replace("\\", "/").strip("/").casefold()


def _parent(rel_path: str) -> str:
    rel_path = rel_path.replace("\\", "/").strip("/")
    if "/" not in rel_path:
        return ""
    return rel_path.rsplit("/", 1)[0]


def _basename(rel_path: str) -> str:
    return rel_path.replace("\\", "/").rsplit("/", 1)[-1]


class _HashCache:
    """只对“新快照中的文件”允许读盘计算哈希，避免把旧快照路径的现状当成历史内容。"""

    def __init__(self, store: "SnapshotStore") -> None:
        self.store = store
        self._cache: dict[tuple[str, str], Optional[str]] = {}

    def stored(self, scan_id: str, entry: FileRecord) -> Optional[str]:
        if entry.sha256:
            return entry.sha256
        digest = self.store.stored_hash(scan_id, entry.rel_path)
        if digest:
            entry.sha256 = digest
        return digest

    def current(self, scan_id: str, entry: FileRecord) -> Optional[str]:
        key = (scan_id, entry.rel_path)
        if key in self._cache:
            return self._cache[key]
        digest = self.stored(scan_id, entry)
        if digest is None and os.path.isfile(entry.path):
            try:
                digest = file_sha256(entry.path)
            except OSError:
                digest = None
            if digest:
                self.store.set_entry_hash(scan_id, entry.rel_path, digest)
                entry.sha256 = digest
        self._cache[key] = digest
        return digest


def _load_files(store: "SnapshotStore", scan_id: str) -> dict[str, FileRecord]:
    files: dict[str, FileRecord] = {}
    for entry in store.iter_entries(scan_id):
        if entry.kind != "file" or entry.status != "scanned":
            continue
        files.setdefault(_norm_rel(entry.rel_path), entry)
    return files


def compare_scans(store: "SnapshotStore", old_scan_id: str, new_scan_id: str) -> DiffResult:
    old_scan = store.get_scan(old_scan_id)
    new_scan = store.get_scan(new_scan_id)
    if old_scan is None:
        raise ValueError(f"旧扫描不存在: {old_scan_id}")
    if new_scan is None:
        raise ValueError(f"新扫描不存在: {new_scan_id}")

    result = DiffResult(
        old_scan_id=old_scan_id,
        new_scan_id=new_scan_id,
        old_stats=old_scan.stats,
        new_stats=new_scan.stats,
    )
    if os.path.normcase(os.path.abspath(old_scan.root)) != os.path.normcase(os.path.abspath(new_scan.root)):
        result.notes.append(
            f"两次扫描根目录不同（{old_scan.root} vs {new_scan.root}），路径对比仅供参考"
        )

    # 只保留旧快照的一份字典，并边读新快照边弹出匹配项。
    # 这样峰值内存约为“旧快照 + 变更集”，而不是同时保留两份完整文件字典。
    old_files = _load_files(store, old_scan_id)
    hashes = _HashCache(store)

    old_only: list[FileRecord] = []
    new_only: list[FileRecord] = []
    new_seen_count = 0

    for new_entry in store.iter_entries(new_scan_id):
        if new_entry.kind != "file" or new_entry.status != "scanned":
            continue
        new_seen_count += 1
        key = _norm_rel(new_entry.rel_path)
        old_entry = old_files.pop(key, None)
        if old_entry is None:
            new_only.append(new_entry)
            continue
        old_size = old_entry.size
        new_size = new_entry.size
        if old_size is None or new_size is None:
            result.notes.append(f"大小未知，跳过比较: {new_entry.rel_path}")
            continue
        if new_size > old_size:
            result.changes.append(
                DiffChange(
                    change_type=ChangeType.GROWN.value,
                    old_path=old_entry.path,
                    new_path=new_entry.path,
                    rel_path=new_entry.rel_path,
                    old_size=old_size,
                    new_size=new_size,
                    evidence=f"大小 {human_size(old_size)} -> {human_size(new_size)}，增加 {human_size(new_size - old_size)}",
                )
            )
            continue
        if new_size < old_size:
            result.changes.append(
                DiffChange(
                    change_type=ChangeType.SHRUNK.value,
                    old_path=old_entry.path,
                    new_path=new_entry.path,
                    rel_path=new_entry.rel_path,
                    old_size=old_size,
                    new_size=new_size,
                    evidence=f"大小 {human_size(old_size)} -> {human_size(new_size)}，减少 {human_size(old_size - new_size)}",
                )
            )
            continue

        # 大小相同：优先使用快照内的哈希；旧快照没有哈希时不能读盘“伪造历史”。
        old_hash = hashes.stored(old_scan_id, old_entry)
        new_hash = hashes.current(new_scan_id, new_entry)
        if old_hash and new_hash:
            if old_hash != new_hash:
                result.changes.append(
                    DiffChange(
                        change_type=ChangeType.MODIFIED.value,
                        old_path=old_entry.path,
                        new_path=new_entry.path,
                        rel_path=new_entry.rel_path,
                        old_size=old_size,
                        new_size=new_size,
                        evidence=(
                            f"大小不变（{human_size(new_size)}）；SHA-256 不一致 "
                            f"{old_hash[:16]}… vs {new_hash[:16]}…"
                        ),
                        sha256=new_hash,
                    )
                )
            else:
                result.unchanged_count += 1
        elif old_entry.mtime_ns == new_entry.mtime_ns:
            result.unchanged_count += 1
        else:
            result.changes.append(
                DiffChange(
                    change_type=ChangeType.MODIFIED.value,
                    old_path=old_entry.path,
                    new_path=new_entry.path,
                    rel_path=new_entry.rel_path,
                    old_size=old_size,
                    new_size=new_size,
                    evidence=(
                        "大小不变，但 mtime 变化；旧快照未保存哈希，"
                        "无法给出哈希级证据（建议开启扫描哈希）"
                    ),
                    sha256=new_hash,
                )
            )

    old_only.extend(old_files.values())

    moved_pairs: list[tuple[FileRecord, FileRecord, str]] = []
    unmatched_old: list[FileRecord] = []
    unmatched_new: list[FileRecord] = []

    old_by_size: dict[int, list[FileRecord]] = defaultdict(list)
    new_by_size: dict[int, list[FileRecord]] = defaultdict(list)
    for entry in old_only:
        if entry.size is not None:
            old_by_size[int(entry.size)].append(entry)
    for entry in new_only:
        if entry.size is not None:
            new_by_size[int(entry.size)].append(entry)

    missing_old_hash = 0
    for size in sorted(set(old_by_size) & set(new_by_size), reverse=True):
        old_group = old_by_size[size]
        new_group = new_by_size[size]
        old_hash_groups: dict[str, list[FileRecord]] = defaultdict(list)
        for entry in old_group:
            digest = hashes.stored(old_scan_id, entry)
            if digest is None:
                missing_old_hash += 1
                unmatched_old.append(entry)
            else:
                old_hash_groups[digest].append(entry)

        new_hash_groups: dict[str, list[FileRecord]] = defaultdict(list)
        for entry in new_group:
            digest = hashes.current(new_scan_id, entry)
            if digest is None:
                unmatched_new.append(entry)
            else:
                new_hash_groups[digest].append(entry)

        for digest, olds in old_hash_groups.items():
            news = new_hash_groups.get(digest)
            if not news:
                unmatched_old.extend(olds)
                continue
            used_new: set[int] = set()
            leftovers_old: list[FileRecord] = []
            for old_entry in olds:
                match_index = None
                # 优先同名（跨目录移动），其次同大小同哈希的任意候选（改名）。
                for idx, candidate in enumerate(news):
                    if idx in used_new:
                        continue
                    if _basename(candidate.rel_path).casefold() == _basename(old_entry.rel_path).casefold():
                        match_index = idx
                        break
                if match_index is None:
                    for idx in range(len(news)):
                        if idx not in used_new:
                            match_index = idx
                            break
                if match_index is None:
                    leftovers_old.append(old_entry)
                    continue
                used_new.add(match_index)
                moved_pairs.append((old_entry, news[match_index], digest))
            for idx, candidate in enumerate(news):
                if idx not in used_new:
                    unmatched_new.append(candidate)
            unmatched_old.extend(leftovers_old)

        # 没有旧哈希的条目无法参与移动判定，直接视为删除 / 新增。
        old_hash_groups.clear()

    # 上面按大小遍历后，未匹配的 new（来自没有旧哈希的 size 组）也需要收集。
    paired_new_paths = {_norm_rel(new_entry.rel_path) for _, new_entry, _ in moved_pairs}
    for entry in new_only:
        if _norm_rel(entry.rel_path) not in paired_new_paths and entry not in unmatched_new:
            unmatched_new.append(entry)

    old_by_paths = {_norm_rel(e.rel_path) for e in old_only}
    paired_old_paths = {_norm_rel(old_entry.rel_path) for old_entry, _, _ in moved_pairs}
    for entry in old_only:
        if _norm_rel(entry.rel_path) not in paired_old_paths and entry not in unmatched_old:
            unmatched_old.append(entry)

    if missing_old_hash:
        result.notes.append(
            f"{missing_old_hash} 个旧快照文件缺少 SHA-256，无法作为移动证据；"
            "如需检测改名/移动，请在扫描时开启哈希（--hash）"
        )

    move_parents: dict[tuple[str, str], dict[str, int]] = {}
    for old_entry, new_entry, digest in moved_pairs:
        old_base = _basename(old_entry.rel_path)
        new_base = _basename(new_entry.rel_path)
        change_type = ChangeType.RENAMED.value if old_base.casefold() != new_base.casefold() else ChangeType.MOVED.value
        result.changes.append(
            DiffChange(
                change_type=change_type,
                old_path=old_entry.path,
                new_path=new_entry.path,
                rel_path=new_entry.rel_path,
                old_size=old_entry.size,
                new_size=new_entry.size,
                evidence=(
                    f"大小一致（{human_size(old_entry.size)}）且 SHA-256 一致 "
                    f"{digest[:16]}…；{old_entry.rel_path} -> {new_entry.rel_path}"
                ),
                sha256=digest,
            )
        )
        old_parent = _parent(old_entry.rel_path)
        new_parent = _parent(new_entry.rel_path)
        if old_parent.casefold() != new_parent.casefold():
            bucket = move_parents.setdefault((old_parent, new_parent), {"count": 0, "bytes": 0})
            bucket["count"] += 1
            bucket["bytes"] += int(new_entry.size or 0)

    for entry in unmatched_old:
        result.changes.append(
            DiffChange(
                change_type=ChangeType.DELETED.value,
                old_path=entry.path,
                new_path=None,
                rel_path=entry.rel_path,
                old_size=entry.size,
                new_size=None,
                evidence=f"旧快照存在（{human_size(entry.size)}），新快照不存在",
                sha256=entry.sha256,
            )
        )
    for entry in unmatched_new:
        result.changes.append(
            DiffChange(
                change_type=ChangeType.ADDED.value,
                old_path=None,
                new_path=entry.path,
                rel_path=entry.rel_path,
                old_size=None,
                new_size=entry.size,
                evidence=f"旧快照不存在，新快照存在（{human_size(entry.size)}）",
                sha256=entry.sha256,
            )
        )

    # 目录移动：仅在“旧父目录在新快照消失、新父目录在旧快照不存在”时认定为目录移动。
    old_dirs = {
        _norm_rel(d.rel_path)
        for d in store.iter_dirs(old_scan_id)
        if d.status == "scanned"
    }
    new_dirs = {
        _norm_rel(d.rel_path)
        for d in store.iter_dirs(new_scan_id)
        if d.status == "scanned"
    }
    for (old_parent, new_parent), stats in sorted(move_parents.items()):
        if (
            old_parent
            and new_parent
            and _norm_rel(old_parent) not in new_dirs
            and _norm_rel(new_parent) not in old_dirs
        ):
            result.dir_moves.append(
                DirMove(
                    old_rel=old_parent,
                    new_rel=new_parent,
                    file_count=stats["count"],
                    moved_bytes=stats["bytes"],
                )
            )

    # 目录净增长（只列正增长，按增量降序）。
    old_dir_sizes = {
        _norm_rel(d.rel_path): d
        for d in store.iter_dirs(old_scan_id)
        if d.status == "scanned"
    }
    new_dir_sizes = {
        _norm_rel(d.rel_path): d
        for d in store.iter_dirs(new_scan_id)
        if d.status == "scanned"
    }
    growth: list[DirGrowth] = []
    for key in set(old_dir_sizes) & set(new_dir_sizes):
        old_dir = old_dir_sizes[key]
        new_dir = new_dir_sizes[key]
        if new_dir.size > old_dir.size:
            growth.append(
                DirGrowth(
                    rel_path=new_dir.rel_path,
                    path=new_dir.path,
                    old_size=old_dir.size,
                    new_size=new_dir.size,
                )
            )
    growth.sort(key=lambda item: item.delta, reverse=True)
    result.dir_growth = growth[:50]

    total_seen = new_seen_count + len(old_only)
    result.changes.sort(key=lambda change: (change.change_type, -abs(change.delta), change.rel_path))
    return result


def compare_with_latest(store: "SnapshotStore", scan_id: str) -> DiffResult:
    latest = store.latest_scan()
    if latest is None:
        raise ValueError("数据库中没有扫描快照")
    return compare_scans(store, latest.scan_id, scan_id)
