"""命令配置校验与安全预览。

默认为参数数组 + ``shell=False``。只有用户显式选择时才构造 shell 命令行，
并始终返回醒目的风险提示。
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path


class ConfigError(ValueError):
    """运行配置不合法。"""


@dataclass(slots=True)
class RunConfig:
    name: str
    cwd: str
    command: str
    args: list[str] = field(default_factory=list)
    env_overrides: dict[str, str] = field(default_factory=dict)
    timeout: float | None = None
    shell: bool = False
    project_path: str | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "cwd": self.cwd,
            "command": self.command,
            "args": list(self.args),
            "env_overrides": dict(self.env_overrides),
            "timeout": self.timeout,
            "shell": self.shell,
            "project_path": self.project_path,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RunConfig":
        return cls(
            name=str(data.get("name") or data.get("command") or "run"),
            cwd=str(data.get("cwd") or os.getcwd()),
            command=str(data.get("command") or ""),
            args=[str(x) for x in data.get("args") or []],
            env_overrides={str(k): str(v) for k, v in (data.get("env_overrides") or {}).items()},
            timeout=float(data["timeout"]) if data.get("timeout") is not None else None,
            shell=bool(data.get("shell", False)),
            project_path=data.get("project_path"),
        )

    def copy(self, **changes) -> "RunConfig":
        return replace(self, **changes)


@dataclass(slots=True)
class BuildResult:
    argv: list[str] | str
    shell: bool
    display: str
    warning: str | None
    cwd: str

    @property
    def is_shell(self) -> bool:
        return self.shell


SHELL_WARNING = (
    "已显式启用 shell 模式：命令字符串会交给系统 shell 解释，"
    "存在命令注入与转义风险。仅在你完全信任该命令时使用。"
)


def validate_config(config: RunConfig) -> RunConfig:
    """规范化并验证配置；不修改文件系统。"""
    if not isinstance(config, RunConfig):
        raise ConfigError("config 必须是 RunConfig")
    name = (config.name or "").strip()
    if not name:
        name = Path(config.command).name or "run"
    command = (config.command or "").strip()
    if not command:
        raise ConfigError("命令不能为空")
    if "\x00" in command or "\x00" in name:
        raise ConfigError("命令/名称不能包含 NUL 字符")
    if not isinstance(config.args, (list, tuple)):
        raise ConfigError("args 必须是字符串数组")
    args = [str(arg) for arg in config.args]
    if any("\x00" in arg for arg in args):
        raise ConfigError("参数不能包含 NUL 字符")
    cwd_raw = (config.cwd or "").strip()
    if not cwd_raw:
        raise ConfigError("工作目录不能为空")
    cwd = Path(cwd_raw).expanduser().resolve()
    if not cwd.is_dir():
        raise ConfigError(f"工作目录不存在或不是目录：{cwd}")
    timeout = config.timeout
    if timeout is not None:
        timeout = float(timeout)
        if timeout <= 0:
            raise ConfigError("超时必须大于 0 秒")
    env_overrides: dict[str, str] = {}
    for key, value in (config.env_overrides or {}).items():
        key_s = str(key)
        if not key_s or "=" in key_s or "\x00" in key_s:
            raise ConfigError(f"非法环境变量名：{key_s!r}")
        value_s = "" if value is None else str(value)
        if "\x00" in value_s:
            raise ConfigError(f"环境变量 {key_s} 的值不能包含 NUL 字符")
        env_overrides[key_s] = value_s
    project = config.project_path
    if project:
        project = str(Path(project).expanduser().resolve())
    return RunConfig(
        name=name,
        cwd=str(cwd),
        command=command,
        args=args,
        env_overrides=env_overrides,
        timeout=timeout,
        shell=bool(config.shell),
        project_path=project,
    )


def build_command(config: RunConfig) -> BuildResult:
    """构造 Popen 可用的参数数组或 shell 命令行，并生成预览。"""
    cfg = validate_config(config)
    if cfg.shell:
        if os.name == "nt":
            # 使用 Windows 命令行引用规则，避免逐参数简单拼接。
            command_line = subprocess.list2cmdline([cfg.command, *cfg.args])
        else:
            import shlex

            command_line = " ".join(shlex.quote(x) for x in [cfg.command, *cfg.args])
        return BuildResult(
            argv=command_line,
            shell=True,
            display=command_line,
            warning=SHELL_WARNING,
            cwd=cfg.cwd,
        )
    argv = [cfg.command, *cfg.args]
    display = subprocess.list2cmdline(argv) if os.name == "nt" else " ".join(argv)
    warning = None
    if cfg.command in {"echo", "cd", "set", "dir", "type", "copy", "del"}:
        warning = (
            f"“{cfg.command}”看起来是 shell 内建命令；默认参数数组模式不会经过 shell，"
            "请改用可执行程序，或显式启用 shell 模式并承担相应风险。"
        )
    return BuildResult(argv=argv, shell=False, display=display, warning=warning, cwd=cfg.cwd)


def env_overrides_from_pairs(pairs: list[str] | None) -> dict[str, str]:
    """解析 CLI/UI 的 ``KEY=VALUE`` 覆盖项。"""
    result: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise ConfigError(f"环境变量覆盖必须为 KEY=VALUE：{pair!r}")
        key, value = pair.split("=", 1)
        if not key:
            raise ConfigError("环境变量名不能为空")
        result[key] = value
    return result
