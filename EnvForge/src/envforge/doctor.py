"""环境健康检查：只读诊断，不修改任何系统状态。"""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

from .db import Database
from .envvars import analyze_path, scope_label
from .envstore import EnvService
from .errors import EnvAccessError
from .models import Finding
from .profiles import ProjectService
from .runtimes import RuntimeService

_SEVERITY_ORDER = {"error": 0, "warn": 1, "info": 2}
_TOOL_FILES = {"python": ("python.exe", "python"), "node": ("node.exe", "node"), "java": ("java.exe", "java")}


def _first_in_path(path_value: str, filename: str) -> str | None:
    for entry in analyze_path(path_value):
        if not entry.raw:
            continue
        candidate = Path(entry.expanded) / filename
        if candidate.is_file():
            return str(candidate)
    return None


class Doctor:
    def __init__(
        self,
        db: Database,
        env: EnvService,
        runtimes: RuntimeService | None = None,
        projects: ProjectService | None = None,
    ) -> None:
        self.db = db
        self.env = env
        self.runtimes = runtimes or RuntimeService(db)
        self.projects = projects or ProjectService(db)

    def run(self, scopes: Iterable[str] = ("user", "system")) -> list[Finding]:
        findings: list[Finding] = []
        for scope in scopes:
            try:
                findings.extend(self._check_path(scope))
            except EnvAccessError as exc:
                findings.append(
                    Finding(
                        code=f"env-access-{scope}",
                        severity="warn",
                        title=f"无法读取{scope_label(scope)}环境变量",
                        detail=str(exc),
                        suggestion="以当前用户运行，或检查注册表访问权限；也可改用 scope=session 自检。",
                    )
                )
        findings.extend(self._check_runtimes(scopes))
        findings.extend(self._check_profiles())
        findings.extend(self._check_projects())
        findings.sort(key=lambda item: _SEVERITY_ORDER.get(item.severity, 3))
        return findings

    # ------------------------------------------------------------------ PATH
    def _check_path(self, scope: str) -> list[Finding]:
        findings: list[Finding] = []
        snapshot = self.env.current(scope)
        path_value = ""
        for key, value in snapshot.values.items():
            if key.upper() == "PATH":
                path_value = value
                break
        if not path_value:
            return [
                Finding(
                    code=f"path-missing-{scope}",
                    severity="warn",
                    title=f"{scope_label(scope)} PATH 为空或不存在",
                    detail="未找到 PATH 变量，命令可能无法在终端中被发现。",
                    suggestion="检查系统设置中的环境变量，或使用 EnvForge 创建快照后手动修复。",
                )
            ]
        entries = analyze_path(path_value)
        duplicates = [entry for entry in entries if entry.duplicate_of is not None]
        missing = [entry for entry in entries if entry.raw and not entry.exists]
        empty = [entry for entry in entries if not entry.raw]
        if duplicates:
            findings.append(
                Finding(
                    code=f"path-duplicate-{scope}",
                    severity="warn",
                    title=f"{scope_label(scope)} PATH 存在 {len(duplicates)} 个重复目录",
                    detail="重复条目会拖慢命令查找，并让实际生效顺序难以判断："
                    + "、".join(dict.fromkeys(entry.expanded for entry in duplicates)),
                    suggestion="可运行 `envforge env dedupe --scope user` 预览去重结果，再确认写入。",
                )
            )
        if missing:
            findings.append(
                Finding(
                    code=f"path-missing-dir-{scope}",
                    severity="warn",
                    title=f"{scope_label(scope)} PATH 有 {len(missing)} 个目录不存在",
                    detail="失效目录不会报错，但会让排错变困难："
                    + "、".join(entry.raw for entry in missing[:5]),
                    suggestion="确认对应软件是否已卸载；如已卸载，考虑从 PATH 中移除。",
                )
            )
        if empty:
            findings.append(
                Finding(
                    code=f"path-empty-{scope}",
                    severity="info",
                    title=f"{scope_label(scope)} PATH 含空段",
                    detail="空段在某些程序中等价于当前目录，存在被同名程序劫持的风险。",
                    suggestion="建议删除空段（连续的分号）。",
                )
            )
        return findings

    # ------------------------------------------------------------------ 运行时
    def _check_runtimes(self, scopes: Iterable[str]) -> list[Finding]:
        findings: list[Finding] = []
        registered = self.runtimes.list()
        if not registered:
            findings.append(
                Finding(
                    code="runtime-none",
                    severity="info",
                    title="尚未登记任何运行时",
                    detail="无法基于登记记录生成激活脚本或做版本对比。",
                    suggestion="执行 `envforge runtime detect` 自动登记 PATH 中已安装的 Python / Node / JDK。",
                )
            )
        for entry in registered:
            if not Path(entry.exe_path).is_file():
                findings.append(
                    Finding(
                        code="runtime-missing",
                        severity="error",
                        title=f"已登记运行时不存在：{entry.kind} {entry.version}",
                        detail=f"路径 {entry.exe_path} 已不可用，激活脚本会注入失效目录。",
                        suggestion="移除该记录（runtime remove）或重新登记安装路径。",
                    )
                )
        by_kind: dict[str, list[str]] = defaultdict(list)
        for entry in registered:
            by_kind[entry.kind].append(entry.version or "未知")
        default = {entry.kind: entry for entry in self.runtimes.default_entries()}
        for kind, versions in by_kind.items():
            if len(versions) > 1:
                chosen = default.get(kind)
                findings.append(
                    Finding(
                        code="runtime-multi",
                        severity="info",
                        title=f"{kind} 存在多个版本",
                        detail=f"已登记版本：{', '.join(sorted(set(versions)))}；"
                        f"激活时默认使用 {chosen.version if chosen else '未知'}。",
                        suggestion="如需切换，请在登记顺序中移除不用的版本，或按项目激活时手动调整 PATH。",
                    )
                )
        for scope in scopes:
            try:
                path_value = ""
                for key, value in self.env.current(scope).values.items():
                    if key.upper() == "PATH":
                        path_value = value
                        break
            except EnvAccessError:
                continue
            for kind, filenames in _TOOL_FILES.items():
                first = None
                for filename in filenames:
                    first = _first_in_path(path_value, filename)
                    if first:
                        break
                if not first:
                    continue
                same = any(
                    os.path.normcase(os.path.realpath(entry.exe_path))
                    == os.path.normcase(os.path.realpath(first))
                    for entry in registered
                    if entry.kind == kind
                )
                if not same:
                    findings.append(
                        Finding(
                            code=f"runtime-unregistered-{kind}-{scope}",
                            severity="info",
                            title=f"{scope_label(scope)} PATH 中的 {kind} 未登记",
                            detail=f"PATH 优先命中的是 {first}，但该路径不在登记表中。",
                            suggestion="登记该运行时，或在界面中对比版本后决定是否切换。",
                        )
                    )
        return findings

    # ------------------------------------------------------------------ 档案
    def _check_profiles(self) -> list[Finding]:
        findings: list[Finding] = []
        rows = self.db.query(
            "SELECT pv.key, p.name AS profile, pj.name AS project FROM profile_vars pv"
            " JOIN profiles p ON p.id = pv.profile_id JOIN projects pj ON pj.id = p.project_id"
            " WHERE pv.sensitive = 1 AND pv.encrypted = 0"
        )
        if rows:
            names = "、".join(f"{row['project']}/{row['profile']}:{row['key']}" for row in rows[:5])
            findings.append(
                Finding(
                    code="profile-secret-unencrypted",
                    severity="warn",
                    title=f"有 {len(rows)} 个敏感变量未加密存储",
                    detail=f"这些变量只有键名，没有可用的值：{names}",
                    suggestion="在支持 DPAPI 的 Windows 环境重新设置这些变量。",
                )
            )
        return findings

    def _check_projects(self) -> list[Finding]:
        findings: list[Finding] = []
        for project in self.projects.list():
            if not Path(project.path).is_dir():
                findings.append(
                    Finding(
                        code="project-path-missing",
                        severity="warn",
                        title=f"项目路径不存在：{project.name}",
                        detail=f"{project.path} 已被移动或删除，激活脚本中的项目路径将无效。",
                        suggestion="更新项目路径或删除该记录。",
                    )
                )
        return findings


def summarize(findings: Iterable[Finding]) -> dict[str, int]:
    counts = {"error": 0, "warn": 0, "info": 0}
    for item in findings:
        counts[item.severity] = counts.get(item.severity, 0) + 1
    return counts
