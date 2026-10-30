"""运行时版本管理：探测、登记、默认版本选择与激活脚本生成。

只读探测不会修改系统；激活脚本仅影响当前终端会话，不写注册表、不改系统 PATH。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

from .db import Database
from .errors import NotFoundError, ValidationError
from .models import RUNTIME_KINDS, DetectedRuntime, RuntimeEntry, now_iso, parse_version

COMMANDS: dict[str, tuple[str, ...]] = {
    "python": ("python.exe", "python", "python3.exe", "python3"),
    "node": ("node.exe", "node"),
    "java": ("java.exe", "java"),
    "custom": (),
}

WhichFn = Callable[[str], str | None]
ProbeFn = Callable[[str, str], tuple[str, str]]


def _version_key(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for token in version.replace("-", ".").replace("_", ".").split("."):
        digits = "".join(ch for ch in token if ch.isdigit())
        if digits:
            parts.append(int(digits))
        elif parts:
            break
    return tuple(parts)


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))
    except OSError:
        return os.path.normcase(a) == os.path.normcase(b)


def _default_probe(exe_path: str, kind: str) -> tuple[str, str]:
    """调用运行时自身的 --version；超时 5 秒，失败返回空版本而不是抛出。"""
    attempts: list[list[str]]
    if kind == "java":
        attempts = [[exe_path, "--version"], [exe_path, "-version"]]
    elif kind == "python":
        attempts = [[exe_path, "--version"], [exe_path, "-V"]]
    else:
        attempts = [[exe_path, "--version"]]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    for args in attempts:
        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=5,
                errors="replace",
                creationflags=creationflags,
                check=False,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        output = (completed.stdout or "") + (completed.stderr or "")
        version = parse_version(output)
        if version:
            return version, output.strip()
    return "", ""


def _quote_powershell(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def build_activation_script(
    entries: Sequence[RuntimeEntry],
    extra_vars: dict[str, str] | None = None,
    project_path: str | None = None,
) -> str:
    """生成仅作用于当前 PowerShell 会话的激活脚本。"""
    prefix: list[str] = []
    for entry in entries:
        directory = str(Path(entry.exe_path).parent)
        if directory and directory not in prefix:
            prefix.append(directory)
    lines = [
        "# EnvForge 激活脚本（仅当前 PowerShell 会话生效，不修改系统环境变量）",
    ]
    if project_path:
        lines.append(f"$env:ENVFORGE_PROJECT = {_quote_powershell(project_path)}")
    if prefix:
        joined = ";".join(prefix)
        lines.append(f"$env:PATH = {_quote_powershell(joined + ';')} + $env:PATH")
    for key, value in (extra_vars or {}).items():
        lines.append(f"$env:{key} = {_quote_powershell(value)}")
    if entries:
        lines.append("")
        lines.append("# 当前会话运行时版本：")
        for entry in entries:
            lines.append(f"#   {entry.kind} {entry.version or '未知'} -> {entry.exe_path}")
    return "\n".join(lines) + "\n"


class RuntimeService:
    def __init__(
        self,
        db: Database,
        which_fn: WhichFn | None = None,
        probe_fn: ProbeFn | None = None,
    ) -> None:
        self.db = db
        self.which = which_fn or shutil.which
        self.probe = probe_fn or _default_probe

    # ------------------------------------------------------------------ 探测
    def detect(self, kinds: Iterable[str] | None = None) -> list[DetectedRuntime]:
        selected = list(kinds) if kinds else ["python", "node", "java"]
        found: list[DetectedRuntime] = []
        seen: list[str] = []
        for kind in selected:
            if kind not in RUNTIME_KINDS:
                raise ValidationError(f"不支持的运行时类型 {kind!r}；可选：{', '.join(RUNTIME_KINDS)}")
            for command in COMMANDS.get(kind, ()):
                path = self.which(command)
                if not path:
                    continue
                if any(_same_file(path, item) for item in seen):
                    continue
                seen.append(path)
                version, output = self.probe(path, kind)
                if not version:
                    # 探测不到版本的条目（如 WindowsApps 的 python 存根）不自动登记，
                    # 避免把不可用的占位程序当成本地运行时。
                    continue
                found.append(
                    DetectedRuntime(kind=kind, version=version, exe_path=path, probe_output=output)
                )
        return found

    def detect_and_register(self, kinds: Iterable[str] | None = None) -> dict[str, Any]:
        detected = self.detect(kinds)
        added = 0
        updated = 0
        for item in detected:
            existed = self._find(item.kind, item.exe_path)
            self.register(item.kind, item.exe_path, item.version, source="detected")
            if existed:
                updated += 1
            else:
                added += 1
        return {"detected": [item.to_dict() for item in detected], "added": added, "updated": updated}

    # ------------------------------------------------------------------ 登记
    def register(
        self,
        kind: str,
        exe_path: str,
        version: str | None = None,
        *,
        notes: str = "",
        source: str = "manual",
    ) -> RuntimeEntry:
        if kind not in RUNTIME_KINDS:
            raise ValidationError(f"不支持的运行时类型 {kind!r}；可选：{', '.join(RUNTIME_KINDS)}")
        raw = (exe_path or "").strip().strip('"')
        if not raw:
            raise ValidationError("运行时路径不能为空。")
        path = Path(raw).expanduser()
        if not path.exists():
            raise ValidationError(f"路径不存在：{path}；请确认安装位置后再登记。")
        if path.is_dir():
            raise ValidationError(
                f"{path} 是目录而不是可执行文件；请登记具体的 exe（如 python.exe）。"
            )
        normalized = str(path.resolve(strict=False))
        existing = self._find(kind, normalized)
        resolved_version = version if version is not None else self.probe(normalized, kind)[0]
        version = parse_version(resolved_version) or (resolved_version or "").strip()
        if not version and existing and existing["version"]:
            # 探测失败时保留旧版本号，避免把已知信息覆盖成空值。
            version = str(existing["version"])
        now = now_iso()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO runtimes(kind, version, exe_path, source, notes, created_at)"
                " VALUES(?,?,?,?,?,?)"
                " ON CONFLICT(kind, exe_path) DO UPDATE SET version = excluded.version,"
                " source = excluded.source, notes = excluded.notes",
                (kind, version, normalized, source, notes, now),
            )
        row = self._find(kind, normalized)
        assert row is not None
        return self._to_entry(row)

    def remove(self, runtime_id: int) -> None:
        with self.db.transaction() as conn:
            cursor = conn.execute("DELETE FROM runtimes WHERE id = ?", (runtime_id,))
        if cursor.rowcount == 0:
            raise NotFoundError(f"运行时记录 {runtime_id} 不存在。")

    def list(self, kind: str | None = None) -> list[RuntimeEntry]:
        if kind is not None and kind not in RUNTIME_KINDS:
            raise ValidationError(f"不支持的运行时类型 {kind!r}。")
        if kind:
            rows = self.db.query(
                "SELECT * FROM runtimes WHERE kind = ? ORDER BY version DESC, exe_path", (kind,)
            )
        else:
            rows = self.db.query("SELECT * FROM runtimes ORDER BY kind, version DESC, exe_path")
        return [self._to_entry(row) for row in rows]

    def default_entries(self) -> list[RuntimeEntry]:
        """每个类型取版本号最高的已登记运行时。"""
        best: dict[str, RuntimeEntry] = {}
        for entry in self.list():
            current = best.get(entry.kind)
            if current is None or _version_key(entry.version) > _version_key(current.version):
                best[entry.kind] = entry
        return [best[kind] for kind in RUNTIME_KINDS if kind in best]

    def activation_script(
        self,
        project_path: str | None = None,
        extra_vars: dict[str, str] | None = None,
    ) -> str:
        return build_activation_script(self.default_entries(), extra_vars, project_path)

    # ------------------------------------------------------------------ 内部
    def _find(self, kind: str, exe_path: str) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM runtimes WHERE kind = ? AND exe_path = ?", (kind, str(exe_path))
        )

    @staticmethod
    def _to_entry(row: dict[str, Any]) -> RuntimeEntry:
        return RuntimeEntry(
            id=row["id"],
            kind=row["kind"],
            version=row["version"],
            exe_path=row["exe_path"],
            source=row["source"],
            notes=row["notes"],
            created_at=row["created_at"],
        )
