"""命令行界面：扫描 / 快照 / 对比 / 重复 / 建议 / 观察 / 隔离 / 回滚。

GUI 不可用时的完整回退入口。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .diff_engine import compare_scans
from .duplicates import DuplicateReport, duplicates_to_csv, find_duplicates_from_store
from .executor import (
    Executor,
    ExecutorError,
    build_quarantine_plan,
    load_plan,
    preview_text,
    save_plan,
)
from .models import (
    ActionKind,
    DiffResult,
    RiskLevel,
    ScanResult,
    human_size,
)
from .policy import (
    ExcludeRules,
    add_watch_target,
    default_db_path,
    default_log_path,
    default_quarantine_dir,
    evaluate_watch_targets,
    generate_recommendations,
    is_critical_path,
    load_config,
    rules_from_config,
)
from .scanner import ScanError, ScanOptions, Scanner
from .snapshot_store import SnapshotStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="spacepilot",
        description="SpacePilot - Windows 磁盘空间分析与安全治理平台（默认只读）",
        epilog="提示：图形界面使用 python -m spacepilot --gui；本帮助为 CLI 模式。",
    )
    parser.add_argument("--db", default=str(default_db_path()), help="SQLite 快照数据库路径")
    parser.add_argument("--version", action="version", version="SpacePilot 0.1.0")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    scan = sub.add_parser("scan", help="扫描目录并保存快照")
    scan.add_argument("root", help="要扫描的根目录")
    scan.add_argument("--exclude-dir", action="append", default=[], help="排除的目录名（可多次）")
    scan.add_argument("--exclude-suffix", action="append", default=[], help="排除的文件后缀（可多次，如 .tmp）")
    scan.add_argument("--min-size", type=int, default=0, help="忽略小于该字节数的文件")
    scan.add_argument("--config", help="JSON 配置文件（排除规则等）")
    scan.add_argument("--hash", action="store_true", help="为文件计算 SHA-256（对比移动/改名需要）")
    scan.add_argument("--follow-symlinks", action="store_true", help="跟随符号链接（默认关闭并记录 skipped）")
    scan.add_argument("--top", type=int, default=15, help="展示前 N 个目录/文件/类型")
    scan.add_argument("--json-out", help="导出扫描摘要 JSON")
    scan.add_argument("--csv-out", help="导出条目 CSV")
    scan.add_argument("--quiet", action="store_true", help="不显示扫描进度")
    scan.set_defaults(func=cmd_scan)

    snapshots = sub.add_parser("snapshots", help="列出已保存的扫描快照")
    snapshots.add_argument("--limit", type=int, default=50)
    snapshots.add_argument("--json", action="store_true", help="以 JSON 输出")
    snapshots.set_defaults(func=cmd_snapshots)

    files = sub.add_parser("files", help="按大小/时间/路径排序过滤文件，可只输出路径便于复制")
    files.add_argument("scan_id", nargs="?", help="快照 ID，默认最新")
    files.add_argument("--min-size", type=int, help="最小字节数")
    files.add_argument("--max-size", type=int, help="最大字节数")
    files.add_argument("--suffix", help="按后缀精确过滤，例如 .log")
    files.add_argument("--contains", help="路径包含的子串")
    files.add_argument("--sort", choices=["size", "mtime", "path", "name"], default="size")
    files.add_argument("--asc", action="store_true", help="升序（默认降序）")
    files.add_argument("--limit", type=int, default=50)
    files.add_argument("--copy", action="store_true", help="只输出绝对路径，便于复制到剪贴板")
    files.add_argument("--json-out", help="导出过滤结果 JSON")
    files.add_argument("--csv-out", help="导出过滤结果 CSV")
    files.set_defaults(func=cmd_files)

    compare = sub.add_parser("compare", help="比较两个快照，识别变化")
    compare.add_argument("old", help="旧快照 ID（可用前缀）")
    compare.add_argument("new", help="新快照 ID（可用前缀）")
    compare.add_argument("--json-out", help="导出差异 JSON")
    compare.add_argument("--top", type=int, default=20)
    compare.set_defaults(func=cmd_compare)

    duplicates = sub.add_parser("duplicates", help="检测重复文件与潜在可回收空间")
    duplicates.add_argument("scan_id", nargs="?", help="快照 ID，默认最新")
    duplicates.add_argument("--min-size", type=int, default=4096, help="参与检测的最小文件大小（字节）")
    duplicates.add_argument("--top", type=int, default=20)
    duplicates.add_argument("--json-out", help="导出 JSON")
    duplicates.add_argument("--csv-out", help="导出 CSV")
    duplicates.set_defaults(func=cmd_duplicates)

    suggest = sub.add_parser("suggest", help="生成带证据的清理 / 隔离建议")
    suggest.add_argument("scan_id", nargs="?", help="快照 ID，默认最新")
    suggest.add_argument("--min-size", type=int, default=4096, help="重复检测最小文件大小（字节）")
    suggest.add_argument("--top", type=int, default=20)
    suggest.add_argument("--json-out", help="导出 JSON")
    suggest.add_argument("--csv-out", help="导出 CSV")
    suggest.set_defaults(func=cmd_suggest)

    watch = sub.add_parser("watch", help="目录增长观察目标")
    watch_group = watch.add_mutually_exclusive_group(required=True)
    watch_group.add_argument("--add", metavar="REL_PATH", help="添加观察目标（相对根目录）")
    watch_group.add_argument("--list", action="store_true", help="列出观察目标")
    watch_group.add_argument("--check", metavar="SCAN_ID", help="按快照检查是否超过阈值")
    watch.add_argument("--threshold", type=int, default=10 * 1024 * 1024, help="增长阈值（字节）")
    watch.add_argument("--scan-id", help="建立基线所用的快照 ID，默认最新")
    watch.add_argument("--json", action="store_true")
    watch.set_defaults(func=cmd_watch)

    quarantine = sub.add_parser("quarantine", help="生成隔离计划；或执行已保存的计划（需令牌）")
    quarantine.add_argument("paths", nargs="*", help="要隔离的文件路径")
    quarantine.add_argument("--dest", default=str(default_quarantine_dir()), help="隔离根目录")
    quarantine.add_argument("--root", help="用于保留相对目录结构的扫描根目录")
    quarantine.add_argument("--save-plan", help="把预览后的计划保存为 JSON")
    quarantine.add_argument("--execute-plan", help="执行已保存的计划 JSON")
    quarantine.add_argument("--token", help="执行时所需的预览令牌")
    quarantine.add_argument("--log", default=str(default_log_path()), help="执行日志路径")
    quarantine.set_defaults(func=cmd_quarantine)

    rollback = sub.add_parser("rollback", help="回滚某次隔离执行（仅隔离副本）")
    rollback.add_argument("execution_id", help="执行 ID")
    rollback.add_argument("--log", default=str(default_log_path()))
    rollback.set_defaults(func=cmd_rollback)

    return parser


def _open_store(args: argparse.Namespace) -> SnapshotStore:
    return SnapshotStore(args.db)


def _resolve(store: SnapshotStore, scan_id: Optional[str]) -> str:
    if scan_id:
        return store.resolve_scan_id(scan_id)
    latest = store.latest_scan()
    if latest is None:
        raise ValueError("数据库中没有扫描快照，请先运行 scan")
    return latest.scan_id


def _print_scan_result(store: SnapshotStore, result: ScanResult, top: int) -> None:
    stats = result.stats
    print(f"扫描完成: {result.scan_id}")
    print(f"根目录: {result.root}")
    print(f"开始: {stats.started_at}  结束: {stats.finished_at}  耗时: {stats.duration_seconds:.2f}s")
    print(
        f"文件: {stats.files_scanned} 个，总计 {human_size(stats.total_size)}；"
        f"目录: {stats.dirs_scanned} 个"
    )
    print(
        f"跳过: {stats.skipped}（文件 {stats.files_skipped} / 目录 {stats.dirs_skipped} / "
        f"符号链接 {stats.symlinks_skipped}）；失败: {stats.failed}；"
        f"0 字节: {stats.zero_byte}；volatile: {stats.volatile}"
    )
    print(f"完整度: {stats.completeness_label}（{stats.completeness_pct:.2f}%）")
    if stats.failed:
        print("失败项（不能当作不存在）:")
        for item in store.failed_entries(result.scan_id, limit=10):
            print(f"  [失败] {item['rel_path'] or item['path']} :: {item['reason']}")

    print("\n目录占用 Top:")
    for row in store.largest_dirs(result.scan_id, limit=top):
        print(f"  {human_size(row['size']):>10}  {row['file_count']:>6} 文件  {row['rel_path'] or '(根目录)'}")
    print("\n最大文件 Top:")
    for row in store.largest_files(result.scan_id, limit=top):
        flag = " [volatile]" if row["volatile"] else ""
        print(f"  {row['size_human']:>10}  {row['rel_path']}{flag}")
    print("\n类型分布 Top:")
    for row in store.extension_stats(result.scan_id, limit=top):
        print(f"  {row['suffix']:<12} {row['total_size_human']:>10}  {row['file_count']} 个")


def cmd_scan(args: argparse.Namespace) -> int:
    store = _open_store(args)
    try:
        rules = ExcludeRules()
        if args.config:
            rules = rules_from_config(load_config(args.config), base=rules)
        rules = ExcludeRules(
            dir_names=rules.dir_names | frozenset(args.exclude_dir),
            suffixes=rules.suffixes | frozenset(args.exclude_suffix),
            min_file_size=max(rules.min_file_size, args.min_size),
        )
        options = ScanOptions(
            rules=rules,
            follow_symlinks=args.follow_symlinks,
            hash_files=args.hash,
        )

        def on_progress(payload: dict[str, Any]) -> None:
            if args.quiet:
                return
            processed = payload["processed"]
            if payload["phase"] == "scanning" and processed:
                if processed % 500 == 0:
                    print(
                        f"\r已处理 {processed} 项，文件 {payload['files_scanned']}，"
                        f"大小 {human_size(payload['total_size'])}，失败 {payload['failed']}",
                        end="",
                        file=sys.stderr,
                        flush=True,
                    )

        result = Scanner(
            args.root,
            store,
            options=options,
            progress_callback=on_progress,
        ).scan()
        if not args.quiet:
            print("\r", end="", file=sys.stderr)
        _print_scan_result(store, result, args.top)
        if args.json_out:
            print(f"JSON 已导出: {store.export_json(result.scan_id, args.json_out, top=args.top)}")
        if args.csv_out:
            print(f"CSV 已导出: {store.export_entries_csv(result.scan_id, args.csv_out)}")
        return 0
    finally:
        store.close()


def cmd_snapshots(args: argparse.Namespace) -> int:
    store = _open_store(args)
    try:
        scans = store.list_scans(limit=args.limit)
        if args.json:
            print(json.dumps([scan.to_dict() for scan in scans], ensure_ascii=False, indent=2))
            return 0
        if not scans:
            print("暂无快照，请先运行 scan")
            return 0
        print(f"{'ID':<14}{'根目录':<40}{'文件':>8}{'大小':>12}{'失败':>6}  {'完成度'}")
        for scan in scans:
            root = scan.root if len(scan.root) <= 38 else "…" + scan.root[-37:]
            print(
                f"{scan.scan_id[:12]:<14}{root:<40}{scan.stats.files_scanned:>8}"
                f"{human_size(scan.stats.total_size):>12}{scan.stats.failed:>6}  {scan.stats.completeness_label}"
            )
        return 0
    finally:
        store.close()


def cmd_files(args: argparse.Namespace) -> int:
    store = _open_store(args)
    try:
        scan_id = _resolve(store, args.scan_id)
        suffix = args.suffix
        if suffix and suffix != "(无后缀)" and not suffix.startswith("."):
            suffix = "." + suffix
        entries = store.query_entries(
            scan_id,
            min_size=args.min_size,
            max_size=args.max_size,
            suffix=suffix,
            contains=args.contains,
            sort_by=args.sort,
            descending=not args.asc,
            limit=args.limit,
        )
        if args.copy:
            for entry in entries:
                print(entry.path)
        else:
            print(
                f"快照 {scan_id[:12]}：{len(entries)} 个文件；排序 {args.sort} "
                f"{'升序' if args.asc else '降序'}，limit={args.limit}"
            )
            print(f"{'大小':>12}  {'修改时间':<19} 路径")
            for entry in entries:
                mtime = datetime.fromtimestamp((entry.mtime_ns or 0) / 1_000_000_000).strftime("%Y-%m-%d %H:%M:%S")
                print(f"{human_size(entry.size):>12}  {mtime:<19} {entry.path}")

        if args.json_out:
            path = Path(args.json_out)
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "scan_id": scan_id,
                "filters": {
                    "min_size": args.min_size,
                    "max_size": args.max_size,
                    "suffix": suffix,
                    "contains": args.contains,
                    "sort": args.sort,
                    "ascending": args.asc,
                    "limit": args.limit,
                },
                "entries": [entry.to_dict() for entry in entries],
            }
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"JSON 已导出: {path}")
        if args.csv_out:
            path = Path(args.csv_out)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["rel_path", "path", "size", "mtime_ns", "suffix", "sha256"])
                for entry in entries:
                    writer.writerow(
                        [entry.rel_path, entry.path, entry.size or 0, entry.mtime_ns, entry.suffix, entry.sha256 or ""]
                    )
            print(f"CSV 已导出: {path}")
        return 0
    finally:
        store.close()


def _print_diff(diff: DiffResult, top: int) -> None:
    print(f"对比: {diff.old_scan_id[:12]} -> {diff.new_scan_id[:12]}")
    counts = diff.counts
    print(
        "变化统计: "
        + "，".join(
            f"{label} {counts.get(key, 0)}"
            for key, label in (
                ("added", "新增"),
                ("deleted", "删除"),
                ("grown", "变大"),
                ("shrunk", "变小"),
                ("modified", "内容修改"),
                ("moved", "移动"),
                ("renamed", "改名"),
            )
        )
    )
    print(
        f"净变化: {human_size(diff.net_change)}；未变化: {diff.unchanged_count} 个文件；"
        f"新增字节 {human_size(diff.added_bytes)}，删除字节 {human_size(diff.deleted_bytes)}"
    )
    if diff.dir_moves:
        print("\n目录移动（需要大小 + SHA-256 证据）:")
        for move in diff.dir_moves:
            print(f"  {move.old_rel} -> {move.new_rel}  ({move.file_count} 文件，{human_size(move.moved_bytes)})")
    if diff.dir_growth:
        print("\n目录净增长 Top:")
        for growth in diff.dir_growth[:top]:
            print(f"  +{human_size(growth.delta):>10}  {growth.rel_path or '(根目录)'}  ({human_size(growth.old_size)} -> {human_size(growth.new_size)})")
    sources = diff.growth_sources(limit=top)
    if sources:
        print("\n主要增长来源:")
        for source in sources:
            print(f"  +{source['delta_human']:>10}  {source['path']}")
    print("\n变化明细 Top:")
    for change in sorted(diff.changes, key=lambda c: -abs(c.delta))[:top]:
        target = change.new_path or change.old_path
        print(f"  [{change.change_type}] {target}  {change.evidence}")
    for note in diff.notes:
        print(f"注意: {note}")


def cmd_compare(args: argparse.Namespace) -> int:
    store = _open_store(args)
    try:
        old_id = store.resolve_scan_id(args.old)
        new_id = store.resolve_scan_id(args.new)
        diff = compare_scans(store, old_id, new_id)
        _print_diff(diff, args.top)
        if args.json_out:
            path = Path(args.json_out)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(diff.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"JSON 已导出: {path}")
        return 0
    finally:
        store.close()


def _print_duplicates(report: DuplicateReport, top: int) -> None:
    print(f"重复检测: {report.scan_id[:12]}；阈值 {human_size(report.threshold)}")
    print(
        f"扫描文件 {report.scanned_files}，候选 {report.candidate_files}，"
        f"计算哈希 {report.hashed_files}，重复组 {len(report.groups)}"
    )
    print(f"潜在可回收空间: {human_size(report.total_reclaimable)}")
    for group in report.groups[:top]:
        print(f"\n  [{human_size(group.size)} x {len(group.files)}] SHA-256 {group.sha256[:20]}…")
        for file in group.files:
            hardlink = f"  (硬链接 -> {file.hardlink_of})" if file.hardlink_of else ""
            print(f"    {file.path}{hardlink}")
        print(f"    可回收: {human_size(group.reclaimable)}；硬链接跳过 {group.hardlink_skipped}")
    if report.errors:
        print(f"\n无法哈希 {len(report.errors)} 个文件:")
        for error in report.errors[:5]:
            print(f"  {error['path']}: {error['error']}")


def cmd_duplicates(args: argparse.Namespace) -> int:
    store = _open_store(args)
    try:
        scan_id = _resolve(store, args.scan_id)
        report = find_duplicates_from_store(store, scan_id, min_size=args.min_size)
        _print_duplicates(report, args.top)
        if args.json_out:
            path = Path(args.json_out)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"JSON 已导出: {path}")
        if args.csv_out:
            path = Path(args.csv_out)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerows(duplicates_to_csv(report))
            print(f"CSV 已导出: {path}")
        return 0
    finally:
        store.close()


def cmd_suggest(args: argparse.Namespace) -> int:
    store = _open_store(args)
    try:
        scan_id = _resolve(store, args.scan_id)
        report = find_duplicates_from_store(store, scan_id, min_size=args.min_size)
        recommendations = generate_recommendations(store, scan_id, duplicates=report)
        print(f"清理建议: {scan_id[:12]}；共 {len(recommendations)} 条（默认只读，删除需显式开启）")
        print(f"{'风险':<8}{'动作':<12}{'收益':>10}  {'分类':<12} 路径")
        for rec in recommendations[: args.top]:
            print(
                f"{rec.risk:<8}{rec.action:<12}{human_size(rec.benefit_bytes):>10}  "
                f"{rec.category:<12} {rec.path}"
            )
            print(f"    证据: {rec.evidence}")
            print(f"    原因: {rec.reason}")
        total = sum(rec.benefit_bytes for rec in recommendations)
        print(f"\n建议收益合计: {human_size(total)}（隔离可回滚，删除不可撤销）")
        if args.json_out:
            path = Path(args.json_out)
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "scan_id": scan_id,
                "duplicates": report.to_dict(),
                "recommendations": [rec.to_dict() for rec in recommendations],
            }
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"JSON 已导出: {path}")
        if args.csv_out:
            path = Path(args.csv_out)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["rec_id", "action", "category", "risk", "benefit_bytes", "path", "reason", "evidence"])
                for rec in recommendations:
                    writer.writerow(
                        [rec.rec_id, rec.action, rec.category, rec.risk, rec.benefit_bytes, rec.path, rec.reason, rec.evidence]
                    )
            print(f"CSV 已导出: {path}")
        return 0
    finally:
        store.close()


def cmd_watch(args: argparse.Namespace) -> int:
    store = _open_store(args)
    try:
        if args.list:
            targets = store.list_watch_targets()
            if args.json:
                print(json.dumps([t.to_dict() for t in targets], ensure_ascii=False, indent=2))
            else:
                if not targets:
                    print("暂无观察目标")
                for target in targets:
                    print(
                        f"{target.rel_path}: 阈值 {human_size(target.threshold_bytes)}，"
                        f"基线 {human_size(target.baseline_bytes)}"
                    )
            return 0
        if args.add:
            target = add_watch_target(store, args.add, args.threshold, args.scan_id)
            print(
                f"已添加观察目标 {target.rel_path}，阈值 {human_size(target.threshold_bytes)}，"
                f"基线 {human_size(target.baseline_bytes)}"
            )
            return 0
        scan_id = _resolve(store, args.check)
        alerts = evaluate_watch_targets(store, scan_id)
        if args.json:
            print(json.dumps([a.to_dict() for a in alerts], ensure_ascii=False, indent=2))
        else:
            if not alerts:
                print("暂无观察目标")
            for alert in alerts:
                status = "超过阈值" if alert.exceeded else "正常"
                print(
                    f"{alert.rel_path}: 基线 {human_size(alert.baseline_bytes)} -> "
                    f"当前 {human_size(alert.current_bytes)}，增长 {human_size(alert.growth)}，{status}"
                )
        return 0
    finally:
        store.close()


def cmd_quarantine(args: argparse.Namespace) -> int:
    if args.execute_plan:
        plan = load_plan(args.execute_plan)
        executor = Executor(
            quarantine_root=args.dest,
            log_path=args.log,
        )
        execution = executor.execute(plan, confirm=True, token=args.token or "")
        print(f"执行 {execution.execution_id}: 成功 {execution.succeeded}，失败 {execution.failed}")
        for result in execution.results:
            print(f"  [{result.status}] {result.kind} {result.source} -> {result.target or ''} {result.message}")
        print(f"日志: {execution.log_path}")
        return 0 if execution.failed == 0 else 2

    if not args.paths:
        print("错误: 需要至少一个待隔离文件路径（或使用 --execute-plan）", file=sys.stderr)
        return 1

    plan = build_quarantine_plan(
        args.paths,
        quarantine_root=args.dest,
        root=args.root,
        risks={
            os.path.abspath(path): RiskLevel.HIGH.value if is_critical_path(os.path.abspath(path)) else RiskLevel.MEDIUM.value
            for path in args.paths
        },
    )
    print(preview_text(plan))
    if args.save_plan:
        save_plan(plan, args.save_plan)
        print(f"计划已保存: {args.save_plan}")
        print(f"执行示例: python -m spacepilot quarantine {' '.join(args.paths)} "
              f"--dest \"{args.dest}\" --execute-plan \"{args.save_plan}\" --token {plan.token}")
    else:
        print("当前为预览模式，未修改任何文件。使用 --save-plan 保存计划后再执行。")
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    executor = Executor(log_path=args.log)
    result = executor.rollback(args.execution_id)
    print(
        f"回滚 {result.execution_id}: 恢复 {result.restored}，跳过 {result.skipped}，失败 {result.failed}"
    )
    for detail in result.details:
        print(f"  [{detail['status']}] {detail['source']} {detail.get('message', '')}")
    return 0 if result.failed == 0 else 2


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args))
    except (ValueError, ScanError, OSError, ExecutorError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
