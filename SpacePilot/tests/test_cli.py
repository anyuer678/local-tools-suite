"""CLI 测试：扫描、快照、对比、重复、建议、导出，以及 python -m spacepilot --help。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from spacepilot.cli import main
from spacepilot.snapshot_store import SnapshotStore


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_cli_end_to_end(tmp_path: Path, capsys) -> None:
    db = tmp_path / "cli.db"
    root = tmp_path / "root"
    root.mkdir()
    write_bytes(root / "keep.txt", b"keep")
    write_bytes(root / "left.bin", b"payload" * 10)
    write_bytes(root / "right.bin", b"payload" * 10)
    write_bytes(root / "notes.log", b"note" * 100)
    json_out = tmp_path / "scan.json"
    csv_out = tmp_path / "scan.csv"

    assert main(["--db", str(db), "scan", str(root), "--top", "5", "--json-out", str(json_out), "--csv-out", str(csv_out)]) == 0
    out = capsys.readouterr().out
    assert "扫描完成" in out
    assert "完整度" in out
    assert json_out.exists() and csv_out.exists()

    store = SnapshotStore(db)
    first = store.latest_scan()
    assert first is not None
    store.close()

    write_bytes(root / "keep.txt", b"keep-MORE")
    write_bytes(root / "new.txt", b"new-file")
    assert main(["--db", str(db), "scan", str(root), "--top", "5"]) == 0
    capsys.readouterr()

    store = SnapshotStore(db)
    second = store.latest_scan()
    assert second is not None
    assert second.scan_id != first.scan_id
    store.close()

    assert main(["--db", str(db), "snapshots", "--json"]) == 0
    assert second.scan_id in capsys.readouterr().out

    assert main(["--db", str(db), "compare", first.scan_id[:10], second.scan_id[:10], "--top", "10"]) == 0
    out = capsys.readouterr().out
    assert "变化统计" in out
    assert "新增" in out

    assert main(["--db", str(db), "duplicates", second.scan_id[:10], "--min-size", "1"]) == 0
    out = capsys.readouterr().out
    assert "潜在可回收空间" in out
    assert "left.bin" in out

    assert main(["--db", str(db), "suggest", second.scan_id[:10], "--min-size", "1"]) == 0
    out = capsys.readouterr().out
    assert "清理建议" in out
    assert "证据" in out

    assert main(
        [
            "--db", str(db), "files", second.scan_id[:10],
            "--min-size", "1", "--suffix", ".bin", "--sort", "size",
            "--limit", "1", "--copy",
        ]
    ) == 0
    out = capsys.readouterr().out
    paths = [line for line in out.splitlines() if line.strip()]
    assert len(paths) == 1
    assert paths[0].endswith("left.bin") or paths[0].endswith("right.bin")
    assert str(root) in paths[0]


def test_module_help_runs_without_gui() -> None:
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    src = str(project_root / "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, "-m", "spacepilot", "--help"],
        cwd=str(project_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0
    output = completed.stdout + completed.stderr
    assert "SpacePilot" in output
    assert "scan" in output
    assert "duplicates" in output
