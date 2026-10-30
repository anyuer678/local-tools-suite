"""领域数据模型：全部为纯数据对象，不依赖数据库与界面。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

RUNTIME_KINDS = ("python", "node", "java", "custom")
ENV_SCOPES = ("user", "system", "session")
SEVERITIES = ("error", "warn", "info")

_VERSION_RE = re.compile(r"\d+(?:\.\d+)+(?:[.\-+~][0-9A-Za-z.\-+]+)?")
_SECRET_KEY_RE = re.compile(
    r"(SECRET|TOKEN|PASSWORD|PASSWD|PWD|API[_-]?KEY|PRIVATE[_-]?KEY|ACCESS[_-]?KEY"
    r"|SIGNING[_-]?KEY|ENCRYPTION[_-]?KEY|DATABASE[_-]?URL|CONNECTION[_-]?STRING"
    r"|MONGO[_-]?URI|REDIS[_-]?URL|DSN|CREDENTIAL|AUTH|SALT)",
    re.IGNORECASE,
)


def now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_version(text: str | None) -> str:
    """从版本输出中提取第一个形如 3.12.3 / v20.11.0 / 17.0.9 的版本号。"""
    if not text:
        return ""
    match = _VERSION_RE.search(text)
    return match.group(0).lstrip("v") if match else ""


def looks_sensitive_key(key: str) -> bool:
    """按变量名判断是否属于敏感字段。"""
    return bool(_SECRET_KEY_RE.search(key or ""))


@dataclass(slots=True)
class RuntimeEntry:
    kind: str
    version: str
    exe_path: str
    source: str = "manual"
    notes: str = ""
    id: int | None = None
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "version": self.version,
            "exe_path": self.exe_path,
            "source": self.source,
            "notes": self.notes,
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class DetectedRuntime:
    kind: str
    version: str
    exe_path: str
    probe_output: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "version": self.version,
            "exe_path": self.exe_path,
            "probe_output": self.probe_output,
        }


@dataclass(slots=True)
class ProjectEntry:
    name: str
    path: str
    id: int | None = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "path": self.path,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class ProfileEntry:
    project_id: int
    name: str
    description: str = ""
    active: bool = False
    id: int | None = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "name": self.name,
            "description": self.description,
            "active": self.active,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class ProfileVar:
    key: str
    value: str
    sensitive: bool = False
    encrypted: bool = False
    stored: bool = True
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "sensitive": self.sensitive,
            "encrypted": self.encrypted,
            "stored": self.stored,
            "updated_at": self.updated_at,
        }


@dataclass(slots=True)
class VarChange:
    key: str
    before: str | None
    after: str | None
    sensitive: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "before": self.before,
            "after": self.after,
            "sensitive": self.sensitive,
        }


@dataclass(slots=True)
class PathEntry:
    index: int
    raw: str
    expanded: str
    exists: bool
    duplicate_of: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "raw": self.raw,
            "expanded": self.expanded,
            "exists": self.exists,
            "duplicate_of": self.duplicate_of,
        }


@dataclass(slots=True)
class EnvSnapshot:
    scope: str
    values: dict[str, str]
    path_entries: list[str] = field(default_factory=list)
    label: str = ""
    id: int | None = None
    created_at: str = ""

    def to_dict(self, *, reveal: bool = False) -> dict[str, Any]:
        from .secrets import mask_value

        values = {
            key: (value if (reveal or not looks_sensitive_key(key)) else mask_value(value))
            for key, value in self.values.items()
        }
        return {
            "id": self.id,
            "scope": self.scope,
            "label": self.label,
            "created_at": self.created_at,
            "values": values,
            "path_entries": list(self.path_entries),
            "var_count": len(self.values),
        }


@dataclass(slots=True)
class Finding:
    code: str
    severity: str
    title: str
    detail: str
    suggestion: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "suggestion": self.suggestion,
        }
