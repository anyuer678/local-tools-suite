"""插件宿主：清单解析、启用/禁用状态、独立子进程执行与错误隔离。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .command_registry import Command, CommandContext, CommandRegistry
from .plugin_api import API_VERSION, PluginCommand
from .plugin_runner import RESULT_MARKER


class PluginError(RuntimeError):
    pass


class PluginConsentRequired(PluginError):
    pass


class PluginDisabled(PluginError):
    pass


class PluginExecutionError(PluginError):
    pass


_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def build_runner_command(
    runner: str | Path,
    plugin_path: str | Path,
    *,
    frozen: bool | None = None,
) -> list[str]:
    """构造插件执行命令。

    源码运行时使用 ``python -I plugin_runner.py <插件目录>`` 隔离用户环境；
    冻结版没有外部 python，改为让程序自身以 ``--plugin-runner`` 模式启动，
    由打包入口分流到同一个 plugin_runner，仍保持独立进程与超时隔离。
    """
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        return [sys.executable, "--plugin-runner", str(plugin_path)]
    return [sys.executable, "-I", str(runner), str(plugin_path)]


def api_version_compatible(version: Any) -> bool:
    try:
        return str(version).strip().split(".")[0] == API_VERSION
    except Exception:
        return False


@dataclass
class PluginManifest:
    id: str
    name: str
    version: str
    api_version: str
    entry: str
    commands: list[PluginCommand]
    path: Path

    @classmethod
    def load(cls, plugin_dir: str | Path) -> "PluginManifest":
        directory = Path(plugin_dir).expanduser().resolve()
        manifest_path = directory / "plugin.json"
        if not manifest_path.is_file():
            raise PluginError(f"缺少 plugin.json：{directory}")
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PluginError(f"plugin.json 无法读取：{exc}") from exc
        if not isinstance(data, dict):
            raise PluginError("plugin.json 顶层必须是对象")
        plugin_id = str(data.get("id", "")).strip()
        if not _ID_RE.match(plugin_id):
            raise PluginError("插件 id 必须由字母、数字、点、下划线或短横线组成")
        for key in ("name", "version", "api_version", "entry"):
            if not str(data.get(key, "")).strip():
                raise PluginError(f"plugin.json 缺少字段：{key}")
        if not api_version_compatible(data.get("api_version")):
            raise PluginError(
                f"插件 API 版本不兼容：需要 {API_VERSION}，收到 {data.get('api_version')}"
            )
        entry = str(data.get("entry")).strip()
        entry_path = (directory / entry).resolve()
        try:
            entry_path.relative_to(directory)
        except ValueError as exc:
            raise PluginError("插件入口不能指向插件目录之外") from exc
        if not entry_path.is_file():
            raise PluginError(f"插件入口不存在：{entry}")
        raw_commands = data.get("commands")
        if not isinstance(raw_commands, list) or not raw_commands:
            raise PluginError("plugin.json 的 commands 必须是非空数组")
        commands: list[PluginCommand] = []
        seen: set[str] = set()
        for raw in raw_commands:
            if not isinstance(raw, dict):
                raise PluginError("commands 中的每一项必须是对象")
            command = PluginCommand.from_dict(raw)
            if command.id in seen:
                raise PluginError(f"插件命令 id 重复：{command.id}")
            if command.input_type not in {"text", "string"}:
                raise PluginError(f"当前只支持文本输入：{command.id}")
            if command.output_type != "text":
                raise PluginError(f"当前只支持文本输出：{command.id}")
            seen.add(command.id)
            commands.append(command)
        return cls(
            id=plugin_id,
            name=str(data["name"]).strip(),
            version=str(data["version"]).strip(),
            api_version=str(data["api_version"]).strip(),
            entry=entry,
            commands=commands,
            path=directory,
        )


@dataclass
class PluginInfo:
    id: str
    name: str
    version: str
    api_version: str
    path: str
    entry: str = ""
    commands: list[PluginCommand] = field(default_factory=list)
    enabled: bool = False
    error: str = ""

    @property
    def valid(self) -> bool:
        return not self.error

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "api_version": self.api_version,
            "path": self.path,
            "entry": self.entry,
            "commands": [
                {
                    "id": command.id,
                    "name": command.name,
                    "description": command.description,
                }
                for command in self.commands
            ],
            "enabled": self.enabled,
            "valid": self.valid,
            "error": self.error,
        }


@dataclass
class PluginExecutionResult:
    ok: bool
    plugin_id: str
    command_id: str
    output: str = ""
    error: str = ""
    timed_out: bool = False
    returncode: int | None = None
    stderr: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "plugin_id": self.plugin_id,
            "command_id": self.command_id,
            "output": self.output,
            "error": self.error,
            "timed_out": self.timed_out,
            "returncode": self.returncode,
            "stderr": self.stderr,
        }


class PluginHost:
    """把插件运行在独立 Python 子进程，使用 -I 隔离用户环境。"""

    def __init__(self, *, runner: str | Path | None = None, default_timeout: float = 5.0) -> None:
        self.runner = Path(runner) if runner else Path(__file__).with_name("plugin_runner.py")
        self.default_timeout = float(default_timeout)
        if self.default_timeout <= 0:
            raise PluginError("插件默认超时必须大于 0")

    def execute(
        self,
        plugin_dir: str | Path,
        command_id: str,
        text: str = "",
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
        plugin_id: str = "",
    ) -> PluginExecutionResult:
        plugin_path = Path(plugin_dir).expanduser().resolve()
        plugin_id = plugin_id or plugin_path.name
        effective_timeout = float(timeout if timeout is not None else self.default_timeout)
        if effective_timeout <= 0:
            return PluginExecutionResult(
                ok=False,
                plugin_id=plugin_id,
                command_id=command_id,
                error="插件超时必须大于 0",
            )
        payload = json.dumps(
            {
                "command_id": command_id,
                "text": str(text),
                "params": dict(params or {}),
            },
            ensure_ascii=False,
        )
        command = build_runner_command(self.runner, plugin_path)
        env = self._minimal_env()
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        try:
            completed = subprocess.run(
                command,
                input=payload,
                cwd=str(plugin_path),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=effective_timeout,
                shell=False,
                creationflags=creationflags,
            )
        except subprocess.TimeoutExpired as exc:
            stderr = exc.stderr or ""
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", "replace")
            return PluginExecutionResult(
                ok=False,
                plugin_id=plugin_id,
                command_id=command_id,
                error=f"插件执行超时（{effective_timeout:g} 秒）",
                timed_out=True,
                stderr=str(stderr),
            )
        except OSError as exc:
            return PluginExecutionResult(
                ok=False,
                plugin_id=plugin_id,
                command_id=command_id,
                error=f"无法启动插件进程：{exc}",
            )
        return self._parse_result(
            completed.stdout or "",
            completed.stderr or "",
            completed.returncode,
            plugin_id,
            command_id,
        )

    @staticmethod
    def _minimal_env() -> dict[str, str]:
        keys = (
            "SYSTEMROOT",
            "WINDIR",
            "PATH",
            "TEMP",
            "TMP",
            "COMSPEC",
            "PATHEXT",
            "HOME",
            "USERPROFILE",
        )
        env = {key: os.environ[key] for key in keys if key in os.environ}
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    @staticmethod
    def _parse_result(
        stdout: str,
        stderr: str,
        returncode: int,
        plugin_id: str,
        command_id: str,
    ) -> PluginExecutionResult:
        payload: dict[str, Any] | None = None
        for line in reversed(stdout.splitlines()):
            if line.startswith(RESULT_MARKER):
                try:
                    payload = json.loads(line[len(RESULT_MARKER):])
                except json.JSONDecodeError:
                    payload = None
                break
        if payload is None:
            detail = stderr.strip() or stdout.strip() or "插件进程没有返回协议结果"
            return PluginExecutionResult(
                ok=False,
                plugin_id=plugin_id,
                command_id=command_id,
                error=f"插件结果无效：{detail[-500:]}",
                returncode=returncode,
                stderr=stderr,
            )
        if not payload.get("ok"):
            return PluginExecutionResult(
                ok=False,
                plugin_id=plugin_id,
                command_id=command_id,
                error=str(payload.get("error") or "插件执行失败"),
                returncode=returncode,
                stderr=stderr,
            )
        output = payload.get("output")
        if not isinstance(output, str):
            return PluginExecutionResult(
                ok=False,
                plugin_id=plugin_id,
                command_id=command_id,
                error="插件返回值不是文本",
                returncode=returncode,
                stderr=stderr,
            )
        return PluginExecutionResult(
            ok=True,
            plugin_id=plugin_id,
            command_id=command_id,
            output=output,
            returncode=returncode,
            stderr=stderr,
        )


class PluginManager:
    """插件清单、同意状态、命令注册与调用。"""

    def __init__(
        self,
        plugins_root: str | Path,
        *,
        state_path: str | Path | None = None,
        host: PluginHost | None = None,
        timeout: float = 5.0,
    ) -> None:
        self.plugins_root = Path(plugins_root).expanduser().resolve()
        self.state_path = (
            Path(state_path).expanduser()
            if state_path
            else self.plugins_root.parent / "plugin_state.json"
        )
        self.host = host or PluginHost(default_timeout=timeout)
        self.timeout = float(timeout)
        self._lock = threading.RLock()
        self._cache: dict[str, PluginInfo] = {}
        self._manifests: dict[str, PluginManifest] = {}
        self._state: dict[str, dict[str, Any]] = self._load_state()
        self._registry: CommandRegistry | None = None
        self.scan()

    # ------------------------------------------------------------------ state
    def _load_state(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        raw = data.get("plugins", data) if isinstance(data, dict) else {}
        if not isinstance(raw, dict):
            return {}
        return {
            str(key): dict(value)
            for key, value in raw.items()
            if isinstance(value, dict)
        }

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"version": 1, "plugins": self._state},
            ensure_ascii=False,
            indent=2,
        )
        temp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temp.write_text(payload, encoding="utf-8")
        temp.replace(self.state_path)

    def _is_enabled(self, plugin_id: str) -> bool:
        entry = self._state.get(plugin_id, {})
        # 必须同时存在明确同意记录，防止仅手工改 enabled 就自动执行插件。
        return bool(entry.get("enabled", False) and entry.get("consented_at"))

    # ------------------------------------------------------------------- scan
    def scan(self, *, refresh: bool = True) -> list[PluginInfo]:
        """扫描插件目录，仅解析 JSON，不执行插件代码（懒加载）。"""

        with self._lock:
            if not refresh and self._cache:
                return list(self._cache.values())
            infos: dict[str, PluginInfo] = {}
            manifests: dict[str, PluginManifest] = {}
            if self.plugins_root.is_dir():
                for directory in sorted(self.plugins_root.iterdir()):
                    if not directory.is_dir() or directory.name.startswith("__"):
                        continue
                    try:
                        manifest = PluginManifest.load(directory)
                        if manifest.id in infos:
                            raise PluginError(f"插件 id 重复：{manifest.id}")
                    except PluginError as exc:
                        fallback_id = directory.name
                        infos[fallback_id] = PluginInfo(
                            id=fallback_id,
                            name=directory.name,
                            version="",
                            api_version="",
                            path=str(directory),
                            enabled=False,
                            error=str(exc),
                        )
                        continue
                    info = PluginInfo(
                        id=manifest.id,
                        name=manifest.name,
                        version=manifest.version,
                        api_version=manifest.api_version,
                        path=str(manifest.path),
                        entry=manifest.entry,
                        commands=list(manifest.commands),
                        enabled=self._is_enabled(manifest.id),
                    )
                    infos[manifest.id] = info
                    manifests[manifest.id] = manifest
            self._cache = infos
            self._manifests = manifests
            return list(infos.values())

    def list_plugins(self, *, refresh: bool = False) -> list[PluginInfo]:
        return self.scan(refresh=refresh)

    def get(self, plugin_id: str) -> PluginInfo:
        with self._lock:
            info = self._cache.get(plugin_id)
        if info is None:
            raise PluginError(f"插件不存在：{plugin_id}")
        return info

    def _manifest(self, plugin_id: str) -> PluginManifest:
        with self._lock:
            manifest = self._manifests.get(plugin_id)
        if manifest is None:
            raise PluginError(f"插件不可用或清单无效：{plugin_id}")
        return manifest

    # ------------------------------------------------------- enable / uninstall
    def enable(self, plugin_id: str, *, consent: bool = False) -> PluginInfo:
        if consent is not True:
            raise PluginConsentRequired(
                "启用插件会执行第三方代码，必须由用户明确同意（consent=True / --consent）。"
            )
        self.scan(refresh=False)
        manifest = self._manifest(plugin_id)
        with self._lock:
            self._state[plugin_id] = {
                "enabled": True,
                "consented_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            self._save_state()
            info = self._cache[plugin_id]
            info.enabled = True
        self._refresh_registry()
        return info

    def disable(self, plugin_id: str) -> PluginInfo:
        self.get(plugin_id)
        with self._lock:
            state = dict(self._state.get(plugin_id, {}))
            state["enabled"] = False
            self._state[plugin_id] = state
            self._save_state()
            info = self._cache[plugin_id]
            info.enabled = False
        self._refresh_registry()
        return info

    def is_enabled(self, plugin_id: str) -> bool:
        try:
            return self.get(plugin_id).enabled and self.valid_manifest(plugin_id)
        except PluginError:
            return False

    def valid_manifest(self, plugin_id: str) -> bool:
        with self._lock:
            return plugin_id in self._manifests

    def uninstall(
        self,
        plugin_id: str,
        *,
        consent: bool = False,
        remove_files: bool = False,
    ) -> bool:
        if consent is not True:
            raise PluginConsentRequired("卸载插件需要明确同意（consent=True / --consent）。")
        info = self.get(plugin_id)
        with self._lock:
            self._state.pop(plugin_id, None)
            self._save_state()
            self._cache.pop(plugin_id, None)
            self._manifests.pop(plugin_id, None)
            registry = self._registry
        if registry is not None:
            registry.unregister_source(self.plugin_source(plugin_id))
        if remove_files:
            path = Path(info.path).resolve()
            try:
                path.relative_to(self.plugins_root)
            except ValueError as exc:
                raise PluginError("拒绝删除插件目录之外的路径") from exc
            if path.exists():
                shutil.rmtree(path)
        self._refresh_registry()
        return True

    # --------------------------------------------------------------- execution
    def run(
        self,
        plugin_id: str,
        command_id: str,
        text: str = "",
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> PluginExecutionResult:
        info = self.get(plugin_id)
        if not info.valid:
            raise PluginError(f"插件清单无效：{info.error}")
        if not self.is_enabled(plugin_id):
            raise PluginDisabled(f"插件已禁用，不能调用：{plugin_id}")
        declared = {command.id for command in info.commands}
        if command_id not in declared:
            return PluginExecutionResult(
                ok=False,
                plugin_id=plugin_id,
                command_id=command_id,
                error=f"插件未声明命令：{command_id}",
            )
        manifest = self._manifest(plugin_id)
        return self.host.execute(
            manifest.path,
            command_id,
            text,
            params,
            timeout=timeout if timeout is not None else self.timeout,
            plugin_id=plugin_id,
        )

    # 兼容不同调用习惯
    execute = run

    def execute_or_raise(
        self,
        plugin_id: str,
        command_id: str,
        text: str = "",
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> str:
        result = self.run(plugin_id, command_id, text, params, timeout=timeout)
        if not result.ok:
            raise PluginExecutionError(result.error)
        return result.output

    # --------------------------------------------------------------- registry
    def register_commands(self, registry: CommandRegistry) -> int:
        with self._lock:
            self._registry = registry
            for plugin_id in list(self._cache):
                registry.unregister_source(self.plugin_source(plugin_id))
            count = 0
            for plugin_id, info in self._cache.items():
                if not info.valid or not info.enabled:
                    continue
                for plugin_command in info.commands:
                    command_id = self.command_id(plugin_id, plugin_command.id)
                    command = Command(
                        id=command_id,
                        name=plugin_command.name,
                        aliases=list(plugin_command.aliases),
                        category="插件",
                        keywords=list(plugin_command.keywords)
                        + [plugin_id, "plugin", "插件"],
                        description=plugin_command.description,
                        source=self.plugin_source(plugin_id),
                    )

                    def handler(
                        ctx: CommandContext,
                        _plugin_id: str = plugin_id,
                        _plugin_command: str = plugin_command.id,
                    ) -> str:
                        result = self.run(_plugin_id, _plugin_command, ctx.text, ctx.params)
                        if not result.ok:
                            raise PluginExecutionError(result.error)
                        return result.output

                    registry.register(command, handler)
                    count += 1
        return count

    def _refresh_registry(self) -> None:
        with self._lock:
            registry = self._registry
        if registry is not None:
            self.register_commands(registry)

    @staticmethod
    def plugin_source(plugin_id: str) -> str:
        return f"plugin:{plugin_id}"

    @staticmethod
    def command_id(plugin_id: str, command_id: str) -> str:
        return f"plugin:{plugin_id}:{command_id}"

    def close(self) -> None:
        with self._lock:
            self._registry = None
