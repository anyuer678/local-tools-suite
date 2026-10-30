"""应用上下文与 GUI/CLI 启动分流。"""

from __future__ import annotations

import importlib.util
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .builtin_actions import BuiltinToolkit
from .clipboard_service import ClipboardHistoryService
from .command_registry import CommandRegistry
from .plugin_host import PluginManager
from .settings_store import SettingsStore, default_data_dir
from .workflow_engine import ActionRunner, ActionStore, WorkflowEngine


class GuiUnavailable(RuntimeError):
    pass


@dataclass
class AppContext:
    settings_store: SettingsStore
    registry: CommandRegistry
    toolkit: BuiltinToolkit
    workflow_engine: WorkflowEngine
    action_runner: ActionRunner
    action_store: ActionStore
    plugin_manager: PluginManager
    plugins_dir: Path

    @property
    def data_dir(self) -> Path:
        return self.settings_store.data_dir

    @property
    def settings(self) -> dict[str, Any]:
        return self.settings_store.settings

    def close(self) -> None:
        for closer in (
            self.plugin_manager.close,
            self.action_store.close,
            self.toolkit.close,
            self.registry.close,
        ):
            try:
                closer()
            except Exception:
                pass


class StartupError(RuntimeError):
    pass


def _startup_command() -> str:
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable)
        if executable.name.lower() == "quickdeckcli.exe":
            for candidate in (
                executable.with_name("QuickDeck.exe"),
                executable.parent.parent / "QuickDeck.exe",
                executable.parent.parent / "QuickDeck" / "QuickDeck.exe",
            ):
                if candidate.exists():
                    executable = candidate
                    break
        args = "--minimized"
    else:
        python = Path(sys.executable)
        pythonw = python.with_name("pythonw.exe")
        executable = pythonw if pythonw.exists() else python
        args = "-m quickdeck --minimized"
    return f'"{executable}" {args}'


def set_startup_enabled(enabled: bool, *, command: str | None = None) -> bool:
    """写入/删除 HKCU Run 项；仅 Windows，失败抛出明确异常。"""

    if sys.platform != "win32":
        raise StartupError("开机启动仅支持 Windows。")
    import winreg

    run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
    value_name = "QuickDeck"
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            run_key,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            if enabled:
                winreg.SetValueEx(
                    key,
                    value_name,
                    0,
                    winreg.REG_SZ,
                    command or _startup_command(),
                )
            else:
                try:
                    winreg.DeleteValue(key, value_name)
                except FileNotFoundError:
                    pass
    except OSError as exc:
        raise StartupError(f"设置开机启动失败：{exc}") from exc
    return True


def get_startup_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_READ,
        ) as key:
            try:
                value, _ = winreg.QueryValueEx(key, "QuickDeck")
            except FileNotFoundError:
                return False
            return bool(value)
    except OSError as exc:
        raise StartupError(f"读取开机启动设置失败：{exc}") from exc


def qt_available() -> bool:
    try:
        return importlib.util.find_spec("PySide6") is not None
    except (ImportError, ValueError):
        return False


def default_plugins_dir(data_dir: str | Path | None = None) -> Path:
    env = os.environ.get("QUICKDECK_PLUGINS_DIR")
    if env:
        return Path(env).expanduser().resolve()
    if getattr(sys, "frozen", False):
        frozen_candidate = Path(sys.executable).resolve().parent / "plugins"
        if frozen_candidate.is_dir():
            return frozen_candidate
    project_candidate = Path(__file__).resolve().parents[2] / "plugins"
    if project_candidate.is_dir():
        return project_candidate
    root = Path(data_dir).expanduser() if data_dir else default_data_dir()
    return root / "plugins"


def build_context(
    data_dir: str | Path | None = None,
    *,
    plugins_dir: str | Path | None = None,
    clipboard_backend: Any = None,
    settings: dict[str, Any] | None = None,
) -> AppContext:
    """构造完整应用上下文；CLI 与 GUI 共用。"""

    store = SettingsStore(data_dir)
    if settings:
        store.update(settings)
    current = store.settings
    store.migrate_database()
    registry = CommandRegistry(store.db_path)
    clipboard_settings = current.get("clipboard", {})
    toolkit = BuiltinToolkit(
        store.db_path,
        data_dir=store.data_dir,
        clipboard_enabled=bool(clipboard_settings.get("enabled", False)),
        clipboard_max_items=int(clipboard_settings.get("max_items", 50)),
        clipboard_exclude=clipboard_settings.get("exclude_patterns", []),
        clipboard_backend=clipboard_backend,
        clipboard_db_path=store.db_path,
    )
    toolkit.register_commands(registry)
    engine = WorkflowEngine(default_timeout=float(current.get("plugins", {}).get("timeout_seconds", 5)))
    action_runner = ActionRunner(engine, toolkit.clipboard)
    action_store = ActionStore(store.db_path)
    action_store.register_into(registry, action_runner)
    resolved_plugins = (
        Path(plugins_dir).expanduser().resolve()
        if plugins_dir
        else default_plugins_dir(store.data_dir)
    )
    plugin_manager = PluginManager(
        resolved_plugins,
        state_path=store.data_dir / "plugin_state.json",
        timeout=float(current.get("plugins", {}).get("timeout_seconds", 5)),
    )
    plugin_manager.register_commands(registry)
    return AppContext(
        settings_store=store,
        registry=registry,
        toolkit=toolkit,
        workflow_engine=engine,
        action_runner=action_runner,
        action_store=action_store,
        plugin_manager=plugin_manager,
        plugins_dir=resolved_plugins,
    )


def launch_gui(
    context: AppContext | None = None,
    *,
    start_minimized: bool = False,
) -> int:
    """惰性导入 PySide6 GUI；不可用时抛 ``GuiUnavailable``。"""

    if not qt_available():
        raise GuiUnavailable("PySide6 未安装，无法启动图形界面。")
    try:
        from .ui.qt_app import run_gui
    except ImportError as exc:
        raise GuiUnavailable(f"PySide6 GUI 无法导入：{exc}") from exc
    return int(run_gui(context, start_minimized=start_minimized))


def run_launcher(context: AppContext | None = None) -> int:
    """默认启动入口：GUI 可用则 GUI，否则打印 CLI 回退提示。"""

    if qt_available():
        return launch_gui(context)
    print("PySide6 不可用，QuickDeck 已回退到 CLI 模式。")
    print("使用 `python -m quickdeck --help` 查看搜索、执行工具、管理动作/插件等命令。")
    return 0
