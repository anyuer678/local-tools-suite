"""数据模型：扫描记录、统计、快照、差异、重复、建议与执行计划。

所有模型都只使用标准库 dataclasses，便于 CLI / GUI / 测试共用。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class EntryKind(str, Enum):
    FILE = "file"
    DIR = "dir"
    SYMLINK = "symlink"
    OTHER = "other"


class EntryStatus(str, Enum):
    SCANNED = "scanned"
    SKIPPED = "skipped"
    FAILED = "failed"


class ChangeType(str, Enum):
    ADDED = "added"
    DELETED = "deleted"
    GROWN = "grown"
    SHRUNK = "shrunk"
    MODIFIED = "modified"
    MOVED = "moved"
    RENAMED = "renamed"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ActionKind(str, Enum):
    QUARANTINE = "quarantine"
    MOVE = "move"
    DELETE = "delete"
    REVIEW = "review"


class OperationKind(str, Enum):
    QUARANTINE = "quarantine"
    MOVE = "move"
    DELETE = "delete"


def human_size(num: Optional[int]) -> str:
    """把字节数格式化为人类可读文本（二进制单位）。"""
    if num is None:
        return "未知"
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{value:.2f} TB"


@dataclass
class FileRecord:
    """单个文件系统条目（文件 / 目录 / 符号链接）的扫描记录。"""

    path: str
    rel_path: str
    name: str
    suffix: str
    size: Optional[int]
    mtime_ns: int = 0
    ctime_ns: int = 0
    inode: int = 0
    device: int = 0
    nlink: int = 1
    kind: str = EntryKind.FILE.value
    status: str = EntryStatus.SCANNED.value
    reason: str = ""
    volatile: bool = False
    sha256: Optional[str] = None

    @property
    def is_scanned_file(self) -> bool:
        return self.kind == EntryKind.FILE.value and self.status == EntryStatus.SCANNED.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "rel_path": self.rel_path,
            "name": self.name,
            "suffix": self.suffix,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "ctime_ns": self.ctime_ns,
            "inode": self.inode,
            "device": self.device,
            "nlink": self.nlink,
            "kind": self.kind,
            "status": self.status,
            "reason": self.reason,
            "volatile": self.volatile,
            "sha256": self.sha256,
        }


@dataclass
class DirectoryRecord:
    """目录聚合记录：目录本身的大小 = 其下所有被扫描文件大小之和。"""

    path: str
    rel_path: str
    size: int = 0
    file_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    status: str = EntryStatus.SCANNED.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "rel_path": self.rel_path,
            "size": self.size,
            "file_count": self.file_count,
            "failed_count": self.failed_count,
            "skipped_count": self.skipped_count,
            "status": self.status,
        }


@dataclass
class ScanStats:
    files_scanned: int = 0
    dirs_scanned: int = 0
    total_size: int = 0
    files_skipped: int = 0
    dirs_skipped: int = 0
    symlinks_skipped: int = 0
    failed: int = 0
    unknown_size: int = 0
    volatile: int = 0
    zero_byte: int = 0
    cancelled: bool = False
    complete: bool = False
    started_at: str = ""
    finished_at: str = ""
    duration_seconds: float = 0.0

    @property
    def skipped(self) -> int:
        return self.files_skipped + self.dirs_skipped + self.symlinks_skipped

    @property
    def completeness_pct(self) -> float:
        seen = self.files_scanned + self.failed
        if seen == 0:
            return 100.0
        return round(self.files_scanned * 100.0 / seen, 2)

    @property
    def completeness_label(self) -> str:
        if self.cancelled:
            return "已取消（部分结果）"
        if self.failed:
            return "部分完整（存在失败项）"
        return "完整"

    def as_dict(self) -> dict[str, Any]:
        return {
            "files_scanned": self.files_scanned,
            "dirs_scanned": self.dirs_scanned,
            "total_size": self.total_size,
            "total_size_human": human_size(self.total_size),
            "files_skipped": self.files_skipped,
            "dirs_skipped": self.dirs_skipped,
            "symlinks_skipped": self.symlinks_skipped,
            "skipped": self.skipped,
            "failed": self.failed,
            "unknown_size": self.unknown_size,
            "volatile": self.volatile,
            "zero_byte": self.zero_byte,
            "cancelled": self.cancelled,
            "complete": self.complete,
            "completeness_pct": self.completeness_pct,
            "completeness": self.completeness_label,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round(self.duration_seconds, 3),
        }


@dataclass
class ScanInfo:
    """一次已保存扫描的元信息。"""

    scan_id: str
    root: str
    started_at: str
    finished_at: Optional[str]
    stats: ScanStats
    options: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scan_id": self.scan_id,
            "root": self.root,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stats": self.stats.as_dict(),
            "options": self.options,
        }


@dataclass
class ScanResult:
    scan_id: str
    root: str
    stats: ScanStats
    options: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scan_id": self.scan_id,
            "root": self.root,
            "stats": self.stats.as_dict(),
            "options": self.options,
        }


@dataclass
class DiffChange:
    change_type: str
    old_path: Optional[str] = None
    new_path: Optional[str] = None
    rel_path: str = ""
    old_size: Optional[int] = None
    new_size: Optional[int] = None
    evidence: str = ""
    sha256: Optional[str] = None

    @property
    def delta(self) -> int:
        return (self.new_size or 0) - (self.old_size or 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_type": self.change_type,
            "old_path": self.old_path,
            "new_path": self.new_path,
            "rel_path": self.rel_path,
            "old_size": self.old_size,
            "new_size": self.new_size,
            "delta": self.delta,
            "delta_human": human_size(self.delta),
            "evidence": self.evidence,
            "sha256": self.sha256,
        }


@dataclass
class DirMove:
    old_rel: str
    new_rel: str
    file_count: int
    moved_bytes: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_rel": self.old_rel,
            "new_rel": self.new_rel,
            "file_count": self.file_count,
            "moved_bytes": self.moved_bytes,
            "moved_bytes_human": human_size(self.moved_bytes),
        }


@dataclass
class DirGrowth:
    rel_path: str
    path: str
    old_size: int
    new_size: int

    @property
    def delta(self) -> int:
        return self.new_size - self.old_size

    def to_dict(self) -> dict[str, Any]:
        return {
            "rel_path": self.rel_path,
            "path": self.path,
            "old_size": self.old_size,
            "new_size": self.new_size,
            "delta": self.delta,
            "delta_human": human_size(self.delta),
        }


@dataclass
class DiffResult:
    old_scan_id: str
    new_scan_id: str
    changes: list[DiffChange] = field(default_factory=list)
    dir_moves: list[DirMove] = field(default_factory=list)
    dir_growth: list[DirGrowth] = field(default_factory=list)
    unchanged_count: int = 0
    old_stats: Optional[ScanStats] = None
    new_stats: Optional[ScanStats] = None
    notes: list[str] = field(default_factory=list)

    def changes_of(self, change_type: str) -> list[DiffChange]:
        return [c for c in self.changes if c.change_type == change_type]

    @property
    def counts(self) -> dict[str, int]:
        result = {t.value: 0 for t in ChangeType}
        for c in self.changes:
            result[c.change_type] = result.get(c.change_type, 0) + 1
        return result

    @property
    def added_bytes(self) -> int:
        return sum(c.new_size or 0 for c in self.changes if c.change_type == ChangeType.ADDED.value)

    @property
    def deleted_bytes(self) -> int:
        return sum(c.old_size or 0 for c in self.changes if c.change_type == ChangeType.DELETED.value)

    @property
    def net_change(self) -> int:
        if self.old_stats and self.new_stats:
            return self.new_stats.total_size - self.old_stats.total_size
        return 0

    def growth_sources(self, limit: int = 10) -> list[dict[str, Any]]:
        """按父目录汇总正增量，返回主要增长来源。"""
        buckets: dict[str, int] = {}
        for change in self.changes:
            if change.delta <= 0:
                continue
            path = change.new_path or change.old_path or ""
            parent = path.rsplit("\\", 1)[0] if "\\" in path else path.rsplit("/", 1)[0]
            buckets[parent] = buckets.get(parent, 0) + change.delta
        ordered = sorted(buckets.items(), key=lambda item: item[1], reverse=True)
        return [{"path": p, "delta": d, "delta_human": human_size(d)} for p, d in ordered[:limit]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_scan_id": self.old_scan_id,
            "new_scan_id": self.new_scan_id,
            "counts": self.counts,
            "unchanged_count": self.unchanged_count,
            "added_bytes": self.added_bytes,
            "deleted_bytes": self.deleted_bytes,
            "net_change": self.net_change,
            "net_change_human": human_size(self.net_change),
            "old_stats": self.old_stats.as_dict() if self.old_stats else None,
            "new_stats": self.new_stats.as_dict() if self.new_stats else None,
            "changes": [c.to_dict() for c in self.changes],
            "dir_moves": [d.to_dict() for d in self.dir_moves],
            "dir_growth": [d.to_dict() for d in self.dir_growth],
            "growth_sources": self.growth_sources(),
            "notes": self.notes,
        }


@dataclass
class DuplicateFile:
    path: str
    rel_path: str
    size: int
    inode: int = 0
    device: int = 0
    sha256: Optional[str] = None
    hardlink_of: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "rel_path": self.rel_path,
            "size": self.size,
            "inode": self.inode,
            "device": self.device,
            "sha256": self.sha256,
            "hardlink_of": self.hardlink_of,
        }


@dataclass
class DuplicateGroup:
    size: int
    sha256: str
    files: list[DuplicateFile] = field(default_factory=list)
    reclaimable: int = 0
    hardlink_skipped: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "size": self.size,
            "size_human": human_size(self.size),
            "sha256": self.sha256,
            "files": [f.to_dict() for f in self.files],
            "file_count": len(self.files),
            "reclaimable": self.reclaimable,
            "reclaimable_human": human_size(self.reclaimable),
            "hardlink_skipped": self.hardlink_skipped,
        }


@dataclass
class DuplicateReport:
    scan_id: str
    threshold: int
    groups: list[DuplicateGroup] = field(default_factory=list)
    scanned_files: int = 0
    candidate_files: int = 0
    hashed_files: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    total_reclaimable: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "scan_id": self.scan_id,
            "threshold": self.threshold,
            "threshold_human": human_size(self.threshold),
            "group_count": len(self.groups),
            "groups": [g.to_dict() for g in self.groups],
            "scanned_files": self.scanned_files,
            "candidate_files": self.candidate_files,
            "hashed_files": self.hashed_files,
            "errors": self.errors,
            "total_reclaimable": self.total_reclaimable,
            "total_reclaimable_human": human_size(self.total_reclaimable),
        }


@dataclass
class Recommendation:
    rec_id: str
    action: str
    path: str
    rel_path: str
    category: str
    reason: str
    evidence: str
    benefit_bytes: int
    risk: str
    confidence: float = 1.0
    requires_review: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "rec_id": self.rec_id,
            "action": self.action,
            "path": self.path,
            "rel_path": self.rel_path,
            "category": self.category,
            "reason": self.reason,
            "evidence": self.evidence,
            "benefit_bytes": self.benefit_bytes,
            "benefit_human": human_size(self.benefit_bytes),
            "risk": self.risk,
            "confidence": self.confidence,
            "requires_review": self.requires_review,
        }


@dataclass
class WatchTarget:
    rel_path: str
    threshold_bytes: int
    baseline_bytes: int = 0
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rel_path": self.rel_path,
            "threshold_bytes": self.threshold_bytes,
            "threshold_human": human_size(self.threshold_bytes),
            "baseline_bytes": self.baseline_bytes,
            "baseline_human": human_size(self.baseline_bytes),
            "created_at": self.created_at,
        }


@dataclass
class WatchAlert:
    rel_path: str
    threshold_bytes: int
    baseline_bytes: int
    current_bytes: int
    scan_id: str

    @property
    def growth(self) -> int:
        return self.current_bytes - self.baseline_bytes

    @property
    def exceeded(self) -> bool:
        return self.growth >= self.threshold_bytes

    def to_dict(self) -> dict[str, Any]:
        return {
            "rel_path": self.rel_path,
            "threshold_bytes": self.threshold_bytes,
            "threshold_human": human_size(self.threshold_bytes),
            "baseline_bytes": self.baseline_bytes,
            "current_bytes": self.current_bytes,
            "growth": self.growth,
            "growth_human": human_size(self.growth),
            "exceeded": self.exceeded,
            "scan_id": self.scan_id,
        }


@dataclass
class Operation:
    op_id: str
    kind: str
    source: str
    target: Optional[str] = None
    expected_size: Optional[int] = None
    expected_hash: Optional[str] = None
    reason: str = ""
    risk: str = RiskLevel.HIGH.value
    irreversible: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "op_id": self.op_id,
            "kind": self.kind,
            "source": self.source,
            "target": self.target,
            "expected_size": self.expected_size,
            "expected_hash": self.expected_hash,
            "reason": self.reason,
            "risk": self.risk,
            "irreversible": self.irreversible,
        }


@dataclass
class Plan:
    plan_id: str
    operations: list[Operation]
    token: str
    created_at: str
    notes: list[str] = field(default_factory=list)

    def preview_lines(self) -> list[str]:
        lines = [f"操作计划 {self.plan_id}（共 {len(self.operations)} 项）"]
        for op in self.operations:
            target = f" -> {op.target}" if op.target else ""
            lines.append(
                f"[{op.kind.upper()}] {op.source}{target} "
                f"(预期大小 {human_size(op.expected_size)}, 风险 {op.risk})"
            )
            if op.reason:
                lines.append(f"    原因: {op.reason}")
        lines.append(f"预览令牌: {self.token}")
        lines.append("确认后执行：必须传入 confirm=True 且令牌一致。")
        return lines

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "token": self.token,
            "created_at": self.created_at,
            "operations": [o.to_dict() for o in self.operations],
            "notes": self.notes,
        }


@dataclass
class OperationResult:
    op_id: str
    kind: str
    source: str
    target: Optional[str]
    status: str
    message: str = ""
    verified: bool = False
    sha256: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "op_id": self.op_id,
            "kind": self.kind,
            "source": self.source,
            "target": self.target,
            "status": self.status,
            "message": self.message,
            "verified": self.verified,
            "sha256": self.sha256,
        }


@dataclass
class ExecutionResult:
    execution_id: str
    plan_id: str
    log_path: str
    results: list[OperationResult] = field(default_factory=list)

    @property
    def succeeded(self) -> int:
        return sum(1 for r in self.results if r.status == "succeeded")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == "failed")

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "plan_id": self.plan_id,
            "log_path": self.log_path,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "results": [r.to_dict() for r in self.results],
        }


@dataclass
class RollbackResult:
    execution_id: str
    restored: int = 0
    skipped: int = 0
    failed: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_id": self.execution_id,
            "restored": self.restored,
            "skipped": self.skipped,
            "failed": self.failed,
            "details": self.details,
        }


def dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)
