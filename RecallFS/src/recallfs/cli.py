"""命令行界面：索引、搜索、查看片段、集合、收藏、最近搜索、维护。

不依赖任何第三方库；PySide6 不可用时 ``python -m recallfs`` 会回退到这里。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import html
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from . import __app_name__, __version__
from .document_parser import optional_parsers_status
from .evidence_locator import EvidenceLocator
from .fulltext_index import IndexDatabase, IndexVersionError, default_db_path
from .index_scheduler import IndexScheduler
from .project_collections import CollectionStore, FavoritesStore, RecentSearchStore, RelatedFiles
from .search_engine import SearchEngine, SearchFilters
from .semantic_index import MODEL_LABEL, SemanticIndex
from .summary import build_extract_summary, format_locator


def _json_dump(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def parse_time(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        return float(value)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return _dt.datetime.strptime(value, fmt).timestamp()
        except ValueError:
            continue
    try:
        return _dt.datetime.fromisoformat(value).timestamp()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"无法解析时间: {value}") from exc


@dataclass
class Services:
    db: IndexDatabase
    scheduler: IndexScheduler
    engine: SearchEngine
    collections: CollectionStore
    favorites: FavoritesStore
    recent: RecentSearchStore
    semantic: SemanticIndex

    def close(self) -> None:
        self.db.close()


def build_services(db_path: str | os.PathLike[str] | None = None, *, allow_version_reset: bool = False) -> Services:
    db = IndexDatabase(db_path or default_db_path(), allow_version_reset=allow_version_reset)
    scheduler = IndexScheduler(db)
    semantic = SemanticIndex(db)
    collections = CollectionStore(db)
    favorites = FavoritesStore(db)
    recent = RecentSearchStore(db)
    engine = SearchEngine(db, semantic=semantic, collections=collections, recent=recent)
    return Services(db, scheduler, engine, collections, favorites, recent, semantic)


# ---------------------------------------------------------------------------
# 命令实现
# ---------------------------------------------------------------------------
def _headline(text: str) -> None:
    print(f"\n== {text} ==")


def cmd_index(services: Services, args: argparse.Namespace) -> int:
    if args.verify:
        return cmd_verify(services, args)
    if args.clear_root:
        removed = services.scheduler.clear_root(args.clear_root)
        print(f"已清除目录索引（未删除源文件）: {args.clear_root}，移除文件记录 {removed} 条")
        return 0
    if args.retry_failed:
        report = services.scheduler.retry_failed()
        if args.json:
            _json_dump(report)
        else:
            print(f"重试解析 {report['attempted']} 个文件")
            for item in report["results"]:
                print(f"  {item['before']} -> {item['after']}  {item['path']}")
        return 0
    if not args.root and not args.all:
        print("错误: 请指定 --root 目录，或使用 --all 扫描已登记根目录", file=sys.stderr)
        return 2
    roots = args.root if args.root else None
    if args.rebuild:
        result = services.scheduler.rebuild(roots)
        if args.json:
            _json_dump(result.to_dict())
        else:
            print(
                f"重建完成：移除 {result.removed_files} 条旧记录，"
                f"扫描 {result.index.files_scanned} 个文件，新增 {result.index.added}，"
                f"修改 {result.index.modified}，删除 {result.index.deleted}，移动 {result.index.moved}，"
                f"耗时 {result.index.duration_seconds}s"
            )
        return 0
    stats = services.scheduler.scan(roots)
    if args.json:
        _json_dump(stats.to_dict())
    else:
        print(
            f"索引完成：根目录 {stats.roots}，扫描 {stats.files_scanned}，新增 {stats.added}，"
            f"修改 {stats.modified}，删除 {stats.deleted}，移动 {stats.moved}，未变 {stats.unchanged}，"
            f"跳过 {stats.skipped}，chunk {stats.chunks}，耗时 {stats.duration_seconds}s"
        )
        if stats.skipped_reasons:
            print("  跳过原因: " + ", ".join(f"{k}={v}" for k, v in stats.skipped_reasons.items()))
        if stats.errors:
            print("  错误:")
            for error in stats.errors[:10]:
                print(f"    - {error}")
    return 0


def cmd_search(services: Services, args: argparse.Namespace) -> int:
    filters = SearchFilters(
        roots=args.root or None,
        collections=args.collection or None,
        exts=args.ext or None,
        kinds=args.kind or None,
        symbols=args.symbol or None,
        name_contains=args.name_contains,
        path_contains=args.path_contains,
        mtime_from=parse_time(args.mtime_from),
        mtime_to=parse_time(args.mtime_to),
        size_min=args.size_min,
        size_max=args.size_max,
        include_stale=args.include_stale,
        exact_name=not args.no_exact,
    )
    query = " ".join(args.query).strip()
    result = services.engine.search(query, filters, mode=args.mode, limit=args.limit)
    if args.json:
        _json_dump(result.to_dict())
        return 0 if not result.empty else 1
    if result.empty:
        reason_map = {
            "empty_query": "查询为空",
            "empty_index": "索引为空，请先执行 index",
            "no_match": "无匹配结果",
            "all_results_stale": "命中的索引片段均已失效（文件被修改或删除），请重新索引",
        }
        print(f"空结果：{reason_map.get(result.empty_reason or '', result.empty_reason)}")
        if result.warning:
            print(f"提示: {result.warning}")
        return 1
    print(
        f"查询「{query}」模式={result.mode} 命中 {result.total} 条（显示 {len(result.hits)} 条，"
        f"{result.elapsed_ms} ms）"
    )
    if result.warning:
        print(f"提示: {result.warning}")
    for index, hit in enumerate(result.hits, start=1):
        location = ""
        if hit.page is not None:
            location = f" 第{hit.page}页"
        elif hit.start_line is not None:
            location = f" L{hit.start_line}" + (f"-{hit.end_line}" if hit.end_line and hit.end_line != hit.start_line else "")
        tags = [hit.layer, hit.kind]
        if hit.stale:
            tags.append("STALE")
        if hit.parse_status not in ("ok",):
            tags.append(hit.parse_status)
        print(f"\n[{index}] {hit.path}{location}")
        print(f"    ({', '.join(tags)}; score={hit.score}; 索引于 { _fmt_time(hit.indexed_at) })")
        snippet = html.unescape(hit.snippet).replace("<mark>", "【").replace("</mark>", "】")
        for line in snippet.splitlines():
            print(f"    {line}")
        if hit.stale and hit.stale_reason:
            print(f"    ! {hit.stale_reason}")
        if hit.parse_error:
            print(f"    ! 解析说明: {hit.parse_error}")
    if result.semantic:
        print(
            f"\n语义索引: {result.semantic.get('model')} v{result.semantic.get('version')} "
            f"覆盖率 {result.semantic.get('coverage')}（{result.semantic.get('label')}）"
        )
    return 0


def _fmt_time(value: float | None) -> str:
    if not value:
        return "未知"
    return _dt.datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def _resolve_file(db: IndexDatabase, target: str) -> Any:
    if target.isdigit():
        return db.get_file(int(target))
    return db.get_file_by_path(target)


def cmd_show(services: Services, args: argparse.Namespace) -> int:
    row = _resolve_file(services.db, args.target)
    if row is None:
        print(f"未找到文件记录: {args.target}", file=sys.stderr)
        return 2
    chunks = services.db.chunks_for_file(int(row["id"]))
    if not chunks:
        print("该文件没有 chunk（可能尚未索引）")
        return 1
    selected = chunks
    if args.chunk_id is not None:
        selected = [chunk for chunk in chunks if int(chunk["id"]) == args.chunk_id]
        if not selected:
            print(f"未找到 chunk: {args.chunk_id}", file=sys.stderr)
            return 2
    selected = selected[: args.max_chunks]
    locator = EvidenceLocator(services.db)
    payload = {
        "file": SearchEngine._file_row_to_dict(row),
        "chunks": [],
    }
    for chunk in selected:
        evidence = locator.locate(
            path=row["path"],
            name=row["name"],
            kind=chunk["kind"],
            chunk_text=chunk["text"],
            start_line=chunk["start_line"],
            end_line=chunk["end_line"],
            page=chunk["page"],
            file_id=int(row["id"]),
            chunk_id=int(chunk["id"]),
            indexed_at=row["indexed_at"],
            parse_status=row["parse_status"],
            size=int(row["size"]),
            mtime=float(row["mtime"]),
        )
        payload["chunks"].append(
            {
                "chunk_id": int(chunk["id"]),
                "kind": chunk["kind"],
                "start_line": chunk["start_line"],
                "end_line": chunk["end_line"],
                "page": chunk["page"],
                "char_count": chunk["char_count"],
                "symbol": chunk["symbol"],
                "text": chunk["text"],
                "evidence": evidence.to_dict(),
            }
        )
    if args.json:
        _json_dump(payload)
        return 0
    print(f"文件: {row['path']}")
    print(
        f"  大小 {row['size']} 字节 | 修改 {_fmt_time(row['mtime'])} | 索引 {_fmt_time(row['indexed_at'])}"
    )
    print(
        f"  解析状态 {row['parse_status']} | 解析器 {row['parser']} | 截断 {bool(row['truncated'])} | "
        f"哈希 {row['hash_status']}"
    )
    if row["parse_error"]:
        print(f"  解析说明: {row['parse_error']}")
    print(f"  chunk 数 {len(chunks)}")
    for chunk in payload["chunks"]:
        evidence = chunk["evidence"]
        location = f"第{chunk['page']}页" if chunk["page"] else f"L{chunk['start_line']}-{chunk['end_line']}"
        stale = " [STALE: %s]" % evidence["stale_reason"] if evidence["stale"] else ""
        print(f"\n--- chunk #{chunk['chunk_id']} ({chunk['kind']}, {location}){stale} ---")
        for line in chunk["text"].splitlines()[: args.max_lines]:
            print(f"  {line}")
        if evidence["live_lines"] and args.context:
            print("  当前文件上下文:")
            for number, line in evidence["live_lines"]:
                print(f"    {number:>5} | {line}")
    print(f"\n打开定位: file={row['path']} line={selected[0]['start_line']} page={selected[0]['page']}")
    return 0


def cmd_status(services: Services, args: argparse.Namespace) -> int:
    stats = services.db.stats()
    stats["optional_parsers"] = optional_parsers_status()
    stats["semantic"] = services.semantic.stats()
    stats["roots"] = [
        {"id": int(row["id"]), "path": row["path"], "label": row["label"]} for row in services.db.list_roots()
    ]
    if args.json:
        _json_dump(stats)
        return 0
    print(f"{__app_name__} {__version__}")
    print(f"索引库: {stats['database']}（{stats['db_size_bytes']} 字节，schema v{stats['schema_version']}）")
    print(f"根目录 {stats['roots']} 个 | 文件 {stats['files']} | chunk {stats['chunks']} | 符号 {stats['symbols']}")
    for row in stats["roots"]:
        print(f"  - {row['path']}  {row['label'] or ''}")
    if stats["parse_status"]:
        print("解析状态: " + ", ".join(f"{k}={v}" for k, v in stats["parse_status"].items()))
    print("可选解析器: " + ", ".join(f"{k}={'可用' if v else '不可用'}" for k, v in stats["optional_parsers"].items()))
    sem = stats["semantic"]
    print(
        f"语义索引: {sem['model']} v{sem['version']} 覆盖率 {sem['coverage']} "
        f"（{MODEL_LABEL}，非神经网络模型）"
    )
    return 0


def cmd_verify(services: Services, args: argparse.Namespace) -> int:
    report = services.scheduler.verify_integrity()
    if getattr(args, "json", False):
        _json_dump(report)
        return 0 if report.get("ok") else 1
    _headline("完整性检查")
    for detail in report.get("details", []):
        print("  " + detail)
    print(f"  SQLite 完整性: {'通过' if report.get('ok') else '发现问题'}")
    print(f"  磁盘缺失文件: {report.get('files_missing_on_disk')} | 磁盘已变化文件: {report.get('files_changed_on_disk')}")
    return 0 if report.get("ok") else 1


def cmd_related(services: Services, args: argparse.Namespace) -> int:
    row = _resolve_file(services.db, args.target)
    if row is None:
        print(f"未找到文件记录: {args.target}", file=sys.stderr)
        return 2
    results = RelatedFiles(services.db).related(int(row["id"]), limit=args.limit)
    if args.json:
        _json_dump(results)
        return 0
    print(f"与 {row['path']} 相关的文件（{len(results)}）:")
    for item in results:
        print(f"  - {item['path']}  score={item['score']}")
        for reason in item["reasons"]:
            print(f"      依据: {reason}")
    if not results:
        print("  （无）")
    return 0


def cmd_summary(services: Services, args: argparse.Namespace) -> int:
    row = _resolve_file(services.db, args.target)
    if row is None:
        print(f"未找到文件记录: {args.target}", file=sys.stderr)
        return 2
    data = build_extract_summary(services.db, row, max_chars=args.max_chars)
    if args.json:
        _json_dump(data)
        return 0 if data["excerpt"] else 1
    print(f"文件: {data['path']}")
    if not data["excerpt"]:
        print(f"摘要: 不可用（{data['reason']}）")
        return 1
    print("摘要（原文摘录）:")
    for line in data["excerpt"].splitlines():
        print("  " + line)
    print("定位: " + "；".join(format_locator(item) for item in data["locators"]))
    if data["truncated"]:
        print(f"  （已截断到 {args.max_chars} 字符，完整内容用 show 查看）")
    print(f"说明: {data['note']}")
    return 0


def cmd_collections(services: Services, args: argparse.Namespace) -> int:
    action = args.action
    if action == "list":
        items = services.collections.list()
        if args.json:
            _json_dump(items)
            return 0
        if not items:
            print("暂无集合")
            return 0
        for item in items:
            print(f"- {item['name']} [{item['kind']}] 文件 {item['file_count']} 个")
            for root in item["roots"]:
                print(f"    根目录: {root}")
        return 0
    if action == "create":
        item = services.collections.create(args.name, args.kind, args.description or "", args.root or [])
        if args.json:
            _json_dump(item)
        else:
            print(f"已创建集合 {item['name']}（{item['kind']}），文件 {item['file_count']} 个")
        return 0
    if action == "delete":
        removed = services.collections.delete(args.name)
        print("已删除集合" if removed else "集合不存在")
        return 0 if removed else 2
    if action == "add-root":
        services.collections.add_root(args.name, args.path)
        print("已添加根目录到集合")
        return 0
    if action == "add-file":
        added = services.collections.add_file(args.name, args.path)
        print("已添加文件到集合" if added else "集合或文件不存在（文件需先被索引）")
        return 0 if added else 2
    if action == "remove-file":
        services.collections.remove_file(args.name, args.path)
        print("已移除文件")
        return 0
    print("未知操作", file=sys.stderr)
    return 2


def cmd_favorites(services: Services, args: argparse.Namespace) -> int:
    if args.action == "list":
        items = services.favorites.list()
        if args.json:
            _json_dump(items)
            return 0
        if not items:
            print("暂无收藏")
            return 0
        for item in items:
            target = item["path"] or item["query"]
            print(f"#{item['id']} [{item['type']}] {target}  {('备注: ' + item['note']) if item['note'] else ''}")
        return 0
    if args.action == "add-file":
        fav_id = services.favorites.add_file(args.path, args.note or "", args.label)
        print(f"已收藏文件 #{fav_id}" if fav_id else "文件未索引，无法收藏")
        return 0 if fav_id else 2
    if args.action == "add-query":
        fav_id = services.favorites.add_query(args.query, args.note or "", args.label)
        print(f"已收藏查询 #{fav_id}")
        return 0
    if args.action == "remove":
        removed = services.favorites.remove(args.id)
        print("已删除收藏" if removed else "收藏不存在")
        return 0 if removed else 2
    if args.action == "clear":
        services.favorites.clear()
        print("已清空收藏")
        return 0
    return 2


def cmd_recent(services: Services, args: argparse.Namespace) -> int:
    if args.clear:
        services.recent.clear()
        print("已清空最近搜索")
        return 0
    items = services.recent.list(args.limit)
    if args.json:
        _json_dump(items)
        return 0
    if not items:
        print("暂无最近搜索")
        return 0
    for item in items:
        print(f"{_fmt_time(item['created_at'])}  {item['query']}  ({item['result_count']} 条)")
    return 0


def cmd_roots(services: Services, args: argparse.Namespace) -> int:
    if args.action == "list":
        rows = services.db.list_roots()
        for row in rows:
            print(f"#{row['id']} {row['path']}  {row['label'] or ''}")
        if not rows:
            print("暂无根目录")
        return 0
    if args.action == "add":
        root_id = services.scheduler.add_root(args.path, args.label)
        print(f"已登记根目录 #{root_id}: {args.path}")
        return 0
    if args.action == "remove":
        removed = services.db.remove_root(args.path)
        print(f"已移除根目录，删除索引记录 {removed} 条（源文件未删除）")
        return 0
    return 2


def cmd_semantic(services: Services, args: argparse.Namespace) -> int:
    if args.action == "build":
        report = services.semantic.build(limit=args.limit)
        if args.json:
            _json_dump(report)
        else:
            print(
                f"已构建 {report['model']}（{report['label']}，非语义模型）: "
                f"{report['indexed_chunks']}/{report['candidate_chunks']} chunk，"
                f"覆盖率 {report['coverage']}，耗时 {report['duration_seconds']}s"
            )
        return 0
    if args.action == "stats":
        stats = services.semantic.stats()
        if args.json:
            _json_dump(stats)
        else:
            print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 0
    if args.action == "clear":
        removed = services.semantic.clear()
        print(f"已清除 {removed} 条本地哈希向量")
        return 0
    return 2


def cmd_rebuild(services: Services, args: argparse.Namespace) -> int:
    scheduler = IndexScheduler(services.db)
    result = scheduler.rebuild(args.root or None)
    if args.json:
        _json_dump(result.to_dict())
        return 0
    if result.index is not None:
        print(
            f"重建完成：移除 {result.removed_files} 条旧记录，"
            f"扫描 {result.index.files_scanned} 个文件，新增 {result.index.added}，"
            f"chunk {result.index.chunks}，耗时 {result.index.duration_seconds}s"
        )
    else:
        print(f"重建完成：移除 {result.removed_files} 条旧记录（无根目录可扫描）")
    return 0


# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="recallfs",
        description=(
            "RecallFS —— 本地离线开发资料检索：可靠索引、跨文件检索、证据定位。\n"
            "默认数据库位于 ~/.recallfs/recallfs.sqlite3，可用 --db 指定。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--db", default=str(default_db_path()), help="索引数据库路径")
    sub = parser.add_subparsers(dest="command")

    p_index = sub.add_parser("index", help="索引目录（增量）")
    p_index.add_argument("--root", action="append", help="根目录，可多次指定")
    p_index.add_argument("--all", action="store_true", help="扫描所有已登记根目录")
    p_index.add_argument("--rebuild", action="store_true", help="先删除索引再全量重建")
    p_index.add_argument("--retry-failed", action="store_true", help="重试解析失败/不支持/不完整的文件")
    p_index.add_argument("--clear-root", help="清除指定目录的索引记录（不删除源文件）")
    p_index.add_argument("--verify", action="store_true", help="执行完整性检查")
    p_index.add_argument("--json", action="store_true")
    p_index.set_defaults(func=cmd_index)

    p_search = sub.add_parser("search", help="检索文件内容（全文/语义）")
    p_search.add_argument("query", nargs="+", help="查询词；支持 \"短语\" 与 -排除词")
    p_search.add_argument("--root", action="append", help="限定根目录")
    p_search.add_argument("--collection", action="append", help="限定集合")
    p_search.add_argument("--ext", action="append", help="限定扩展名，如 .py 或 py")
    p_search.add_argument("--kind", action="append", help="限定 chunk 类型 metadata/raw_text")
    p_search.add_argument("--symbol", action="append", help="限定代码符号名")
    p_search.add_argument("--name-contains", help="文件名包含")
    p_search.add_argument("--path-contains", help="路径包含")
    p_search.add_argument("--mtime-from", help="修改时间下限（ISO 日期或 epoch）")
    p_search.add_argument("--mtime-to", help="修改时间上限")
    p_search.add_argument("--size-min", type=int, help="文件大小下限（字节）")
    p_search.add_argument("--size-max", type=int, help="文件大小上限（字节）")
    p_search.add_argument("--mode", choices=["fulltext", "semantic"], default="fulltext")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.add_argument("--include-stale", action="store_true", help="包含已失效片段并标注")
    p_search.add_argument("--no-exact", action="store_true", help="禁用精确文件名层")
    p_search.add_argument("--json", action="store_true")
    p_search.set_defaults(func=cmd_search)

    p_show = sub.add_parser("show", help="查看文件记录与片段（证据定位）")
    p_show.add_argument("target", help="文件 id 或绝对路径")
    p_show.add_argument("--chunk-id", type=int)
    p_show.add_argument("--max-chunks", type=int, default=5)
    p_show.add_argument("--max-lines", type=int, default=40)
    p_show.add_argument("--context", action="store_true", help="显示当前文件上下文行")
    p_show.add_argument("--json", action="store_true")
    p_show.set_defaults(func=cmd_show, verify=False)

    p_status = sub.add_parser("status", help="查看索引与解析器状态")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    p_verify = sub.add_parser("verify", help="完整性检查")
    p_verify.add_argument("--json", action="store_true")
    p_verify.set_defaults(func=cmd_verify)

    p_related = sub.add_parser("related", help="相关文件（同哈希/引用/共同关键词）")
    p_related.add_argument("target")
    p_related.add_argument("--limit", type=int, default=10)
    p_related.add_argument("--json", action="store_true")
    p_related.set_defaults(func=cmd_related, verify=False)

    p_summary = sub.add_parser("summary", help="文件摘要（可定位的原文摘录）")
    p_summary.add_argument("target")
    p_summary.add_argument("--max-chars", type=int, default=600)
    p_summary.add_argument("--json", action="store_true")
    p_summary.set_defaults(func=cmd_summary, verify=False)

    p_col = sub.add_parser("collections", help="集合管理")
    col_sub = p_col.add_subparsers(dest="action", required=True)
    col_list = col_sub.add_parser("list")
    col_list.add_argument("--json", action="store_true")
    for name, help_text in (("create", "创建集合"), ("delete", "删除集合")):
        item = col_sub.add_parser(name, help=help_text)
        item.add_argument("name")
        if name == "create":
            item.add_argument("--kind", choices=["project", "course", "custom"], default="custom")
            item.add_argument("--description", default="")
            item.add_argument("--root", action="append")
    add_root = col_sub.add_parser("add-root")
    add_root.add_argument("name")
    add_root.add_argument("path")
    add_file = col_sub.add_parser("add-file")
    add_file.add_argument("name")
    add_file.add_argument("path")
    remove_file = col_sub.add_parser("remove-file")
    remove_file.add_argument("name")
    remove_file.add_argument("path")
    p_col.add_argument("--json", action="store_true")
    p_col.set_defaults(func=cmd_collections, json=False)

    p_fav = sub.add_parser("favorites", help="收藏管理")
    fav_sub = p_fav.add_subparsers(dest="action", required=True)
    fav_list = fav_sub.add_parser("list")
    fav_list.add_argument("--json", action="store_true")
    fav_file = fav_sub.add_parser("add-file")
    fav_file.add_argument("path")
    fav_file.add_argument("--note", default="")
    fav_file.add_argument("--label")
    fav_query = fav_sub.add_parser("add-query")
    fav_query.add_argument("query")
    fav_query.add_argument("--note", default="")
    fav_query.add_argument("--label")
    fav_remove = fav_sub.add_parser("remove")
    fav_remove.add_argument("id", type=int)
    fav_sub.add_parser("clear")
    p_fav.set_defaults(func=cmd_favorites, json=False)

    p_recent = sub.add_parser("recent", help="最近搜索")
    p_recent.add_argument("--limit", type=int, default=20)
    p_recent.add_argument("--clear", action="store_true")
    p_recent.add_argument("--json", action="store_true")
    p_recent.set_defaults(func=cmd_recent)

    p_roots = sub.add_parser("roots", help="根目录管理")
    roots_sub = p_roots.add_subparsers(dest="action", required=True)
    roots_sub.add_parser("list")
    add = roots_sub.add_parser("add")
    add.add_argument("path")
    add.add_argument("--label")
    remove = roots_sub.add_parser("remove")
    remove.add_argument("path")
    p_roots.set_defaults(func=cmd_roots)

    p_sem = sub.add_parser("semantic", help="本地哈希向量索引（非语义模型）")
    sem_sub = p_sem.add_subparsers(dest="action", required=True)
    sem_build = sem_sub.add_parser("build")
    sem_build.add_argument("--limit", type=int)
    sem_build.add_argument("--json", action="store_true")
    sem_stats = sem_sub.add_parser("stats")
    sem_stats.add_argument("--json", action="store_true")
    sem_sub.add_parser("clear")
    p_sem.set_defaults(func=cmd_semantic)

    p_rebuild = sub.add_parser("rebuild", help="删除索引并全量重建")
    p_rebuild.add_argument("--root", action="append")
    p_rebuild.add_argument("--reset", action="store_true", help="索引版本不兼容时重置数据库（会清除集合/收藏）")
    p_rebuild.add_argument("--json", action="store_true")
    p_rebuild.set_defaults(func=cmd_rebuild)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    allow_reset = bool(getattr(args, "reset", False))
    try:
        services = build_services(args.db, allow_version_reset=allow_reset)
    except IndexVersionError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 3
    try:
        return int(args.func(services, args) or 0)
    except BrokenPipeError:  # pragma: no cover
        return 0
    except FileNotFoundError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except NotADirectoryError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    except KeyError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    finally:
        services.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
