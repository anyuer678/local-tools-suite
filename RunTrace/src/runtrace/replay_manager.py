"""复现包生成、校验与需显式确认的复现执行。"""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import sys
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .command_builder import RunConfig
from .process_runner import ProcessRunner, RunOutcome
from .redaction import REDACTED, scan_sensitive_content
from .run_store import RunStore

_MAX_UNCOMPRESSED = 200 * 1024 * 1024
_REQUIRED_ENTRIES = {
    "manifest.json",
    "run.json",
    "config.json",
    "stdout.log",
    "stderr.log",
    "checksums.json",
    "REPRODUCE.md",
}


class ReplayError(RuntimeError):
    """复现包无效或无法执行。"""


class ReplayConfirmationRequired(ReplayError):
    """执行复现包前必须由用户显式确认。"""


@dataclass(slots=True)
class ReplayPackageInfo:
    path: str
    run_id: str
    warnings: list[str]
    environment_gaps: list[str]
    external_dependencies: list[dict[str, Any]]
    checksums: dict[str, str]
    sensitive_findings: list[dict[str, str]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "run_id": self.run_id,
            "warnings": list(self.warnings),
            "environment_gaps": list(self.environment_gaps),
            "external_dependencies": list(self.external_dependencies),
            "checksums": dict(self.checksums),
            "sensitive_findings": list(self.sensitive_findings),
        }


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _external_dependencies(command: str, shell: bool) -> list[dict[str, Any]]:
    resolved = None
    command_path = Path(command)
    if command_path.is_file():
        resolved = str(command_path.resolve())
    else:
        resolved = shutil.which(command)
    deps: list[dict[str, Any]] = [
        {
            "name": command,
            "resolved_path": resolved,
            "packaged": False,
            "note": (
                "命令来自系统 PATH 或项目目录，复现包不包含可执行文件，"
                "目标机器必须自行提供。"
            ),
        }
    ]
    if shell:
        deps.append(
            {
                "name": "shell",
                "resolved_path": None,
                "packaged": False,
                "note": "shell 模式依赖系统 shell 的解析规则（Windows 上通常为 cmd.exe）。",
            }
        )
    deps.append(
        {
            "name": "python",
            "resolved_path": sys.executable,
            "packaged": False,
            "note": f"RunTrace 记录使用的 Python 运行时；当前 {sys.version.split()[0]}，不随包分发。",
        }
    )
    return deps


def _environment_gaps(run: dict[str, Any]) -> list[str]:
    redacted = sorted(
        set(run.get("metadata", {}).get("redacted_env_keys") or [])
        | {
            key
            for key, value in (run.get("env") or {}).items()
            if value == REDACTED
        }
    )
    gaps = [
        "敏感环境变量值已脱敏，复现执行时不会自动还原；需在目标环境重新提供。",
        "未记录交互式输入、终端尺寸、剪贴板、网络状态和外部服务状态。",
        "记录的环境是运行时的合并快照；系统 PATH、依赖版本或项目文件在复现时可能已变化。",
        "仅保存命令可执行文件之外的命令文本，第三方程序及系统库不会被打包。",
        "日志使用 UTF-8 容错解码；非 UTF-8 或二进制输出可能丢失字节细节。",
    ]
    if redacted:
        gaps.append("被脱敏的键名：" + ", ".join(redacted))
    if run.get("shell"):
        gaps.append("该运行启用了 shell，复现时会再次经过 shell 解释，需自行确认命令安全。")
    return gaps


def _build_manifest(
    run: dict[str, Any],
    *,
    config: dict[str, Any],
    checksums: dict[str, str],
    environment_gaps: list[str],
    external_dependencies: list[dict[str, Any]],
    findings: list[dict[str, str]],
) -> dict[str, Any]:
    warnings = [
        "复现包不包含可执行文件和第三方依赖。",
        "脱敏环境变量无法自动还原。",
        "执行前必须人工确认 cwd、命令、参数和 shell 风险。",
    ]
    if findings:
        warnings.append("日志/命令中发现疑似敏感内容，请勿在未确认的情况下分享复现包。")
    if run.get("shell"):
        warnings.append("该任务使用 shell 模式，命令字符串可能包含注入风险。")
    return {
        "schema": "runtrace.replay.v1",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
        "tool_version": __version__,
        "platform": platform.platform(),
        "python": sys.version,
        "run_id": run["id"],
        "name": run["name"],
        "confirmation_required": True,
        "command": run["command"],
        "args": run["args"],
        "cwd": run["cwd"],
        "shell": bool(run["shell"]),
        "timeout": run["timeout"],
        "project_path": run.get("project_path"),
        "git": run.get("git_context"),
        "redacted_env_keys": sorted(
            set(run.get("metadata", {}).get("redacted_env_keys") or [])
            | {k for k, v in (run.get("env") or {}).items() if v == REDACTED}
        ),
        "environment_gaps": environment_gaps,
        "external_dependencies": external_dependencies,
        "warnings": warnings,
        "sensitive_findings": findings,
        "checksums": checksums,
        "config": config,
    }


