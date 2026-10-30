"""快照差异测试：新增、删除、变大、变小、改名、目录移动、同大小内容修改。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from spacepilot.diff_engine import compare_scans
from spacepilot.scanner import ScanOptions, Scanner
from spacepilot.snapshot_store import SnapshotStore


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def build_before(root: Path) -> None:
    write_bytes(root / "keep.txt", b"hello")
    write_bytes(root / "grow.bin", b"g" * 10)
    write_bytes(root / "shrink.bin", b"s" * 20)
    write_bytes(root / "rename_old.txt", b"rename-content")
    write_bytes(root / "modify.txt", b"MODIFIED-CONTENT!")
    write_bytes(root / "del.txt", b"bye")
    write_bytes(root / "dirA" / "f1.txt", b"one")
    write_bytes(root / "dirA" / "f2.txt", b"two")


def build_after(root: Path) -> None:
    write_bytes(root / "new.txt", b"n" * 100)
    (root / "del.txt").unlink()
    write_bytes(root / "grow.bin", b"G" * 50)
    write_bytes(root / "shrink.bin", b"S" * 5)
    os.rename(root / "rename_old.txt", root / "renamed_new.txt")
    write_bytes(root / "modify.txt", b"CHANGED-CONTENT!!")  # 与旧内容长度一致
    os.rename(root / "dirA", root / "dirB")


@pytest.fixture()
def store(tmp_path: Path) -> SnapshotStore:
    db = SnapshotStore(tmp_path / "diff.db")
    yield db
    db.close()


def test_diff_detects_all_change_types(tmp_path: Path, store: SnapshotStore) -> None:
    root = tmp_path / "root"
    root.mkdir()
    build_before(root)
    old = Scanner(root, store, options=ScanOptions(hash_files=True)).scan()

    build_after(root)
    new = Scanner(root, store, options=ScanOptions(hash_files=True)).scan()

    diff = compare_scans(store, old.scan_id, new.scan_id)
    counts = diff.counts
    assert counts["added"] == 1
    assert counts["deleted"] == 1
    assert counts["grown"] == 1
    assert counts["shrunk"] == 1
    assert counts["modified"] == 1
    assert counts["renamed"] == 1
    assert counts["moved"] == 2
    assert diff.unchanged_count >= 1

    added = diff.changes_of("added")
    assert added[0].new_path.endswith("new.txt")
    assert added[0].new_size == 100

    deleted = diff.changes_of("deleted")
    assert deleted[0].old_path.endswith("del.txt")
    assert deleted[0].old_size == 3

    grown = diff.changes_of("grown")
    assert grown[0].old_size == 10 and grown[0].new_size == 50

    shrunk = diff.changes_of("shrunk")
    assert shrunk[0].old_size == 20 and shrunk[0].new_size == 5

    renamed = diff.changes_of("renamed")
    assert len(renamed) == 1
    assert renamed[0].old_path.endswith("rename_old.txt")
    assert renamed[0].new_path.endswith("renamed_new.txt")
    assert "SHA-256" in renamed[0].evidence
    assert renamed[0].old_size == renamed[0].new_size

    modified = diff.changes_of("modified")
    assert len(modified) == 1
    assert modified[0].new_path.endswith("modify.txt")
    assert modified[0].old_size == modified[0].new_size
    assert "SHA-256" in modified[0].evidence

    moved = diff.changes_of("moved")
    assert len(moved) == 2
    assert all("SHA-256" in change.evidence for change in moved)
    moved_new_paths = {Path(change.new_path).as_posix() for change in moved}
    assert any(path.endswith("dirB/f1.txt") for path in moved_new_paths)
    assert any(path.endswith("dirB/f2.txt") for path in moved_new_paths)

    assert len(diff.dir_moves) == 1
    assert diff.dir_moves[0].old_rel == "dirA"
    assert diff.dir_moves[0].new_rel == "dirB"
    assert diff.dir_moves[0].file_count == 2

    assert diff.net_change == new.stats.total_size - old.stats.total_size
    assert diff.dir_growth
    assert diff.growth_sources()
    for note in diff.notes:
        assert "缺少 SHA-256" not in note


def test_diff_without_hashes_does_not_guess_moves(tmp_path: Path, store: SnapshotStore) -> None:
    root = tmp_path / "root"
    root.mkdir()
    write_bytes(root / "original.txt", b"content-1234")
    old = Scanner(root, store, options=ScanOptions(hash_files=False)).scan()

    os.rename(root / "original.txt", root / "renamed.txt")
    new = Scanner(root, store, options=ScanOptions(hash_files=False)).scan()

    diff = compare_scans(store, old.scan_id, new.scan_id)
    # 没有哈希证据时，不能把改名当成“移动”，只能如实报告删除 + 新增。
    assert diff.counts["renamed"] == 0
    assert diff.counts["moved"] == 0
    assert diff.counts["added"] == 1
    assert diff.counts["deleted"] == 1
    assert any("缺少 SHA-256" in note for note in diff.notes)
