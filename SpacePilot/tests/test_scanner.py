"""扫描器测试：统计准确、排除规则、失败记录、取消、0 字节、符号链接。"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

import spacepilot.scanner as scanner_module
from spacepilot.models import EntryStatus
from spacepilot.policy import ExcludeRules
from spacepilot.scanner import ScanOptions, Scanner
from spacepilot.snapshot_store import SnapshotStore


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def build_known_tree(root: Path) -> None:
    write_bytes(root / "top.bin", b"x" * 100)
    write_bytes(root / "a" / "one.txt", b"y" * 50)
    write_bytes(root / "a" / "two.log", b"z" * 30)
    write_bytes(root / "a" / "deep" / "three.dat", b"d" * 200)
    write_bytes(root / "b" / "four.txt", b"f" * 10)
    write_bytes(root / "zero.txt", b"")
    (root / "empty").mkdir()


@pytest.fixture()
def store(tmp_path: Path) -> SnapshotStore:
    db = SnapshotStore(tmp_path / "scan.db")
    yield db
    db.close()


def test_scan_counts_and_aggregates(tmp_path: Path, store: SnapshotStore) -> None:
    root = tmp_path / "root"
    root.mkdir()
    build_known_tree(root)

    result = Scanner(root, store).scan()
    stats = result.stats

    assert stats.files_scanned == 6
    assert stats.total_size == 390
    assert stats.dirs_scanned == 5
    assert stats.zero_byte == 1
    assert stats.failed == 0
    assert stats.cancelled is False
    assert stats.complete is True

    assert store.get_directory_size(result.scan_id, "") == 390
    assert store.get_directory_size(result.scan_id, "a") == 280
    assert store.get_directory_size(result.scan_id, "a/deep") == 200
    assert store.get_directory_size(result.scan_id, "b") == 10
    assert store.get_directory_size(result.scan_id, "empty") == 0

    extensions = {row["suffix"]: row for row in store.extension_stats(result.scan_id)}
    assert extensions[".txt"]["file_count"] == 3
    assert extensions[".txt"]["total_size"] == 60
    assert extensions[".dat"]["total_size"] == 200
    assert extensions[".bin"]["total_size"] == 100
    assert extensions[".log"]["total_size"] == 30

    files = [entry for entry in store.iter_entries(result.scan_id) if entry.kind == "file"]
    assert len(files) == 6
    assert all(entry.status == EntryStatus.SCANNED.value for entry in files)
    assert store.get_entry(result.scan_id, "zero.txt").size == 0


def test_exclude_rules_and_min_size(tmp_path: Path, store: SnapshotStore) -> None:
    root = tmp_path / "root"
    root.mkdir()
    write_bytes(root / "keep.bin", b"k" * 100)
    write_bytes(root / "excluded.tmp", b"t" * 40)
    write_bytes(root / "small.bin", b"s" * 5)
    write_bytes(root / "skipdir" / "inside.bin", b"i" * 50)

    rules = ExcludeRules(
        dir_names=frozenset({"skipdir"}),
        suffixes=frozenset({".tmp"}),
        min_file_size=20,
    )
    result = Scanner(root, store, options=ScanOptions(rules=rules)).scan()

    assert result.stats.files_scanned == 1
    assert result.stats.total_size == 100
    assert result.stats.files_skipped == 2
    assert result.stats.dirs_skipped == 1
    assert result.stats.skipped == 3
    assert result.stats.dirs_scanned == 1

    entries = {entry.rel_path: entry for entry in store.iter_entries(result.scan_id)}
    assert entries["excluded.tmp"].status == EntryStatus.SKIPPED.value
    assert entries["excluded.tmp"].reason.startswith("excluded:suffix")
    assert entries["small.bin"].reason.startswith("excluded:min_size")
    assert entries["skipdir"].kind == "dir"
    assert entries["skipdir"].reason == "excluded:dir_name"


def test_unreadable_file_is_failed_not_missing(tmp_path: Path, store: SnapshotStore, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    root.mkdir()
    write_bytes(root / "ok.txt", b"ok")
    blocked = root / "blocked.bin"
    write_bytes(blocked, b"blocked")

    real_stat = os.stat
    blocked_norm = os.path.normcase(os.path.abspath(str(blocked)))

    def fake_stat(path, *args, **kwargs):
        if os.path.normcase(os.path.abspath(str(path))) == blocked_norm:
            raise PermissionError(13, "Permission denied", str(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(scanner_module.os, "stat", fake_stat)

    result = Scanner(root, store).scan()
    assert result.stats.files_scanned == 1
    assert result.stats.total_size == 2
    assert result.stats.failed == 1
    assert result.stats.unknown_size == 1
    assert result.stats.complete is False

    failed = [entry for entry in store.iter_entries(result.scan_id, status="failed")]
    assert len(failed) == 1
    assert failed[0].rel_path == "blocked.bin"
    assert failed[0].kind == "file"
    assert failed[0].size is None
    assert "PermissionError" in failed[0].reason


def test_unreadable_directory_is_recorded(tmp_path: Path, store: SnapshotStore, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    root.mkdir()
    write_bytes(root / "fine.txt", b"fine")
    blocked_dir = root / "blocked_dir"
    blocked_dir.mkdir()
    write_bytes(blocked_dir / "inside.bin", b"inside")

    real_scandir = os.scandir
    blocked_norm = os.path.normcase(os.path.abspath(str(blocked_dir)))

    def fake_scandir(path):
        if os.path.normcase(os.path.abspath(str(path))) == blocked_norm:
            raise PermissionError(13, "Permission denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(scanner_module.os, "scandir", fake_scandir)

    result = Scanner(root, store).scan()
    assert result.stats.files_scanned == 1
    assert result.stats.failed == 1
    failed = [entry for entry in store.iter_entries(result.scan_id, status="failed")]
    assert len(failed) == 1
    assert failed[0].rel_path == "blocked_dir"
    assert failed[0].kind == "dir"
    assert failed[0].size is None


def test_cancel_saves_partial_results(tmp_path: Path, store: SnapshotStore) -> None:
    root = tmp_path / "root"
    root.mkdir()
    for index in range(80):
        write_bytes(root / f"file_{index:03d}.bin", b"c" * 10)

    cancel_event = threading.Event()
    progress_events: list[dict] = []

    def on_progress(payload: dict) -> None:
        progress_events.append(payload)
        if payload.get("files_scanned", 0) >= 5:
            cancel_event.set()

    result = Scanner(
        root,
        store,
        options=ScanOptions(progress_every=1),
        cancel_event=cancel_event,
        progress_callback=on_progress,
    ).scan()

    assert result.stats.cancelled is True
    assert result.stats.complete is False
    assert 5 <= result.stats.files_scanned < 80
    saved = store.get_scan(result.scan_id)
    assert saved is not None
    assert saved.stats.cancelled is True
    assert store.count_entries(result.scan_id, status="scanned", kind="file") >= 5
    assert progress_events
    assert progress_events[-1]["processed"] >= result.stats.files_scanned


def test_symbolic_link_skipped_by_default(tmp_path: Path, store: SnapshotStore) -> None:
    root = tmp_path / "root"
    root.mkdir()
    write_bytes(root / "normal.txt", b"n")
    outside = tmp_path / "outside"
    outside.mkdir()
    write_bytes(outside / "secret.bin", b"secret")
    try:
        os.symlink(str(outside), str(root / "link_dir"), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前 Windows 环境不允许创建符号链接")

    result = Scanner(root, store).scan()
    assert result.stats.files_scanned == 1
    assert result.stats.symlinks_skipped == 1
    assert result.stats.total_size == 1
    link_entry = store.get_entry(result.scan_id, "link_dir")
    assert link_entry is not None
    assert link_entry.kind == "symlink"
    assert link_entry.status == EntryStatus.SKIPPED.value


def test_volatile_file_detected(tmp_path: Path, store: SnapshotStore, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    root.mkdir()
    changing = root / "changing.bin"
    write_bytes(changing, b"1234567890")

    real_stat = os.stat
    target_norm = os.path.normcase(os.path.abspath(str(changing)))
    calls = {"count": 0}

    def fake_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if os.path.normcase(os.path.abspath(str(path))) == target_norm:
            calls["count"] += 1
            if calls["count"] % 2 == 0:
                values = list(result)
                values[8] = values[8] + 1  # mtime 秒数变化，模拟扫描期间被修改
                return os.stat_result(tuple(values))
        return result

    monkeypatch.setattr(scanner_module.os, "stat", fake_stat)
    result = Scanner(root, store).scan()
    assert result.stats.files_scanned == 1
    assert result.stats.volatile == 1
    entry = store.get_entry(result.scan_id, "changing.bin")
    assert entry is not None
    assert entry.volatile is True
