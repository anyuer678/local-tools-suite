from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _run_benchmark(args: list[str], env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    project_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(project_root / "src")
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(project_root / "tools" / "benchmark.py"), *args],
        cwd=str(project_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_benchmark_small_dataset_outputs_real_metrics(tmp_path):
    db_path = tmp_path / "bench.sqlite3"
    completed = _run_benchmark(
        ["--files", "30", "--lines", "10", "--repeats", "1", "--db", str(db_path), "--json"]
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["dataset"]["files"] == 30
    assert payload["dataset"]["bytes"] > 0
    assert payload["index"]["seconds"] > 0
    assert payload["index"]["files_scanned"] == 30
    assert payload["index"]["chunks"] > 0
    assert payload["database"]["size_bytes"] > 0
    assert Path(payload["database"]["path"]).exists()
    assert payload["database"]["files"] == 30
    assert payload["memory"]["rss_after_index_mb"] > 0
    assert payload["queries"]["count"] > 0
    assert payload["queries"]["p50_ms"] >= 0
    assert payload["queries"]["mean_hits"] > 0
    assert payload["environment"]["sqlite"]


def test_benchmark_human_output(tmp_path):
    completed = _run_benchmark(
        ["--files", "8", "--lines", "5", "--repeats", "1", "--db", str(tmp_path / "small.sqlite3")]
    )
    assert completed.returncode == 0, completed.stderr
    assert "RecallFS benchmark" in completed.stdout
    assert "数据集: 8 个文件" in completed.stdout
    assert "数据库:" in completed.stdout
    assert "查询" in completed.stdout
