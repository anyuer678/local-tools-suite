"""重复文件检测：先按大小分组，再对候选组计算 SHA-256，最后扣除硬链接。

原则：
- 只有大小相同且 SHA-256 相同才算“完全重复”。
- 同名但内容不同不算重复。
- 同一 inode 的硬链接不重复计入可回收空间。
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Iterable, Optional, TYPE_CHECKING

from .models import DuplicateFile, DuplicateGroup, DuplicateReport, FileRecord, human_size

if TYPE_CHECKING:  # pragma: no cover
    from .snapshot_store import SnapshotStore


DEFAULT_MIN_SIZE = 4096
CHUNK_SIZE = 1024 * 1024


class HashError(OSError):
    """文件无法读取 / 哈希失败。"""


def file_sha256(path: str | os.PathLike[str], chunk_size: int = CHUNK_SIZE) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def find_duplicates(
    entries: Iterable[FileRecord],
    min_size: int = DEFAULT_MIN_SIZE,
    store: Optional["SnapshotStore"] = None,
    scan_id: Optional[str] = None,
) -> DuplicateReport:
    """从文件记录集合中检测重复文件。

    entries 必须是已扫描文件（status=scanned）；大小未知的条目会被忽略。
    """
    candidates: list[FileRecord] = []
    scanned_files = 0
    for entry in entries:
        if entry.kind != "file" or entry.status != "scanned":
            continue
        scanned_files += 1
        if entry.size is None or entry.size <= 0:
            continue
        if entry.size < min_size:
            continue
        candidates.append(entry)

    report = DuplicateReport(
        scan_id=scan_id or "",
        threshold=min_size,
        scanned_files=scanned_files,
        candidate_files=len(candidates),
    )

    by_size: dict[int, list[FileRecord]] = {}
    for entry in candidates:
        by_size.setdefault(int(entry.size or 0), []).append(entry)

    pending_hash_updates: list[tuple[str, str]] = []
    for size, size_group in sorted(by_size.items(), reverse=True):
        if len(size_group) < 2:
            continue
        by_hash: dict[str, list[FileRecord]] = {}
        for entry in size_group:
            digest = entry.sha256
            if digest is None:
                try:
                    digest = file_sha256(entry.path)
                except OSError as exc:
                    report.errors.append({"path": entry.path, "error": f"{type(exc).__name__}: {exc}"})
                    continue
                entry.sha256 = digest
                report.hashed_files += 1
                if store is not None and scan_id:
                    pending_hash_updates.append((entry.rel_path, digest))
            by_hash.setdefault(digest, []).append(entry)

        for digest, hash_group in by_hash.items():
            if len(hash_group) < 2:
                continue
            hash_group.sort(key=lambda item: item.rel_path.lower())
            files: list[DuplicateFile] = []
            seen_inodes: dict[tuple[int, int], DuplicateFile] = {}
            hardlink_skipped = 0
            for entry in hash_group:
                key = (entry.device, entry.inode)
                if entry.inode and key in seen_inodes:
                    hardlink_skipped += 1
                    files.append(
                        DuplicateFile(
                            path=entry.path,
                            rel_path=entry.rel_path,
                            size=int(entry.size or 0),
                            inode=entry.inode,
                            device=entry.device,
                            sha256=digest,
                            hardlink_of=seen_inodes[key].rel_path,
                        )
                    )
                    continue
                dup = DuplicateFile(
                    path=entry.path,
                    rel_path=entry.rel_path,
                    size=int(entry.size or 0),
                    inode=entry.inode,
                    device=entry.device,
                    sha256=digest,
                )
                files.append(dup)
                if entry.inode:
                    seen_inodes[key] = dup
            unique_copies = len(files) - hardlink_skipped
            reclaimable = size * (unique_copies - 1)
            report.groups.append(
                DuplicateGroup(
                    size=size,
                    sha256=digest,
                    files=files,
                    reclaimable=reclaimable,
                    hardlink_skipped=hardlink_skipped,
                )
            )

    report.groups.sort(key=lambda group: (-group.reclaimable, -group.size))
    report.total_reclaimable = sum(group.reclaimable for group in report.groups)
    if store is not None and scan_id:
        for rel_path, digest in pending_hash_updates:
            store.set_entry_hash(scan_id, rel_path, digest)
    return report


def find_duplicates_from_store(
    store: "SnapshotStore",
    scan_id: str,
    min_size: int = DEFAULT_MIN_SIZE,
) -> DuplicateReport:
    return find_duplicates(
        store.iter_entries(scan_id, status="scanned", kind="file", min_size=min_size),
        min_size=min_size,
        store=store,
        scan_id=scan_id,
    )


def find_duplicates_in_paths(
    paths: Iterable[str | os.PathLike[str]],
    min_size: int = DEFAULT_MIN_SIZE,
) -> DuplicateReport:
    """对任意文件路径列表做重复检测（不依赖快照数据库）。"""
    records: list[FileRecord] = []
    for raw in paths:
        path = str(raw)
        try:
            st = os.stat(path, follow_symlinks=False)
        except OSError:
            continue
        if not os.path.isfile(path):
            continue
        records.append(
            FileRecord(
                path=str(Path(path).absolute()),
                rel_path=Path(path).name,
                name=Path(path).name,
                suffix=Path(path).suffix.lower(),
                size=st.st_size,
                mtime_ns=st.st_mtime_ns,
                ctime_ns=st.st_ctime_ns,
                inode=st.st_ino,
                device=st.st_dev,
                nlink=st.st_nlink,
            )
        )
    return find_duplicates(records, min_size=min_size)


def duplicates_to_csv(report: DuplicateReport) -> list[list[str]]:
    rows = [["sha256", "size", "size_human", "files", "reclaimable", "reclaimable_human", "hardlink_skipped"]]
    for group in report.groups:
        rows.append(
            [
                group.sha256,
                str(group.size),
                human_size(group.size),
                ";".join(file.path for file in group.files),
                str(group.reclaimable),
                human_size(group.reclaimable),
                str(group.hardlink_skipped),
            ]
        )
    return rows
