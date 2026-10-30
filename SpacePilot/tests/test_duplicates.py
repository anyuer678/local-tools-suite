"""重复文件检测测试：同内容、同名不同内容、硬链接、最小阈值。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from spacepilot.duplicates import find_duplicates_from_store
from spacepilot.scanner import Scanner
from spacepilot.snapshot_store import SnapshotStore


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


@pytest.fixture()
def store(tmp_path: Path) -> SnapshotStore:
    db = SnapshotStore(tmp_path / "dups.db")
    yield db
    db.close()


def test_duplicates_same_content_and_hardlinks(tmp_path: Path, store: SnapshotStore) -> None:
    root = tmp_path / "root"
    root.mkdir()
    content = b"same-content-123"
    write_bytes(root / "a.txt", content)
    write_bytes(root / "sub" / "b.txt", content)
    write_bytes(root / "same_name" / "one" / "dup.dat", b"ABCDEFGHIJ")
    write_bytes(root / "same_name" / "two" / "dup.dat", b"1234567890")
    write_bytes(root / "unique.bin", b"unique" * 10)

    hard_source = root / "hard_source.bin"
    hard_link = root / "hard_link.bin"
    write_bytes(hard_source, b"hardlink-data")
    try:
        os.link(hard_source, hard_link)
        hardlinks_supported = True
    except (OSError, NotImplementedError):
        hardlinks_supported = False

    scan = Scanner(root, store).scan()
    report = find_duplicates_from_store(store, scan.scan_id, min_size=1)

    assert report.scanned_files == 7
    assert report.threshold == 1

    same_content_groups = [group for group in report.groups if group.sha256 and group.size == len(content)]
    assert len(same_content_groups) == 1
    group = same_content_groups[0]
    assert {file.rel_path for file in group.files} == {"a.txt", "sub/b.txt"}
    assert group.reclaimable == len(content)

    # 同名但内容不同 -> 不是重复
    all_paths = {file.rel_path for group in report.groups for file in group.files}
    assert "same_name/one/dup.dat" not in all_paths
    assert "same_name/two/dup.dat" not in all_paths

    if hardlinks_supported:
        hardlink_groups = [group for group in report.groups if group.size == len(b"hardlink-data")]
        assert len(hardlink_groups) == 1
        hardlink_group = hardlink_groups[0]
        assert len(hardlink_group.files) == 2
        assert hardlink_group.hardlink_skipped == 1
        assert hardlink_group.reclaimable == 0
        assert any(file.hardlink_of for file in hardlink_group.files)
        assert report.total_reclaimable == len(content)
    else:
        pytest.skip("当前平台不支持 os.link 硬链接")

    # 检测过程会把哈希回填到快照，便于后续对比。
    stored = store.stored_hash(scan.scan_id, "a.txt")
    assert stored == group.sha256


def test_min_size_excludes_small_files(tmp_path: Path, store: SnapshotStore) -> None:
    root = tmp_path / "root"
    root.mkdir()
    write_bytes(root / "one.bin", b"tiny")
    write_bytes(root / "two.bin", b"tiny")
    scan = Scanner(root, store).scan()
    report = find_duplicates_from_store(store, scan.scan_id, min_size=10)
    assert report.groups == []
    assert report.total_reclaimable == 0
    assert report.candidate_files == 0


def test_same_name_different_content_not_duplicate(tmp_path: Path, store: SnapshotStore) -> None:
    root = tmp_path / "root"
    root.mkdir()
    write_bytes(root / "left" / "same.txt", b"AAAAA")
    write_bytes(root / "right" / "same.txt", b"BBBBB")
    scan = Scanner(root, store).scan()
    report = find_duplicates_from_store(store, scan.scan_id, min_size=1)
    assert report.groups == []
    assert report.total_reclaimable == 0