class ReplayManager:
    def __init__(self, store: RunStore) -> None:
        self.store = store

    def build_package(
        self, run_id: str, output_path: str | Path
    ) -> ReplayPackageInfo:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(f"运行记录不存在：{run_id}")
        stdout = self.store.get_logs(run_id, "stdout")
        stderr = self.store.get_logs(run_id, "stderr")
        findings: list[dict[str, str]] = []
        findings.extend(scan_sensitive_content(stdout, context="stdout"))
        findings.extend(scan_sensitive_content(stderr, context="stderr"))
        findings.extend(scan_sensitive_content(run["command"], context="command"))
        findings.extend(scan_sensitive_content(" ".join(run.get("args", [])), context="args"))
        env_gaps = _environment_gaps(run)
        deps = _external_dependencies(run["command"], bool(run["shell"]))
        config = {
            "name": run["name"],
            "command": run["command"],
            "args": run["args"],
            "cwd": run["cwd"],
            "shell": bool(run["shell"]),
            "timeout": run["timeout"],
            "project_path": run.get("project_path"),
            "env_overrides": run.get("env_overrides") or {},
            "environment": run.get("env") or {},
            "redacted_env_keys": sorted(
                set(run.get("metadata", {}).get("redacted_env_keys") or [])
                | {k for k, v in (run.get("env") or {}).items() if v == REDACTED}
            ),
        }
        run_json = json.dumps(run, ensure_ascii=False, indent=2)
        config_json = json.dumps(config, ensure_ascii=False, indent=2)
        checksums = {
            "run.json": _sha256(run_json),
            "config.json": _sha256(config_json),
            "stdout.log": _sha256(stdout),
            "stderr.log": _sha256(stderr),
            "REPRODUCE.md": "",  # 写入前统一计算
        }
        reproduce = self._instructions(run, env_gaps, deps)
        checksums["REPRODUCE.md"] = _sha256(reproduce)
        manifest = _build_manifest(
            run,
            config=config,
            checksums=checksums,
            environment_gaps=env_gaps,
            external_dependencies=deps,
            findings=findings,
        )
        manifest["checksums"] = checksums
        manifest_json = json.dumps(manifest, ensure_ascii=False, indent=2)
        checksums_json = json.dumps(checksums, ensure_ascii=False, indent=2)

        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.writestr("manifest.json", manifest_json)
            zf.writestr("run.json", run_json)
            zf.writestr("config.json", config_json)
            zf.writestr("stdout.log", stdout)
            zf.writestr("stderr.log", stderr)
            zf.writestr("checksums.json", checksums_json)
            zf.writestr("REPRODUCE.md", reproduce)
        return ReplayPackageInfo(
            path=str(target),
            run_id=run_id,
            warnings=list(manifest["warnings"]),
            environment_gaps=env_gaps,
            external_dependencies=deps,
            checksums=checksums,
            sensitive_findings=findings,
        )

    @staticmethod
    def _instructions(
        run: dict[str, Any],
        env_gaps: list[str],
        deps: list[dict[str, Any]],
    ) -> str:
        args_display = subprocess_list2cmdline(run.get("args", []))
        return "\n".join(
            [
                "# RunTrace 复现说明",
                "",
                f"- Run ID: `{run['id']}`",
                f"- 名称: {run['name']}",
                f"- 命令: `{run['command']}`",
                f"- 参数: `{args_display}`",
                f"- 工作目录: `{run['cwd']}`",
                f"- Shell: {run['shell']}",
                f"- 退出码: {run['exit_code']}",
                "",
                "## 执行前检查",
                "",
                "1. 确认工作目录和命令来自可信来源。",
                "2. 补齐被脱敏的环境变量（见 manifest 的 redacted_env_keys）。",
                "3. 确认外部依赖可用（见 manifest 的 external_dependencies）。",
                "4. 如启用 shell，先人工检查完整命令字符串。",
                "",
                "## 未记录/无法保证的内容",
                "",
                *[f"- {item}" for item in env_gaps],
                "",
                "## 外部依赖",
                "",
                *[
                    f"- {dep['name']} -> {dep['resolved_path'] or '未解析'}：{dep['note']}"
                    for dep in deps
                ],
                "",
                "RunTrace 执行复现包时需要显式确认（CLI 使用 --yes），不会自动运行。",
            ]
        )

    def inspect_package(self, package_path: str | Path) -> dict[str, Any]:
        path = Path(package_path)
        if not path.is_file():
            raise ReplayError(f"复现包不存在：{path}")
        try:
            with zipfile.ZipFile(path, "r") as zf:
                names = set(zf.namelist())
                if not names:
                    raise ReplayError("复现包为空")
                missing = sorted(_REQUIRED_ENTRIES - names)
                if missing:
                    raise ReplayError("缺少必需文件：" + ", ".join(missing))
                total = sum(info.file_size for info in zf.infolist())
                if total > _MAX_UNCOMPRESSED:
                    raise ReplayError("复现包解压后体积超过安全上限")
                bad_names = [
                    name
                    for name in names
                    if Path(name).is_absolute() or ".." in Path(name).parts
                ]
                if bad_names:
                    raise ReplayError("复现包含不安全的路径名：" + ", ".join(bad_names))
                data = {name: zf.read(name) for name in _REQUIRED_ENTRIES}
        except zipfile.BadZipFile as exc:
            raise ReplayError(f"不是有效 zip：{exc}") from exc

        try:
            manifest = json.loads(data["manifest.json"].decode("utf-8"))
            checksums = json.loads(data["checksums.json"].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReplayError(f"复现包 JSON 损坏：{exc}") from exc
        if manifest.get("schema") != "runtrace.replay.v1":
            raise ReplayError("未知的复现包 schema")
        if not manifest.get("confirmation_required"):
            raise ReplayError("复现包未声明需要确认，拒绝使用")
        verified: dict[str, bool] = {}
        for name, expected in checksums.items():
            if name not in data:
                verified[name] = False
                continue
            actual = hashlib.sha256(data[name]).hexdigest()
            verified[name] = actual == expected
        if not all(verified.values()):
            bad = [name for name, ok in verified.items() if not ok]
            raise ReplayError("复现包校验失败：" + ", ".join(bad))
        return {
            "path": str(path),
            "manifest": manifest,
            "config": manifest.get("config") or json.loads(data["config.json"].decode("utf-8")),
            "verified": verified,
            "checksums_valid": all(verified.values()),
            "run_id": manifest.get("run_id"),
        }

    def execute_package(
        self,
        package_path: str | Path,
        *,
        confirmed: bool = False,
        on_output: Any = None,
    ) -> RunOutcome:
        if not confirmed:
            raise ReplayConfirmationRequired(
                "执行复现包需要显式确认（confirmed=True / CLI --yes）；"
                "请先检查命令、工作目录、shell 风险和环境缺口。"
            )
        info = self.inspect_package(package_path)
        manifest = info["manifest"]
        config = manifest.get("config") or {}
        cwd = str(config.get("cwd") or manifest.get("cwd") or "")
        if not Path(cwd).is_dir():
            raise ReplayError(f"复现工作目录不存在：{cwd}")
        command = str(config.get("command") or manifest.get("command") or "")
        if not command:
            raise ReplayError("复现包没有可执行命令")
        run_config = RunConfig(
            name=f"复现 {manifest.get('name') or manifest.get('run_id')}",
            cwd=cwd,
            command=command,
            args=[str(x) for x in config.get("args") or manifest.get("args") or []],
            env_overrides={
                str(key): str(value)
                for key, value in (config.get("env_overrides") or {}).items()
                if value != REDACTED
            },
            timeout=config.get("timeout") if config.get("timeout") is not None else manifest.get("timeout"),
            shell=bool(config.get("shell", manifest.get("shell"))),
            project_path=config.get("project_path") or manifest.get("project_path"),
        )
        runner = ProcessRunner(self.store)
        outcome = runner.run(run_config, on_output=on_output)
        return outcome


def subprocess_list2cmdline(args: list[str]) -> str:
    """显示复现参数，不依赖子进程执行。"""
    if not args:
        return ""
    if sys.platform == "win32":
        import subprocess

        return subprocess.list2cmdline(args)
    import shlex

    return " ".join(shlex.quote(str(item)) for item in args)
