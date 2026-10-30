"""RunTrace 命令行界面（PySide6 不可用时的完整回退界面）。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .command_builder import ConfigError, RunConfig, build_command, env_overrides_from_pairs
from .diff_engine import compare_runs, render_comparison
from .process_runner import ProcessRunner, RunOutcome
from .project_context import get_git_context, project_label
from .replay_manager import ReplayConfirmationRequired, ReplayError, ReplayManager
from .run_store import RunStore, default_db_path, recover_interrupted_runs

_STATUS_LABELS = {
    "running": "运行中",
    "success": "成功",
    "failed": "失败",
    "timeout": "超时",
    "cancelled": "已停止",
    "interrupted": "异常中断",
}


def _status_label(status: str | None) -> str:
    return _STATUS_LABELS.get(status or "", status or "")


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="runtrace",
        description="RunTrace：本地任务运行历史、差异分析与复现平台",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m runtrace run --name demo --cwd . -- python -c \"print('hi')\"\n"
            "  python -m runtrace list --search 失败\n"
            "  python -m runtrace show <run-id> --stream stderr\n"
            "  python -m runtrace diff <run-a> <run-b>\n"
            "  python -m runtrace export <run-id> --format json --output run.json\n"
            "  python -m runtrace replay <run-id> --output replay.zip\n"
            '  python -m runtrace run --shell --name risk -- "echo hello"\n'
        ),
    )
    parser.add_argument("--db", help=f"SQLite 数据库路径（默认 {default_db_path()}）")
    parser.add_argument("--version", action="version", version=f"RunTrace {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    run_p = sub.add_parser("run", help="新建并执行一个任务")
    run_p.add_argument("--name", default="")
    run_p.add_argument("--cwd", default=os.getcwd(), help="工作目录")
    run_p.add_argument("--timeout", type=float, default=None, help="超时秒数")
    run_p.add_argument("--shell", action="store_true", help="显式启用有风险的 shell 模式")
    run_p.add_argument("--env", action="append", default=[], metavar="KEY=VALUE")
    run_p.add_argument("--project", default=None, help="关联项目目录（默认使用 cwd）")
    run_p.add_argument("--stream", action="store_true", help="在终端实时显示 stdout/stderr")
    run_p.add_argument("--json", action="store_true", help="仅输出运行结果 JSON")
    run_p.add_argument(
        "cmd",
        nargs=argparse.REMAINDER,
        help="命令及参数；建议在选项后加 -- 分隔，如 -- python -c \"print(1)\"",
    )

    list_p = sub.add_parser("list", aliases=["ls"], help="列出运行历史")
    list_p.add_argument("--search", "-q", default=None, help="全文搜索命令、路径、日志")
    list_p.add_argument(
        "--status",
        choices=["running", "success", "failed", "timeout", "cancelled", "interrupted"],
    )
    list_p.add_argument("--project", default=None, help="按项目/cwd 路径筛选")
    list_p.add_argument("--since", default=None, help="起始时间，ISO 或 YYYY-MM-DD")
    list_p.add_argument("--until", default=None, help="结束时间，ISO 或 YYYY-MM-DD")
    list_p.add_argument("--limit", type=int, default=50)
    list_p.add_argument("--json", action="store_true")

    show_p = sub.add_parser("show", help="查看一次运行及日志")
    show_p.add_argument("run_id")
    show_p.add_argument("--stream", choices=["stdout", "stderr"], default=None)
    show_p.add_argument("--json", action="store_true")
    show_p.add_argument("--max-log-chars", type=int, default=0, help="0 表示不截断")

    diff_p = sub.add_parser("diff", help="对比两次运行")
    diff_p.add_argument("run_a")
    diff_p.add_argument("run_b")
    diff_p.add_argument("--json", action="store_true")

    export_p = sub.add_parser("export", help="导出运行数据")
    export_p.add_argument("run_id")
    export_p.add_argument("--format", choices=["json", "text", "zip"], default="json")
    export_p.add_argument("--output", default=None)

    replay_p = sub.add_parser("replay", help="生成复现包（不会执行）")
    replay_p.add_argument("run_id")
    replay_p.add_argument("--output", default=None)
    replay_p.add_argument("--json", action="store_true")

    inspect_p = sub.add_parser("replay-inspect", help="检查复现包完整性与风险")
    inspect_p.add_argument("package")
    inspect_p.add_argument("--json", action="store_true")

    exec_p = sub.add_parser("replay-execute", help="执行复现包（必须显式确认）")
    exec_p.add_argument("package")
    exec_p.add_argument("--yes", action="store_true", help="确认命令、目录和风险后执行")
    exec_p.add_argument("--stream", action="store_true")

    project_p = sub.add_parser("project", help="只读查看项目/Git 上下文")
    project_p.add_argument("path", nargs="?", default=os.getcwd())
    project_p.add_argument("--json", action="store_true")

    rerun_p = sub.add_parser("rerun", help="使用历史运行的非敏感配置再次运行")
    rerun_p.add_argument("run_id")
    rerun_p.add_argument("--timeout", type=float, default=None)
    rerun_p.add_argument("--stream", action="store_true")

    templates_p = sub.add_parser("templates", help="列出任务模板")
    templates_p.add_argument("--json", action="store_true")

    tpl_save = sub.add_parser("template-save", help="保存/覆盖任务模板")
    tpl_save.add_argument("name")
    tpl_save.add_argument("--cwd", default=os.getcwd())
    tpl_save.add_argument("--command", required=True)
    tpl_save.add_argument("--arg", action="append", default=[])
    tpl_save.add_argument("--env", action="append", default=[])
    tpl_save.add_argument("--timeout", type=float, default=None)
    tpl_save.add_argument("--shell", action="store_true")
    tpl_save.add_argument("--project", default=None)

    tpl_run = sub.add_parser("template-run", help="运行任务模板")
    tpl_run.add_argument("name")
    tpl_run.add_argument("--stream", action="store_true")

    tpl_copy = sub.add_parser("template-copy", help="复制任务模板")
    tpl_copy.add_argument("source")
    tpl_copy.add_argument("new_name")

    tpl_delete = sub.add_parser("template-delete", help="删除任务模板")
    tpl_delete.add_argument("name")

    recover_p = sub.add_parser("recover", help="把遗留的 running 记录标记为 interrupted")
    recover_p.add_argument("--json", action="store_true")

    return parser


def _open_store(args: argparse.Namespace) -> RunStore:
    return RunStore(Path(args.db).expanduser() if args.db else default_db_path())


def _execute(
    store: RunStore,
    config: RunConfig,
    *,
    stream: bool,
    timeout: float | None = None,
) -> RunOutcome:
    build = build_command(config)
    print(f"命令: {build.display}")
    print(f"工作目录: {build.cwd}")
    if build.shell:
        print("模式: shell（显式启用）")
        if build.warning:
            print(f"警告: {build.warning}", file=sys.stderr)
    else:
        print("模式: 参数数组（shell=False）")
    if build.warning and not build.shell:
        print(f"提示: {build.warning}", file=sys.stderr)
    if config.timeout is not None:
        print(f"超时: {config.timeout} 秒")
    print("-" * 60)
    runner = ProcessRunner(store)

    def on_output(stream_name: str, text: str) -> None:
        target = sys.stdout if stream_name == "stdout" else sys.stderr
        target.write(text)
        target.flush()

    outcome = runner.run(
        config,
        on_output=on_output if stream else None,
    )
    if stream:
        print("\n" + "-" * 60)
    summary = outcome.to_dict()
    summary["status_label"] = _status_label(outcome.status)
    print(f"Run ID: {outcome.run_id}")
    print(
        f"状态: {_status_label(outcome.status)} | 退出码: {outcome.exit_code} | "
        f"耗时: {outcome.duration_ms} ms"
    )
    if outcome.error:
        print(f"错误: {outcome.error}", file=sys.stderr)
    print(f"查看日志: python -m runtrace show {outcome.run_id}")
    return outcome


def _format_run_line(run: dict[str, Any]) -> str:
    started = (run.get("started_at") or "")[:19].replace("T", " ")
    exit_code = "-" if run.get("exit_code") is None else str(run.get("exit_code"))
    duration = run.get("duration_ms")
    duration_text = f"{duration}ms" if duration is not None else "-"
    cwd = run.get("cwd") or ""
    if len(cwd) > 46:
        cwd = "..." + cwd[-43:]
    return (
        f"{run['id']}  {_status_label(run.get('status')):<6}  exit={exit_code:<5}  "
        f"{duration_text:>10}  {started}  {run.get('name')}  [{cwd}]"
    )


def _print_run_detail(run: dict[str, Any], store: RunStore, *, max_log_chars: int = 0) -> None:
    print(f"Run ID: {run['id']}")
    print(f"名称: {run['name']}")
    print(f"状态: {_status_label(run['status'])} | 退出码: {run['exit_code']} | 耗时: {run['duration_ms']} ms")
    print(f"命令: {run['command']}")
    print(f"参数: {json.dumps(run['args'], ensure_ascii=False)}")
    print(f"工作目录: {run['cwd']}")
    print(f"Shell: {'是（有风险）' if run['shell'] else '否'} | 超时: {run['timeout']}")
    print(f"开始: {run['started_at']}")
    print(f"结束: {run['ended_at']}")
    print(f"项目: {run.get('project_path')}")
    git_ctx = run.get("git_context") or {}
    print(
        "Git: "
        + (
            f"branch={git_ctx.get('branch')} commit={git_ctx.get('commit')} "
            f"dirty={git_ctx.get('dirty')}"
            if git_ctx.get("is_git")
            else "非 Git 项目"
        )
    )
    print("环境（已脱敏）:")
    for key, value in sorted((run.get("env") or {}).items()):
        print(f"  {key}={value}")
    stats = store.get_log_stats(run["id"])
    print(
        f"日志统计: stdout {stats['stdout']['chars']} 字符/{stats['stdout']['chunks']} 块，"
        f"stderr {stats['stderr']['chars']} 字符/{stats['stderr']['chunks']} 块"
    )
    for stream_name, title in (("stdout", "STDOUT"), ("stderr", "STDERR")):
        text = store.get_logs(run["id"], stream_name)
        if max_log_chars > 0 and len(text) > max_log_chars:
            text = f"...[仅显示最后 {max_log_chars} 字符]...\n" + text[-max_log_chars:]
        print(f"\n===== {title} =====")
        print(text, end="" if text.endswith("\n") or not text else "\n")


def _load_both(store: RunStore, run_a: str, run_b: str) -> tuple[dict, dict, str, str]:
    a = store.get_run(run_a)
    b = store.get_run(run_b)
    if a is None:
        raise KeyError(f"运行记录不存在：{run_a}")
    if b is None:
        raise KeyError(f"运行记录不存在：{run_b}")
    return a, b, store.get_logs(run_a, "stdout"), store.get_logs(run_b, "stdout")


def _prompt_confirmation(info: dict[str, Any]) -> bool:
    manifest = info["manifest"]
    print("复现包校验通过。即将执行的配置：")
    config = info.get("config") or {}
    print(f"  命令: {config.get('command')}")
    print(f"  参数: {json.dumps(config.get('args') or [], ensure_ascii=False)}")
    print(f"  工作目录: {config.get('cwd')}")
    print(f"  shell: {config.get('shell')}")
    print("风险提示:")
    for warning in manifest.get("warnings") or []:
        print(f"  - {warning}")
    print("未记录/无法保证:")
    for gap in manifest.get("environment_gaps") or []:
        print(f"  - {gap}")
    if not sys.stdin.isatty():
        print("非交互终端：必须使用 --yes 显式确认后才能执行。", file=sys.stderr)
        return False
    answer = input("确认执行？输入 YES 继续: ").strip()
    return answer == "YES"


def _handle(args: argparse.Namespace, store: RunStore) -> int:
    command = args.command
    if command in ("run",):
        raw = list(args.cmd)
        if raw and raw[0] == "--":
            raw = raw[1:]
        if not raw:
            print("错误: 需要提供命令，例如 run -- python -c \"print(1)\"", file=sys.stderr)
            return 2
        try:
            env = env_overrides_from_pairs(args.env)
            config = RunConfig(
                name=args.name,
                cwd=args.cwd,
                command=raw[0],
                args=raw[1:],
                env_overrides=env,
                timeout=args.timeout,
                shell=bool(args.shell),
                project_path=args.project,
            )
        except ConfigError as exc:
            print(f"配置错误: {exc}", file=sys.stderr)
            return 2
        try:
            outcome = _execute(store, config, stream=bool(args.stream))
        except ConfigError as exc:
            print(f"配置错误: {exc}", file=sys.stderr)
            return 2
        if args.json:
            _print_json(outcome.to_dict())
        return 0 if outcome.status == "success" else 1

    if command in ("list", "ls"):
        runs = store.list_runs(
            search=args.search,
            status=args.status,
            project=args.project,
            since=args.since,
            until=args.until,
            limit=args.limit,
        )
        if args.json:
            _print_json(runs)
        else:
            if not runs:
                print("没有匹配的运行记录。")
            for run in runs:
                print(_format_run_line(run))
        return 0

    if command == "show":
        run = store.get_run(args.run_id)
        if run is None:
            print(f"错误: 运行记录不存在：{args.run_id}", file=sys.stderr)
            return 1
        if args.json:
            payload = store.build_export_payload(args.run_id)
            if args.stream:
                payload["logs"] = {args.stream: store.get_logs(args.run_id, args.stream)}
            _print_json(payload)
        elif args.stream:
            print(store.get_logs(args.run_id, args.stream), end="")
        else:
            _print_run_detail(run, store, max_log_chars=args.max_log_chars)
        return 0

    if command == "diff":
        try:
            a, b, stdout_a, stdout_b = _load_both(store, args.run_a, args.run_b)
        except KeyError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 1
        report = compare_runs(
            a,
            b,
            stdout_a=stdout_a,
            stdout_b=stdout_b,
            stderr_a=store.get_logs(args.run_a, "stderr"),
            stderr_b=store.get_logs(args.run_b, "stderr"),
        )
        if args.json:
            _print_json(report.to_dict())
        else:
            print(render_comparison(report))
        return 0

    if command == "export":
        ext = {"json": "json", "text": "txt", "zip": "zip"}[args.format]
        output = args.output or f"runtrace_{args.run_id}.{ext}"
        try:
            result = store.export_run(args.run_id, output, args.format)
        except KeyError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 1
        print(f"已导出: {result['path']}")
        print(f"SHA-256: {result['sha256']}")
        if result["sensitive_findings"]:
            print(
                f"安全提示: 发现 {len(result['sensitive_findings'])} 处疑似敏感内容，"
                "导出文件可能包含明文信息。",
                file=sys.stderr,
            )
        return 0

    if command == "replay":
        output = args.output or f"runtrace_replay_{args.run_id}.zip"
        try:
            info = ReplayManager(store).build_package(args.run_id, output)
        except KeyError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 1
        if args.json:
            _print_json(info.to_dict())
        else:
            print(f"复现包: {info.path}")
            print(f"Run ID: {info.run_id}")
            print("警告:")
            for warning in info.warnings:
                print(f"  - {warning}")
            print("未记录/外部依赖:")
            for gap in info.environment_gaps:
                print(f"  - {gap}")
            for dep in info.external_dependencies:
                print(f"  - {dep['name']}: {dep['resolved_path'] or '未解析'}")
            if info.sensitive_findings:
                print(
                    f"安全提示: 发现 {len(info.sensitive_findings)} 处疑似敏感内容。",
                    file=sys.stderr,
                )
            print(f"检查: python -m runtrace replay-inspect {info.path}")
        return 0

    if command == "replay-inspect":
        try:
            info = ReplayManager(store).inspect_package(args.package)
        except ReplayError as exc:
            print(f"复现包错误: {exc}", file=sys.stderr)
            return 1
        if args.json:
            _print_json(info)
        else:
            manifest = info["manifest"]
            print(f"校验: {'通过' if info['checksums_valid'] else '失败'}")
            print(f"Run ID: {info['run_id']}")
            print(f"命令: {manifest.get('command')} {json.dumps(manifest.get('args') or [], ensure_ascii=False)}")
            print(f"工作目录: {manifest.get('cwd')}")
            print(f"shell: {manifest.get('shell')}")
            print("警告:")
            for warning in manifest.get("warnings") or []:
                print(f"  - {warning}")
            print("未记录:")
            for gap in manifest.get("environment_gaps") or []:
                print(f"  - {gap}")
            print("外部依赖:")
            for dep in manifest.get("external_dependencies") or []:
                print(f"  - {dep.get('name')}: {dep.get('resolved_path') or '未解析'}")
        return 0

    if command == "replay-execute":
        manager = ReplayManager(store)
        try:
            info = manager.inspect_package(args.package)
        except ReplayError as exc:
            print(f"复现包错误: {exc}", file=sys.stderr)
            return 1
        confirmed = bool(args.yes)
        if not confirmed:
            confirmed = _prompt_confirmation(info)
            if not confirmed:
                return 3
        try:
            outcome = manager.execute_package(
                args.package,
                confirmed=True,
                on_output=(
                    (lambda stream, text: (
                        sys.stdout.write(text) if stream == "stdout" else sys.stderr.write(text)
                    ))
                    if args.stream
                    else None
                ),
            )
        except ReplayConfirmationRequired as exc:
            print(str(exc), file=sys.stderr)
            return 3
        except (ReplayError, ConfigError) as exc:
            print(f"复现失败: {exc}", file=sys.stderr)
            return 1
        print(
            f"\n复现 Run ID: {outcome.run_id} | 状态: {_status_label(outcome.status)} | "
            f"退出码: {outcome.exit_code}"
        )
        return 0 if outcome.status == "success" else 1

    if command == "project":
        ctx = get_git_context(args.path)
        if args.json:
            _print_json(ctx)
        else:
            print(project_label(ctx))
            if ctx.get("error"):
                print(f"说明: {ctx['error']}")
            if ctx.get("is_git"):
                print(f"root: {ctx.get('root')}")
                print(f"branch: {ctx.get('branch') or 'detached'}")
                print(f"commit: {ctx.get('commit')}")
                print(f"dirty: {ctx.get('dirty')}")
        return 0

    if command == "rerun":
        try:
            config = store.config_from_run(args.run_id)
        except KeyError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 1
        if args.timeout is not None:
            config = config.copy(timeout=args.timeout)
        run = store.get_run(args.run_id) or {}
        redacted = run.get("metadata", {}).get("redacted_env_override_keys") or []
        if redacted:
            print(
                "提示: 原运行的部分环境覆盖已脱敏（" + ", ".join(redacted) + "），"
                "再次运行不会自动还原这些值。",
                file=sys.stderr,
            )
        outcome = _execute(store, config, stream=bool(args.stream))
        return 0 if outcome.status == "success" else 1

    if command == "templates":
        templates = store.list_templates()
        if args.json:
            _print_json(templates)
        elif not templates:
            print("尚无任务模板。")
        else:
            for tpl in templates:
                print(
                    f"{tpl['id']:>3}  {tpl['name']}  cwd={tpl['cwd']}  "
                    f"cmd={tpl['command']} shell={tpl['shell']} updated={tpl['updated_at']}"
                )
        return 0

    if command == "template-save":
        try:
            env = env_overrides_from_pairs(args.env)
            store.save_template(
                name=args.name,
                cwd=args.cwd,
                command=args.command,
                args=args.arg,
                env_overrides=env,
                timeout=args.timeout,
                shell=args.shell,
                project_path=args.project,
            )
        except (ConfigError, ValueError) as exc:
            print(f"模板错误: {exc}", file=sys.stderr)
            return 2
        print(f"模板已保存: {args.name}（敏感环境值已脱敏）")
        return 0

    if command == "template-run":
        try:
            config = store.config_from_template(args.name)
        except KeyError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 1
        tpl = store.get_template(args.name) or {}
        if any(value == "***" for value in tpl.get("env_overrides", {}).values()):
            print("提示: 模板中的敏感环境值已脱敏，运行前请通过其他方式重新提供。", file=sys.stderr)
        outcome = _execute(store, config, stream=bool(args.stream))
        return 0 if outcome.status == "success" else 1

    if command == "template-copy":
        try:
            store.copy_template(args.source, args.new_name)
        except KeyError as exc:
            print(f"错误: {exc}", file=sys.stderr)
            return 1
        print(f"已复制模板到: {args.new_name}")
        return 0

    if command == "template-delete":
        deleted = store.delete_template(args.name)
        if not deleted:
            print(f"错误: 模板不存在：{args.name}", file=sys.stderr)
            return 1
        print(f"已删除模板: {args.name}")
        return 0

    if command == "recover":
        count = recover_interrupted_runs(store)
        if args.json:
            _print_json({"recovered": count})
        else:
            print(f"已恢复 {count} 条 running 记录为 interrupted。")
        return 0

    print("未知命令。使用 --help 查看帮助。", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        store = _open_store(args)
    except Exception as exc:
        print(f"无法打开数据库: {exc}", file=sys.stderr)
        return 1
    try:
        try:
            recovered = recover_interrupted_runs(store)
        except Exception as exc:
            print(f"启动恢复失败（继续运行）: {exc}", file=sys.stderr)
            recovered = 0
        if recovered and args.command not in ("recover",):
            print(f"注意: 已将 {recovered} 条遗留的 running 记录标记为 interrupted。", file=sys.stderr)
        return _handle(args, store)
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
