"""安全策略：排除规则、系统关键目录保护、清理建议与增长观察目标。"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, TYPE_CHECKING

from .models import (
    ActionKind,
    DuplicateReport,
    Recommendation,
    RiskLevel,
    WatchAlert,
    WatchTarget,
    human_size,
)

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型提示
    from .snapshot_store import SnapshotStore


DEFAULT_EXCLUDE_DIR_NAMES = (
    "$RECYCLE.BIN",
    "System Volume Information",
    ".git",
    ".svn",
    "node_modules",
    "__pycache__",
)

TEMP_SUFFIXES = (".tmp", ".temp", ".log", ".bak", ".old", ".cache", ".dmp", ".swp")
TEMP_DIR_NAMES = ("temp", "tmp", "cache", "caches", "crashdumps", "logs")

# 这些目录必须精确匹配，不能使用“前缀包含”，否则用户目录下的普通文件会被误判。
_EXACT_CRITICAL_NAMES = ("users", "documents", "desktop", "downloads")


def default_state_dir() -> Path:
    """默认状态目录：%LOCALAPPDATA%\\SpacePilot（非 Windows 回退到 ~/.spacepilot）。"""
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "SpacePilot"
    return Path.home() / ".spacepilot"


def default_db_path() -> Path:
    return default_state_dir() / "spacepilot.db"


def default_quarantine_dir() -> Path:
    return default_state_dir() / "quarantine"


def default_log_path() -> Path:
    return default_state_dir() / "executions.jsonl"


@dataclass(frozen=True)
class ExcludeRules:
    dir_names: frozenset[str] = field(default_factory=lambda: frozenset(DEFAULT_EXCLUDE_DIR_NAMES))
    suffixes: frozenset[str] = frozenset()
    min_file_size: int = 0

    def __post_init__(self) -> None:  # 统一大小写/后缀格式
        object.__setattr__(
            self,
            "dir_names",
            frozenset(name.strip().lower() for name in self.dir_names if name.strip()),
        )
        object.__setattr__(
            self,
            "suffixes",
            frozenset(_normalize_suffix(s) for s in self.suffixes if s.strip()),
        )
        object.__setattr__(self, "min_file_size", max(0, int(self.min_file_size)))

    def should_exclude_dir(self, name: str) -> bool:
        return name.strip().lower() in self.dir_names

    def should_exclude_file(self, name: str, size: Optional[int]) -> tuple[bool, str]:
        suffix = _normalize_suffix(os.path.splitext(name)[1])
        if suffix and suffix in self.suffixes:
            return True, f"excluded:suffix:{suffix}"
        if size is None:
            return False, ""
        if self.min_file_size > 0 and size < self.min_file_size:
            return True, f"excluded:min_size:{size}<{self.min_file_size}"
        return False, ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "dir_names": sorted(self.dir_names),
            "suffixes": sorted(self.suffixes),
            "min_file_size": self.min_file_size,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExcludeRules":
        return cls(
            dir_names=frozenset(data.get("dir_names", data.get("exclude_dirs", DEFAULT_EXCLUDE_DIR_NAMES))),
            suffixes=frozenset(data.get("suffixes", data.get("exclude_suffixes", []))),
            min_file_size=int(data.get("min_file_size", 0)),
        )


def _normalize_suffix(suffix: str) -> str:
    suffix = suffix.strip().lower()
    if not suffix:
        return ""
    return suffix if suffix.startswith(".") else "." + suffix


def load_config(path: str | os.PathLike[str]) -> dict[str, Any]:
    """读取 JSON 配置（排除规则 / 观察目标等）。"""
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("配置文件必须是 JSON 对象")
    return data


def rules_from_config(config: dict[str, Any], base: Optional[ExcludeRules] = None) -> ExcludeRules:
    """把配置文件与默认规则合并（配置项为追加，不整体覆盖）。"""
    base = base or ExcludeRules()
    config_dirs = config.get("exclude_dirs", []) or []
    config_suffixes = config.get("exclude_suffixes", []) or []
    min_size = config.get("min_file_size", base.min_file_size)
    return ExcludeRules(
        dir_names=base.dir_names | frozenset(config_dirs),
        suffixes=base.suffixes | frozenset(config_suffixes),
        min_file_size=max(base.min_file_size, int(min_size or 0)),
    )


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path)).replace("/", "\\")


def _critical_roots() -> tuple[tuple[str, bool], ...]:
    """返回 (路径, 是否递归保护) 列表。"""
    roots: list[tuple[str, bool]] = []
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
    roots.append((windir, True))  # Windows / System32 均在其下
    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "ProgramData", "CommonProgramFiles"):
        value = os.environ.get(env_name)
        if value:
            roots.append((value, True))

    # 驱动器根目录（C:\ 等）与用户根目录只做精确匹配保护。
    for drive in _windows_drives():
        roots.append((drive, False))
    roots.append((os.environ.get("USERPROFILE") or str(Path.home()), False))
    system_drive = os.environ.get("SystemDrive")
    if system_drive:
        roots.append((system_drive + "\\Users", False))
    return tuple((p, recursive) for p, recursive in roots if p)


def _windows_drives() -> list[str]:
    if os.name != "nt":
        return []
    import string

    drives = []
    for letter in string.ascii_uppercase:
        candidate = f"{letter}:\\"
        if os.path.exists(candidate):
            drives.append(candidate)
    return drives


def is_critical_path(path: str | os.PathLike[str]) -> bool:
    """判断路径是否属于系统关键目录（禁止删除 / 移动）。"""
    if not path:
        return True
    normalized = _norm(str(path))
    for root, recursive in _critical_roots():
        root_norm = _norm(root)
        if normalized == root_norm:
            return True
        if recursive and normalized.startswith(root_norm.rstrip("\\") + "\\"):
            return True
    # 常见关键目录名（精确的目录段）作为额外保护。
    parts = [part.lower() for part in normalized.split("\\") if part]
    if parts and parts[-1] in _EXACT_CRITICAL_NAMES:
        return True
    return False


def classify_file(rel_path: str, name: str, size: Optional[int], mtime_ns: int = 0) -> tuple[str, str, str, str] | None:
    """对单个文件做保守分类。

    返回 (category, risk, action, reason)；None 表示无需建议。
    未知 / 大文件只给复核建议，绝不自动处理。
    """
    suffix = os.path.splitext(name)[1].lower()
    parts = [part.lower() for part in rel_path.replace("\\", "/").split("/") if part]
    parent_parts = set(parts[:-1])
    size = size or 0

    if suffix in TEMP_SUFFIXES and parent_parts & set(TEMP_DIR_NAMES):
        return (
            "临时文件",
            RiskLevel.LOW.value,
            ActionKind.QUARANTINE.value,
            f"临时目录中的 {suffix} 文件，内容可由应用重新生成",
        )
    if suffix in TEMP_SUFFIXES and size >= 1024:
        return (
            "日志/备份类",
            RiskLevel.MEDIUM.value,
            ActionKind.QUARANTINE.value,
            f"{suffix} 文件，可能是日志或旧备份，隔离后仍可恢复",
        )
    if parent_parts & set(TEMP_DIR_NAMES) and size >= 1024:
        return (
            "缓存目录",
            RiskLevel.MEDIUM.value,
            ActionKind.QUARANTINE.value,
            "位于缓存/临时目录，建议隔离而非删除",
        )
    if size >= 1024 * 1024 * 1024:
        return (
            "超大文件",
            RiskLevel.HIGH.value,
            ActionKind.REVIEW.value,
            f"单文件 {human_size(size)}，用途未知，仅建议人工复核",
        )
    return None


def _rec_id(category: str, path: str) -> str:
    digest = hashlib.sha1(f"{category}|{path}".encode("utf-8", "ignore")).hexdigest()
    return digest[:12]


def generate_recommendations(
    store: "SnapshotStore",
    scan_id: str,
    duplicates: Optional[DuplicateReport] = None,
    min_benefit: int = 1024,
    max_items: int = 200,
) -> list[Recommendation]:
    """基于扫描证据生成只读/隔离类建议（不包含自动删除）。"""
    scan = store.get_scan(scan_id)
    if scan is None:
        raise ValueError(f"扫描不存在: {scan_id}")

    recommendations: list[Recommendation] = []

    if duplicates is not None:
        for group in duplicates.groups:
            keep = None
            unique_seen: set[tuple[int, int]] = set()
            for dup in group.files:
                key = (dup.device, dup.inode)
                if key in unique_seen:
                    continue
                unique_seen.add(key)
                if keep is None:
                    keep = dup
                    continue
                recommendations.append(
                    Recommendation(
                        rec_id=_rec_id("重复文件", dup.path),
                        action=ActionKind.QUARANTINE.value,
                        path=dup.path,
                        rel_path=dup.rel_path,
                        category="重复文件",
                        reason=f"与 {keep.rel_path} 内容相同，可隔离冗余副本",
                        evidence=(
                            f"SHA-256 {group.sha256[:16]}… 一致；大小 {human_size(group.size)}；"
                            f"保留 1 份，可回收 {human_size(group.size)}"
                        ),
                        benefit_bytes=group.size,
                        risk=RiskLevel.LOW.value,
                    )
                )

    for entry in store.iter_entries(scan_id, status="scanned", kind="file"):
        classified = classify_file(entry.rel_path, entry.name, entry.size, entry.mtime_ns)
        if classified is None:
            continue
        category, risk, action, reason = classified
        benefit = entry.size or 0
        if benefit < min_benefit:
            continue
        recommendations.append(
            Recommendation(
                rec_id=_rec_id(category, entry.path),
                action=action,
                path=entry.path,
                rel_path=entry.rel_path,
                category=category,
                reason=reason,
                evidence=f"大小 {human_size(entry.size)}；mtime_ns={entry.mtime_ns}；路径证据来自扫描快照 {scan_id[:12]}",
                benefit_bytes=benefit,
                risk=risk,
                confidence=0.6 if action == ActionKind.REVIEW.value else 0.9,
                requires_review=action == ActionKind.REVIEW.value or risk == RiskLevel.HIGH.value,
            )
        )

    alerts = evaluate_watch_targets(store, scan_id)
    for alert in alerts:
        if not alert.exceeded:
            continue
        recommendations.append(
            Recommendation(
                rec_id=_rec_id("增长异常", alert.rel_path),
                action=ActionKind.REVIEW.value,
                path=os.path.join(scan.root, alert.rel_path),
                rel_path=alert.rel_path,
                category="增长异常",
                reason=f"目录相对基线增长 {human_size(alert.growth)}，超过阈值 {human_size(alert.threshold_bytes)}",
                evidence=f"基线 {human_size(alert.baseline_bytes)} -> 当前 {human_size(alert.current_bytes)}（扫描 {scan_id[:12]}）",
                benefit_bytes=alert.growth,
                risk=RiskLevel.MEDIUM.value,
                confidence=0.8,
                requires_review=True,
            )
        )

    recommendations.sort(key=lambda rec: (-rec.benefit_bytes, rec.category, rec.path))
    return recommendations[:max_items]


def add_watch_target(
    store: "SnapshotStore",
    rel_path: str,
    threshold_bytes: int,
    scan_id: Optional[str] = None,
) -> WatchTarget:
    """为目录设置增长观察目标，基线取当前最新快照中的目录大小。"""
    rel_path = rel_path.replace("\\", "/").strip("/")
    if not rel_path:
        raise ValueError("观察目标不能为空")
    if threshold_bytes <= 0:
        raise ValueError("阈值必须为正数")
    if scan_id is None:
        latest = store.latest_scan()
        scan_id = latest.scan_id if latest else None
    baseline = 0
    if scan_id:
        baseline = store.get_directory_size(scan_id, rel_path) or 0
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    store.upsert_watch_target(rel_path, int(threshold_bytes), baseline, created_at)
    return WatchTarget(rel_path=rel_path, threshold_bytes=int(threshold_bytes), baseline_bytes=baseline, created_at=created_at)


def evaluate_watch_targets(store: "SnapshotStore", scan_id: str) -> list[WatchAlert]:
    alerts: list[WatchAlert] = []
    for target in store.list_watch_targets():
        current = store.get_directory_size(scan_id, target.rel_path) or 0
        alerts.append(
            WatchAlert(
                rel_path=target.rel_path,
                threshold_bytes=target.threshold_bytes,
                baseline_bytes=target.baseline_bytes,
                current_bytes=current,
                scan_id=scan_id,
            )
        )
    return alerts


def iter_recommendation_paths(recs: Iterable[Recommendation]) -> list[str]:
    return [rec.path for rec in recs if rec.action != ActionKind.REVIEW.value]
