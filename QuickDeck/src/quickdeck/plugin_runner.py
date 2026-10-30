"""插件子进程入口。

由 plugin_host 使用 ``python -I plugin_runner.py <plugin_dir>`` 启动。
协议：stdin 传入 JSON，stdout 最后一行以 ``__QUICKDECK_RESULT__`` 开头。
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import sys
import traceback
from pathlib import Path
from typing import Any

RESULT_MARKER = "__QUICKDECK_RESULT__"


def _load_plugin_module(plugin_dir: Path, entry: str, plugin_id: str):
    entry_path = plugin_dir / entry
    if not entry_path.is_file():
        raise RuntimeError(f"插件入口不存在：{entry}")
    spec = importlib.util.spec_from_file_location(
        f"quickdeck_plugin_{plugin_id.replace('-', '_')}",
        str(entry_path),
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载插件模块")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _register_handlers(api, module) -> None:
    register = getattr(module, "register", None)
    setup = getattr(module, "setup", None)
    if callable(register):
        register(api)
        return
    if callable(setup):
        setup(api)
        return
    for command in api.commands():
        for name in (
            command.id,
            f"cmd_{command.id}",
            f"command_{command.id}",
            f"run_{command.id}",
        ):
            func = getattr(module, name, None)
            if callable(func):
                api.register_command(command.id, func)
                break


def _emit(payload: dict[str, Any]) -> None:
    sys.stdout.write(RESULT_MARKER + json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _force_utf8_streams() -> None:
    # -I 会忽略 PYTHONIOENCODING，必须显式固定协议流编码。
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main() -> int:
    _force_utf8_streams()
    # 运行在 -I 隔离模式下，显式加入本包源码路径以导入稳定 API。
    src_root = Path(__file__).resolve().parents[1]
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))

    if len(sys.argv) != 2:
        _emit({"ok": False, "error": "plugin_runner 需要插件目录参数"})
        return 2
    plugin_dir = Path(sys.argv[1]).resolve()
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        _emit({"ok": False, "error": f"输入协议无效：{exc}"})
        return 2

    from quickdeck.plugin_api import API_VERSION, PluginAPI, PluginCommand

    try:
        manifest = json.loads((plugin_dir / "plugin.json").read_text(encoding="utf-8"))
        if str(manifest.get("api_version", "")).strip().split(".")[0] != API_VERSION:
            raise RuntimeError(
                f"插件 API 版本不兼容：需要 {API_VERSION}，收到 {manifest.get('api_version')}"
            )
        plugin_id = str(manifest.get("id", ""))
        commands = [
            PluginCommand.from_dict(item) for item in manifest.get("commands", [])
        ]
        api = PluginAPI(plugin_id, commands)
        module = _load_plugin_module(plugin_dir, str(manifest.get("entry", "")), plugin_id)
        # 插件自己的 print 不应污染 JSON 协议。
        with contextlib.redirect_stdout(sys.stderr):
            _register_handlers(api, module)
            command_id = str(payload.get("command_id", ""))
            output = api.execute(
                command_id,
                text=str(payload.get("text", "")),
                params=dict(payload.get("params") or {}),
            )
    except BaseException as exc:  # 插件异常必须转成协议错误，不能吞掉
        traceback.print_exc(file=sys.stderr)
        _emit({"ok": False, "error": str(exc) or type(exc).__name__})
        return 1
    _emit({"ok": True, "output": output})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
