from __future__ import annotations

import os

import pytest

from helpers import write_file

from recallfs.filesystem_watcher import ScanRules
from recallfs.index_scheduler import IndexScheduler
from recallfs.project_collections import CollectionStore
from recallfs.search_engine import SearchFilters


def test_exclusions_hidden_and_suffix(corpus, scheduler, engine):
    write_file(corpus, "visible.txt", "可见内容 marker\n")
    write_file(corpus, ".git/config", "git 内部内容 marker\n")
    write_file(corpus, "node_modules/pkg.js", "依赖内容 marker\n")
    write_file(corpus, "cache.tmp", "临时内容 marker\n")
    write_file(corpus, ".secret.txt", "隐藏内容 marker\n")
    stats = scheduler.scan([str(corpus)])
    assert stats.files_scanned == 1
    assert stats.skipped_reasons.get("excluded_dir", 0) >= 2
    assert stats.skipped_reasons.get("excluded_suffix", 0) >= 1
    assert stats.skipped_reasons.get("hidden", 0) >= 1
    result = engine.search("marker")
    assert {hit.name for hit in result.hits} == {"visible.txt"}
    assert engine.search("隐藏内容").empty


def test_include_hidden_option(tmp_path, db):
    root = tmp_path / "hidden_root"
    root.mkdir()
    write_file(root, ".hidden.md", "隐藏但仍可索引 marker\n")
    scheduler = IndexScheduler(db, rules=ScanRules(include_hidden=True))
    stats = scheduler.scan([str(root)])
    assert stats.files_scanned == 1
    assert stats.added == 1


def test_file_size_limit(tmp_path, db):
    root = tmp_path / "size_root"
    root.mkdir()
    write_file(root, "small.txt", "小文件 marker\n")
    write_file(root, "big.txt", "大文件内容 " * 200)
    scheduler = IndexScheduler(db, rules=ScanRules(max_file_size=200))
    stats = scheduler.scan([str(root)])
    assert stats.files_scanned == 1
    assert stats.skipped_reasons.get("too_large") == 1
    assert db.file_count() == 1
    assert db.get_file_by_path(root / "big.txt") is None


def test_symlink_does_not_escape_root(tmp_path, db):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    write_file(outside, "secret.txt", "越界机密内容 不应该被索引\n")
    write_file(root, "inside.txt", "正常内容\n")
    link = root / "escape"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"当前环境无法创建符号链接: {exc}")

    scheduler = IndexScheduler(db)
    stats = scheduler.scan([str(root)])
    assert stats.files_scanned == 1
    assert stats.skipped_reasons.get("symlink", 0) >= 1
    assert db.get_file_by_path(link / "secret.txt") is None

    follow_db = scheduler.db
    follow = IndexScheduler(follow_db, rules=ScanRules(follow_symlinks=True))
    stats2 = follow.scan([str(root)])
    assert stats2.files_scanned == 1
    assert stats2.skipped_reasons.get("symlink_escape", 0) >= 1


def test_symlink_inside_root_can_be_followed(tmp_path, db):
    root = tmp_path / "root2"
    (root / "real").mkdir(parents=True)
    write_file(root, "real/data.txt", "根内链接内容 marker\n")
    link = root / "alias"
    try:
        os.symlink(root / "real", link, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"当前环境无法创建符号链接: {exc}")
    scheduler = IndexScheduler(db, rules=ScanRules(follow_symlinks=True))
    stats = scheduler.scan([str(root)])
    assert stats.files_scanned == 2
    paths = {row["path"] for row in db.all_files()}
    assert str(link / "data.txt") in paths


def test_multiple_roots_and_collection_filter(tmp_path, db, engine):
    root_a = tmp_path / "root_a"
    root_b = tmp_path / "root_b"
    root_a.mkdir()
    root_b.mkdir()
    file_a = write_file(root_a, "a.md", "跨根目录关键词 shared\n")
    file_b = write_file(root_b, "b.md", "跨根目录关键词 shared\n")
    write_file(root_b, "extra.md", "另一段 shared 内容\n")
    scheduler = IndexScheduler(db)
    stats = scheduler.scan([str(root_a), str(root_b)])
    assert stats.roots == 2
    assert stats.added == 3

    limited = engine.search("shared", SearchFilters(roots=[str(root_a)]))
    assert {hit.path for hit in limited.hits} == {str(file_a)}

    collections = CollectionStore(db)
    collections.create("项目A", "project", roots=[str(root_a)])
    in_collection = engine.search("shared", SearchFilters(collections=["项目A"]))
    assert {hit.path for hit in in_collection.hits} == {str(file_a)}

    collections.add_file("项目A", str(file_b))
    expanded = engine.search("shared", SearchFilters(collections=["项目A"]))
    assert {hit.path for hit in expanded.hits} == {str(file_a), str(file_b)}
    assert collections.describe("项目A")["file_count"] == 2

    unknown = engine.search("shared", SearchFilters(collections=["不存在集合"]))
    assert unknown.empty


def test_index_database_not_indexed(corpus, scheduler, db):
    write_file(corpus, "only.txt", "文本内容\n")
    scheduler.scan([str(corpus)])
    names = {row["name"] for row in db.all_files()}
    assert names == {"only.txt"}
