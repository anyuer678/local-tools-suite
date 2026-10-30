"""QuickDeck 命令行接口。

PySide6 不可用或使用 ``--cli`` 时，搜索、执行内置工具、管理动作/插件、
设置与诊断都可以在此完成。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .builtin_actions import (
    ConfirmationRequired,
    ToolError,
    UnitConversionError,
    calculator,
    format_unit_result,
)
from .command_registry import CommandRegistryError
from .diagnostics import collect_diagnostics, log_exception, setup_logging
from .launcher_ui import (
    GuiUnavailable,
    StartupError,
    build_context,
    get_startup_enabled,
    qt_available,
    launch_gui,
    set_startup_enabled,
)
from .plugin_host import PluginError
from .settings_store import SettingsError, SettingsStore, default_data_dir
from .workflow_engine import ActionDefinition, ActionError, WorkflowError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quickdeck",
        description="QuickDeck：键盘为中心的可扩展快捷工具台",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m quickdeck search calc\n"
            "  python -m quickdeck calc \"1 + 2 * 3\"\n"
            "  python -m quickdeck run encoding.base64_encode -i 你好\n"
            "  python -m quickdeck plugins list\n"
            "  python -m quickdeck plugins enable example --consent\n"
            "  python -m quickdeck actions add --file action.json\n"
            "  python -m quickdeck settings backup\n"
        ),
    )
    parser.add_argument("--data-dir", help="数据目录（默认使用 QUICKDECK_DATA_DIR 或系统应用数据目录）")
    parser.add_argument("--plugins-dir", help="插件目录（默认使用项目 plugins/）")
    parser.add_argument("--minimized", action="store_true", help="GUI 启动后隐藏到托盘")
    parser.add_argument("--cli", action="store_true", help="强制 CLI 模式，不启动 GUI")
    parser.add_argument("--version", action="version", version=f"QuickDeck {__version__}")

    sub = parser.add_subparsers(dest="command", metavar="命令")

    gui = sub.add_parser("gui", help="启动 PySide6 图形界面")
    gui.add_argument(
        "--minimized",
        action="store_true",
        default=argparse.SUPPRESS,
        help="启动后隐藏到托盘",
    )

    search = sub.add_parser("search", help="模糊搜索命令")
    search.add_argument("query", nargs="*", help="搜索词；留空显示常用命令")
    search.add_argument("--category", help="按分类过滤")
    search.add_argument("--limit", type=int, default=None, help="结果数量上限")
    search.add_argument("--json", action="store_true", help="输出 JSON")

    run = sub.add_parser("run", help="执行注册表中的命令")
    run.add_argument("command_id", help="命令 id")
    run.add_argument("-i", "--input", default="", help="输入文本")
    run.add_argument("-f", "--file", help="从 UTF-8 文件读取输入")
    run.add_argument("--params", default="", help="参数 JSON 对象")

    calc = sub.add_parser("calc", help="安全计算数学表达式")
    calc.add_argument("expression", nargs="+", help="表达式，例如 \"2 ** 8\"")

    unit = sub.add_parser("unit", help="单位换算（长度/质量/时间/存储）")
    unit.add_argument("value", help="数值")
    unit.add_argument("from_unit", help="源单位")
    unit.add_argument("to_unit", help="目标单位")
    unit.add_argument("--category", help="单位类别，可选 length/mass/time/storage")

    time_parser = sub.add_parser("time", help="时间记录")
    time_sub = time_parser.add_subparsers(dest="time_action", required=True)
    time_record = time_sub.add_parser("record", help="记录当前时间")
    time_record.add_argument("activity", help="活动名称")
    time_record.add_argument("--note", default="", help="备注")
    time_history = time_sub.add_parser("history", help="查看历史")
    time_history.add_argument("--activity", default=None, help="限定活动名称")
    time_history.add_argument("--limit", type=int, default=10)
    time_history.add_argument("--json", action="store_true")

    external = sub.add_parser("exec", help="执行预配置外部命令（必须确认）")
    external.add_argument("--program", required=True, help="可执行文件")
    external.add_argument("--arg", action="append", default=[], help="参数，可重复")
    external.add_argument("--cwd", default=None, help="工作目录")
    external.add_argument("--timeout", type=float, default=10.0, help="超时秒数")
    external.add_argument("--yes", action="store_true", help="明确确认执行")

    clip = sub.add_parser("clipboard", help="剪贴板历史（默认关闭）")
    clip_sub = clip.add_subparsers(dest="clipboard_action", required=True)
    clip_sub.add_parser("status", help="显示状态")
    clip_enable = clip_sub.add_parser("enable", help="启用剪贴板历史")
    clip_disable = clip_sub.add_parser("disable", help="禁用剪贴板历史")
    clip_capture = clip_sub.add_parser("capture", help="读取一次系统剪贴板")
    clip_history = clip_sub.add_parser("history", help="查看历史")
    clip_history.add_argument("--limit", type=int, default=None)
    clip_history.add_argument("--json", action="store_true")
    clip_sub.add_parser("clear", help="清空历史")

    actions = sub.add_parser("actions", help="管理自定义动作")
    actions_sub = actions.add_subparsers(dest="actions_action", required=True)
    actions_list = actions_sub.add_parser("list", help="列出动作")
    actions_list.add_argument("--json", action="store_true")
    actions_show = actions_sub.add_parser("show", help="显示动作定义")
    actions_show.add_argument("action_id")
    actions_add = actions_sub.add_parser("add", help="从 JSON 文件或字符串新增动作")
    actions_add_group = actions_add.add_mutually_exclusive_group(required=True)
    actions_add_group.add_argument("--file", help="动作 JSON 文件")
    actions_add_group.add_argument("--json", dest="json_text", help="动作 JSON 字符串")
    actions_delete = actions_sub.add_parser("delete", help="删除动作")
    actions_delete.add_argument("action_id")
    actions_run = actions_sub.add_parser("run", help="执行动作")
    actions_run.add_argument("action_id")
    actions_run.add_argument("-i", "--input", default="")
    actions_run.add_argument("-f", "--file")
    actions_run.add_argument("--params", default="")

    plugins = sub.add_parser("plugins", help="管理插件（启用/卸载需明确同意）")
    plugins_sub = plugins.add_subparsers(dest="plugins_action", required=True)
    plugins_list = plugins_sub.add_parser("list", help="列出插件")
    plugins_list.add_argument("--json", action="store_true")
    plugins_show = plugins_sub.add_parser("show", help="显示插件信息")
    plugins_show.add_argument("plugin_id")
    plugins_enable = plugins_sub.add_parser("enable", help="启用插件（需 --consent）")
    plugins_enable.add_argument("plugin_id")
    plugins_enable.add_argument("--consent", action="store_true", help="明确同意执行第三方代码")
    plugins_disable = plugins_sub.add_parser("disable", help="禁用插件")
    plugins_disable.add_argument("plugin_id")
    plugins_uninstall = plugins_sub.add_parser("uninstall", help="卸载插件")
    plugins_uninstall.add_argument("plugin_id")
    plugins_uninstall.add_argument("--consent", action="store_true", help="明确同意卸载")
    plugins_uninstall.add_argument("--delete-files", action="store_true", help="同时删除插件文件")
    plugins_run = plugins_sub.add_parser("run", help="直接运行插件命令")
    plugins_run.add_argument("plugin_id")
    plugins_run.add_argument("command_id")
    plugins_run.add_argument("-i", "--input", default="")
    plugins_run.add_argument("-f", "--file")
    plugins_run.add_argument("--params", default="")
    plugins_run.add_argument("--timeout", type=float, default=None)

    settings = sub.add_parser("settings", help="设置、备份与启动项")
    settings_sub = settings.add_subparsers(dest="settings_action", required=True)
    settings_sub.add_parser("show", help="显示设置")
    settings_set = settings_sub.add_parser("set", help="修改设置（点号路径）")
    settings_set.add_argument("key")
    settings_set.add_argument("value")
    settings_export = settings_sub.add_parser("export", help="导出设置 JSON")
    settings_export.add_argument("path")
    settings_import = settings_sub.add_parser("import", help="导入设置 JSON")
    settings_import.add_argument("path")
    settings_import.add_argument("--replace", action="store_true", help="替换而非合并")
    settings_backup = settings_sub.add_parser("backup", help="备份 SQLite 数据库")
    settings_backup.add_argument("--path", default=None, help="备份目标路径")
    settings_restore = settings_sub.add_parser("restore", help="从备份恢复数据库")
    settings_restore.add_argument("path")
    settings_sub.add_parser("migrate", help="迁移数据库 schema")
    settings_startup = settings_sub.add_parser("startup", help="开机启动（仅 Windows）")
    settings_startup.add_argument(
        "startup_action",
        nargs="?",
        choices=["status", "enable", "disable"],
        default="status",
    )

    diagnose = sub.add_parser("diagnose", help="输出诊断信息 / 写入日志")
    diagnose.add_argument("--json", action="store_true", help="输出 JSON（默认也是 JSON）")
    diagnose.add_argument("--event", default=None, help="写入一条诊断事件")

    return parser


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _parse_params(raw: str) -> dict[str, Any]:
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--params 必须是合法 JSON：{exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("--params 顶层必须是 JSON 对象")
    return value


def _read_text(text: str, file_path: str | None) -> str:
    if file_path:
        try:
            return Path(file_path).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"读取输入文件失败：{exc}") from exc
    return text or ""


def _command_to_dict(command) -> dict[str, Any]:
    return {
        "id": command.id,
        "name": command.name,
        "aliases": list(command.aliases),
        "category": command.category,
        "keywords": list(command.keywords),
        "description": command.description,
        "source": command.source,
        "use_count": command.use_count,
        "last_used": command.last_used,
    }


def _action_payload(args) -> list[ActionDefinition]:
    try:
        raw = (
            Path(args.file).expanduser().read_text(encoding="utf-8")
            if args.file
            else args.json_text
        )
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"动作 JSON 无法读取：{exc}") from exc
    if isinstance(data, dict) and "actions" in data:
        data = data["actions"]
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("动作 JSON 必须是对象或对象数组")
    return [ActionDefinition.from_dict(item) for item in data]


def _dispatch(args, parser: argparse.ArgumentParser, context) -> int:
    command = args.command
    if command == "search":
        query = " ".join(args.query)
        limit = args.limit
        if limit is None:
            limit = int(context.settings.get("search", {}).get("max_results", 30))
        if limit < 0:
            raise ValueError("--limit 不能为负数")
        results = context.registry.search(query, category=args.category, limit=limit)
        if args.json:
            _print_json([_command_to_dict(item) for item in results])
        else:
            if not results:
                print("没有匹配的命令")
            for item in results:
                print(f"{item.name}  [{item.category}]  ({item.id})")
                if item.description:
                    print(f"  {item.description}")
        return 0

    if command == "run":
        text = _read_text(args.input, args.file)
        params = _parse_params(args.params)
        result = context.registry.execute(args.command_id, text, params)
        print(result)
        return 0

    if command == "calc":
        print(calculator(" ".join(args.expression)))
        return 0

    if command == "unit":
        print(
            format_unit_result(
                args.value,
                args.from_unit,
                args.to_unit,
                category=args.category,
            )
        )
        return 0

    if command == "time":
        if args.time_action == "record":
            record = context.toolkit.activities.record(args.activity, args.note)
            print(f"已记录 {record.name}：{record.recorded_at}")
            return 0
        records = context.toolkit.activities.history(args.activity, args.limit)
        if args.json:
            _print_json([record.as_dict() for record in records])
        else:
            if not records:
                print("暂无时间记录")
            for record in records:
                print(f"{record.recorded_at}  {record.name}" + (f"  [{record.note}]" if record.note else ""))
        return 0

    if command == "exec":
        spec = {
            "argv": [args.program, *args.arg],
            "cwd": args.cwd,
            "timeout": args.timeout,
            "name": args.program,
        }
        preview = context.toolkit.preview_external(spec)
        print("即将执行的动作：")
        _print_json(preview)
        if not args.yes:
            print("已取消：外部命令必须展示动作信息并明确确认，请追加 --yes。", file=sys.stderr)
            return 2
        result = context.toolkit.execute_external(spec, confirm=True)
        print(result.as_text())
        return 0 if result.ok else 1

    if command == "clipboard":
        service = context.toolkit.clipboard
        action = args.clipboard_action
        if action == "status":
            _print_json(
                {
                    "enabled": service.enabled,
                    "backend": type(service.backend).__name__,
                    "max_items": service.max_items,
                    "items": len(service.history()),
                }
            )
            return 0
        if action in {"enable", "disable"}:
            enabled = action == "enable"
            context.settings_store.set("clipboard.enabled", enabled)
            service.set_enabled(enabled)
            print(f"剪贴板历史已{'启用' if enabled else '禁用'}")
            return 0
        if action == "capture":
            if not service.enabled:
                print("剪贴板历史默认关闭，请先执行 clipboard enable。", file=sys.stderr)
                return 2
            entry = service.capture_once()
            if entry is None:
                print("没有捕获到新内容（空内容、重复或命中排除规则）")
            else:
                print(f"已捕获：{entry.text}")
            return 0
        if action == "history":
            entries = service.history(args.limit)
            if args.json:
                _print_json([entry.as_dict() for entry in entries])
            else:
                for entry in entries:
                    print(f"{entry.captured_at}  {entry.text}")
            return 0
        if action == "clear":
            print(f"已清空 {service.clear()} 条剪贴板历史")
            return 0

    if command == "actions":
        store = context.action_store
        if args.actions_action == "list":
            actions = store.all()
            if args.json:
                _print_json([action.as_dict() for action in actions])
            else:
                if not actions:
                    print("暂无自定义动作")
                for action in actions:
                    print(
                        f"{action.name}  ({action.id})  "
                        f"{len(action.steps)} 步 → {action.output_method}"
                    )
            return 0
        if args.actions_action == "show":
            action = store.get(args.action_id) or store.get(
                args.action_id.removeprefix("action.")
            )
            if action is None:
                print(f"动作不存在：{args.action_id}", file=sys.stderr)
                return 1
            _print_json(action.as_dict())
            return 0
        if args.actions_action == "add":
            actions = _action_payload(args)
            for action in actions:
                store.save(action)
            context.registry.unregister_source("action")
            store.register_into(context.registry, context.action_runner)
            print(f"已保存 {len(actions)} 个动作")
            return 0
        if args.actions_action == "delete":
            action_id = args.action_id.removeprefix("action.")
            existed = store.delete(action_id)
            context.registry.unregister(f"action.{action_id}")
            print("已删除动作" if existed else "动作不存在")
            return 0 if existed else 1
        if args.actions_action == "run":
            action_id = args.action_id.removeprefix("action.")
            action = store.get(action_id)
            if action is None:
                print(f"动作不存在：{args.action_id}", file=sys.stderr)
                return 1
            text = _read_text(args.input, args.file)
            params = _parse_params(args.params)
            execution = context.action_runner.execute(action, text, params)
            if execution.delivered_to == "display":
                print(execution.output)
            else:
                print(execution.message)
            return 0

    if command == "plugins":
        manager = context.plugin_manager
        action = args.plugins_action
        if action == "list":
            plugins = manager.list_plugins(refresh=True)
            if args.json:
                _print_json([plugin.as_dict() for plugin in plugins])
            else:
                if not plugins:
                    print("未发现插件")
                for plugin in plugins:
                    state = "已启用" if plugin.enabled else "未启用"
                    valid = "" if plugin.valid else f"  错误：{plugin.error}"
                    print(
                        f"{plugin.name}  ({plugin.id}) v{plugin.version}  "
                        f"[{state}]{valid}"
                    )
            return 0
        if action == "show":
            _print_json(manager.get(args.plugin_id).as_dict())
            return 0
        if action == "enable":
            if not args.consent:
                print(
                    "启用插件会执行第三方代码，必须明确同意：请追加 --consent。",
                    file=sys.stderr,
                )
                return 2
            plugin = manager.enable(args.plugin_id, consent=True)
            print(f"插件已启用：{plugin.name} ({plugin.id})")
            for plugin_command in plugin.commands:
                print(
                    f"  {manager.command_id(plugin.id, plugin_command.id)}  "
                    f"{plugin_command.name}"
                )
            return 0
        if action == "disable":
            plugin = manager.disable(args.plugin_id)
            print(f"插件已禁用：{plugin.name} ({plugin.id})")
            return 0
        if action == "uninstall":
            if not args.consent:
                print("卸载插件必须明确同意：请追加 --consent。", file=sys.stderr)
                return 2
            manager.uninstall(
                args.plugin_id,
                consent=True,
                remove_files=args.delete_files,
            )
            print(f"插件已卸载：{args.plugin_id}")
            return 0
        if action == "run":
            text = _read_text(args.input, args.file)
            params = _parse_params(args.params)
            result = manager.run(
                args.plugin_id,
                args.command_id,
                text,
                params,
                timeout=args.timeout,
            )
            if result.ok:
                print(result.output)
                return 0
            print(f"插件执行失败：{result.error}", file=sys.stderr)
            if result.stderr:
                print(result.stderr[-1000:], file=sys.stderr)
            return 1

    if command == "diagnose":
        store = SettingsStore(args.data_dir)
        if args.event:
            setup_logging(store.data_dir)
            from .diagnostics import log_event

            log_event(store.data_dir, args.event)
        _print_json(collect_diagnostics(store.data_dir))
        return 0

    if command == "settings":
        store = SettingsStore(args.data_dir)
        action = args.settings_action
        if action == "show":
            _print_json(store.settings)
            return 0
        if action == "set":
            try:
                value = json.loads(args.value)
            except json.JSONDecodeError:
                value = args.value
            store.set(args.key, value)
            print(f"已设置 {args.key} = {value!r}")
            return 0
        if action == "export":
            path = store.export_json(args.path)
            print(f"设置已导出：{path}")
            return 0
        if action == "import":
            store.import_json(args.path, merge=not args.replace)
            print("设置已导入")
            return 0
        if action == "backup":
            path = store.backup_database(backup_path=args.path)
            print(f"数据库已备份：{path}")
            return 0
        if action == "restore":
            path = store.restore_database(args.path)
            print(f"数据库已恢复：{path}")
            return 0
        if action == "migrate":
            version = store.migrate_database()
            print(f"数据库 schema 版本：{version}")
            return 0
        if action == "startup":
            if args.startup_action == "status":
                _print_json(
                    {
                        "platform_supported": sys.platform == "win32",
                        "enabled": get_startup_enabled(),
                        "setting": store.get("startup.open_at_login"),
                    }
                )
                return 0
            enabled = args.startup_action == "enable"
            set_startup_enabled(enabled)
            store.set("startup.open_at_login", enabled)
            print(f"开机启动已{'启用' if enabled else '禁用'}")
            return 0

    parser.print_help()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    context = None
    try:
        if args.command is None:
            if args.cli or not qt_available():
                print("PySide6 GUI 不可用或已指定 --cli，进入 CLI 模式。")
                parser.print_help()
                return 0
            context = build_context(args.data_dir, plugins_dir=args.plugins_dir)
            return launch_gui(context, start_minimized=args.minimized)
        if args.command == "gui":
            context = build_context(args.data_dir, plugins_dir=args.plugins_dir)
            return launch_gui(context, start_minimized=args.minimized)
        needs_context = args.command not in {"settings", "diagnose"}
        if needs_context:
            context = build_context(args.data_dir, plugins_dir=args.plugins_dir)
            setup_logging(context.data_dir)
        return _dispatch(args, parser, context)
    except ConfirmationRequired as exc:
        print(f"需要确认：{exc}", file=sys.stderr)
        if exc.preview:
            _print_json(exc.preview)
        return 2
    except GuiUnavailable as exc:
        print(f"GUI 启动失败：{exc}", file=sys.stderr)
        print("可继续使用 CLI：python -m quickdeck --help", file=sys.stderr)
        return 1
    except (SettingsError, ToolError, CommandRegistryError, PluginError,
            ActionError, WorkflowError, StartupError, UnitConversionError,
            ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130
    except Exception as exc:  # 兜底：CLI 不因单次失败崩溃
        print(f"未预期错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        if context is not None:
            try:
                log_exception(context.data_dir, "cli_unexpected", exc, argv=list(argv or sys.argv))
            except Exception:
                pass
        return 1
    finally:
        if context is not None:
            context.close()
