"""RecallFS 性能测量脚本（真实索引 + 查询，输出实测耗时/DB 大小/内存）。

用法示例：
    python tools/benchmark.py --files 200
    python tools/benchmark.py --files 500 --semantic --json
    python tools/benchmark.py --files 50 --keep --db ./bench.sqlite3

不访问网络；psutil 可用时内存更精确，否则 Windows 使用 psapi、其他平台使用 resource。
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import random
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from recallfs.document_parser import optional_parsers_status  # noqa: E402
from recallfs.fulltext_index import IndexDatabase  # noqa: E402
from recallfs.index_scheduler import IndexScheduler  # noqa: E402
from recallfs.search_engine import SearchEngine  # noqa: E402
from recallfs.semantic_index import SemanticIndex  # noqa: E402

CHINESE_TERMS = ["检索", "索引", "证据", "定位", "离线", "文档", "知识", "文件", "解析", "查询"]
ENGLISH_TERMS = ["benchmark", "evidence", "parser", "index", "search", "chunk", "symbol", "offline"]


# ---------------------------------------------------------------------------
# 内存测量（无第三方依赖优先）
# ---------------------------------------------------------------------------
class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def rss_mb() -> tuple[float, str]:
    try:
        import psutil  # type: ignore

        return psutil.Process().memory_info().rss / (1024 * 1024), "psutil"
    except Exception:
        pass
    if os.name == "nt":
        try:
            counters = _ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(_ProcessMemoryCounters)
            ctypes.windll.psapi.GetProcessMemoryInfo(
                ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
            )
            return counters.WorkingSetSize / (1024 * 1024), "windows-psapi"
        except Exception:
            pass
    try:
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        divisor = 1024 if sys.platform != "darwin" else 1024 * 1024
        return usage / divisor, "resource(ru_maxrss)"
    except Exception:
        return 0.0, "unavailable"


# ---------------------------------------------------------------------------
def generate_dataset(root: Path, files: int, lines_per_file: int, seed: int = 7) -> dict[str, float]:
    started = time.perf_counter()
    rng = random.Random(seed)
    total_bytes = 0
    total_lines = 0
    for index in range(files):
        ext = ".py" if index % 4 == 0 else (".md" if index % 3 == 0 else ".txt")
        folder = root / f"group_{index % 10:02d}"
        folder.mkdir(parents=True, exist_ok=True)
        lines = [f"# 文档 {index} — RecallFS 基准测试数据", ""]
        if ext == ".py":
            lines.append(f"def benchmark_symbol_{index}(value):")
            lines.append("    return value + 1")
            lines.append("")
        for line_no in range(lines_per_file):
            chinese = "，".join(rng.choice(CHINESE_TERMS) for _ in range(6))
            english = " ".join(rng.choice(ENGLISH_TERMS) for _ in range(5))
            lines.append(f"第 {line_no + 1} 行：{chinese}。{english} line={line_no} id={index}")
        content = "\n".join(lines) + "\n"
        path = folder / f"doc_{index:04d}{ext}"
        path.write_text(content, encoding="utf-8")
        total_bytes += len(content.encode("utf-8"))
        total_lines += len(lines)
    return {
        "generation_seconds": round(time.perf_counter() - started, 3),
        "bytes": total_bytes,
        "lines": total_lines,
    }


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = min(len(ordered) - 1, max(0, int(round(ratio * (len(ordered) - 1)))))
    return ordered[position]


def run(files: int, lines_per_file: int, repeats: int, semantic: bool, keep: bool, db_path: str | None) -> dict:
    temp_dir = Path(tempfile.mkdtemp(prefix="recallfs-bench-"))
    corpus = temp_dir / "corpus"
    corpus.mkdir()
    database_path = Path(db_path) if db_path else temp_dir / "bench.sqlite3"
    if database_path.exists():
        database_path.unlink()
    dataset = generate_dataset(corpus, files, lines_per_file)

    rss_before, method = rss_mb()
    db = IndexDatabase(database_path)
    scheduler = IndexScheduler(db)
    index_started = time.perf_counter()
    stats = scheduler.scan([str(corpus)])
    index_seconds = time.perf_counter() - index_started
    rss_after_index, _method = rss_mb()

    db_size = db.db_size_bytes()
    engine = SearchEngine(db)
    queries = ["检索 索引", "benchmark", "证据 定位", '"离线检索"', "不存在的词汇xyzzy", "def benchmark"]
    latencies: list[float] = []
    hit_counts: list[int] = []
    for query in queries:
        for _ in range(max(1, repeats)):
            started = time.perf_counter()
            result = engine.search(query, limit=20, record_recent=False)
            latencies.append((time.perf_counter() - started) * 1000)
            hit_counts.append(len(result.hits))

    semantic_report = None
    if semantic:
        sem = SemanticIndex(db)
        build_started = time.perf_counter()
        semantic_report = sem.build()
        semantic_report["build_seconds"] = round(time.perf_counter() - build_started, 3)
        sem_latencies = []
        for _ in range(max(1, repeats)):
            started = time.perf_counter()
            sem.search("离线检索 证据", limit=20)
            sem_latencies.append((time.perf_counter() - started) * 1000)
        semantic_report["query_p50_ms"] = round(statistics.median(sem_latencies), 3)
        semantic_report["query_max_ms"] = round(max(sem_latencies), 3)

    rss_after_all, _method = rss_mb()
    payload = {
        "dataset": {
            "files": files,
            "bytes": dataset["bytes"],
            "lines": dataset["lines"],
            "generation_seconds": dataset["generation_seconds"],
            "directory": str(corpus),
        },
        "index": {
            "seconds": round(index_seconds, 3),
            "files_per_second": round(stats.files_scanned / index_seconds, 2) if index_seconds else 0,
            "files_scanned": stats.files_scanned,
            "chunks": stats.chunks,
            "added": stats.added,
            "skipped": stats.skipped,
            "parse_errors": stats.parse_errors,
        },
        "database": {
            "path": str(database_path),
            "size_bytes": db_size,
            "size_mb": round(db_size / (1024 * 1024), 3),
            "files": db.file_count(),
            "chunks": int(db.scalar("SELECT COUNT(*) FROM chunks") or 0),
        },
        "memory": {
            "method": method,
            "rss_before_mb": round(rss_before, 2),
            "rss_after_index_mb": round(rss_after_index, 2),
            "rss_after_all_mb": round(rss_after_all, 2),
            "delta_index_mb": round(rss_after_index - rss_before, 2),
        },
        "queries": {
            "count": len(latencies),
            "queries": queries,
            "p50_ms": round(percentile(latencies, 0.5), 3),
            "p95_ms": round(percentile(latencies, 0.95), 3),
            "max_ms": round(percentile(latencies, 1.0), 3),
            "mean_hits": round(sum(hit_counts) / len(hit_counts), 2) if hit_counts else 0,
        },
        "semantic": semantic_report,
        "environment": {
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "sqlite": sqlite3.sqlite_version,
            "optional_parsers": optional_parsers_status(),
        },
    }
    db.close()
    if not keep:
        shutil.rmtree(temp_dir, ignore_errors=True)
        payload["dataset"]["directory"] = "(已删除临时目录，使用 --keep 保留)"
        if not db_path:
            payload["database"]["path"] = "(已删除临时数据库，使用 --keep/--db 保留)"
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RecallFS 性能测量（真实索引与查询）")
    parser.add_argument("--files", type=int, default=200, help="生成的文件数（默认 200）")
    parser.add_argument("--lines", type=int, default=30, help="每个文件的行数（默认 30）")
    parser.add_argument("--repeats", type=int, default=5, help="每个查询重复次数")
    parser.add_argument("--semantic", action="store_true", help="同时测量本地哈希向量构建/查询")
    parser.add_argument("--keep", action="store_true", help="保留临时数据集与数据库")
    parser.add_argument("--db", default=None, help="指定数据库路径")
    parser.add_argument("--json", action="store_true", help="仅输出 JSON")
    args = parser.parse_args(argv)

    if args.files < 1:
        print("--files 必须 >= 1", file=sys.stderr)
        return 2
    payload = run(args.files, args.lines, args.repeats, args.semantic, args.keep, args.db)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print("RecallFS benchmark")
    print(f"  数据集: {payload['dataset']['files']} 个文件, {payload['dataset']['bytes']} 字节, "
          f"{payload['dataset']['lines']} 行（生成 {payload['dataset']['generation_seconds']}s）")
    index = payload["index"]
    print(f"  索引: {index['seconds']}s（{index['files_per_second']} 文件/秒），"
          f"chunk {index['chunks']}，解析错误 {index['parse_errors']}，跳过 {index['skipped']}")
    database = payload["database"]
    print(f"  数据库: {database['size_mb']} MB（{database['size_bytes']} 字节）")
    memory = payload["memory"]
    print(f"  内存({memory['method']}): 索引前 {memory['rss_before_mb']} MB → "
          f"索引后 {memory['rss_after_index_mb']} MB（Δ {memory['delta_index_mb']} MB）")
    queries = payload["queries"]
    print(f"  查询 {queries['count']} 次: p50 {queries['p50_ms']} ms, p95 {queries['p95_ms']} ms, "
          f"max {queries['max_ms']} ms, 平均命中 {queries['mean_hits']}")
    if payload["semantic"]:
        sem = payload["semantic"]
        print(f"  本地哈希向量: {sem['indexed_chunks']} chunk, 覆盖率 {sem['coverage']}, "
              f"构建 {sem['build_seconds']}s, 查询 p50 {sem['query_p50_ms']} ms（{sem['label']}）")
    print(f"  SQLite {payload['environment']['sqlite']} / Python {payload['environment']['python']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
