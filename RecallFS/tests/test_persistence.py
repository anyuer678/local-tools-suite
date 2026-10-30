from __future__ import annotations

import os
import time

import pytest

from helpers import write_file

from recallfs.fulltext_index import IndexDatabase, IndexVersionError
from recallfs.index_scheduler import IndexScheduler
from recallfs.project_collections import CollectionStore, FavoritesStore, RecentSearchStore
from recallfs.search_engine import SearchEngine


def test_favorites_collections_recent_persist(tmp_path, corpus):
    db_path = tmp_path / "persist.sqlite3"
    file_path = write_file(corpus, "收藏目标.md", "收藏与集合的持久化内容\n")

    db = IndexDatabase(db_path)
    IndexScheduler(db).scan([str(corpus)])
    collections = CollectionStore(db)
    collections.create("课程A", "course", "测试课程", roots=[str(corpus)])
    favorites = FavoritesStore(db)
    favorites.add_file(str(file_path), note="重要笔记")
    favorites.add_query("持久化", note="常用查询")
    RecentSearchStore(db).record("持久化", {"exts": [".md"]}, 1)
    file_id = int(db.get_file_by_path(file_path)["id"])
    db.close()

    reopened = IndexDatabase(db_path)
    try:
        assert reopened.file_count() == 1
        assert reopened.get_file(file_id) is not None
        assert {item["name"] for item in CollectionStore(reopened).list()} == {"课程A"}
        collection = CollectionStore(reopened).describe("课程A")
        assert collection["kind"] == "course"
        assert collection["file_count"] == 1
        favs = FavoritesStore(reopened).list()
        assert {fav["type"] for fav in favs} == {"file", "query"}
        assert any(fav["note"] == "重要笔记" for fav in favs)
        recent = RecentSearchStore(reopened).list()
        assert recent and recent[0]["query"] == "持久化"
        assert recent[0]["filters"] == {"exts": [".md"]}

        engine = SearchEngine(reopened)
        assert engine.search("持久化内容").hits
    finally:
        reopened.close()


def test_recent_search_dedup_and_limit(tmp_path, corpus):
    db = IndexDatabase(tmp_path / "recent.sqlite3")
    try:
        write_file(corpus, "a.txt", "最近搜索关键词\n")
        IndexScheduler(db).scan([str(corpus)])
        recent = RecentSearchStore(db, keep=3)
        engine = SearchEngine(db, recent=recent)
        for query in ("查询一", "查询二", "查询三", "查询四"):
            engine.search(query)
        engine.search("查询四")
        items = recent.list()
        assert len(items) == 3
        assert items[0]["query"] == "查询四"
        queries = [item["query"] for item in items]
        assert queries.count("查询四") == 1
        recent.clear()
        assert recent.list() == []
    finally:
        db.close()


def test_integrity_and_stale_listing(tmp_path, corpus):
    db = IndexDatabase(tmp_path / "integrity.sqlite3")
    try:
        path = write_file(corpus, "check.txt", "完整性内容\n")
        IndexScheduler(db).scan([str(corpus)])
        report = db.integrity_check()
        assert report.ok
        assert db.list_stale_files() == []
        time.sleep(0.02)
        path.write_text("修改后的内容更长\n", encoding="utf-8")
        stale = db.list_stale_files()
        assert [row["path"] for row in stale] == [str(path)]
        # 完整性检查不修改任何内容，也不删除源文件
        assert path.exists()
    finally:
        db.close()


def test_delete_collection_does_not_delete_files(tmp_path, corpus):
    db = IndexDatabase(tmp_path / "collections.sqlite3")
    try:
        file_path = write_file(corpus, "note.md", "集合删除测试\n")
        IndexScheduler(db).scan([str(corpus)])
        collections = CollectionStore(db)
        collections.create("临时集合", roots=[str(corpus)])
        assert collections.delete("临时集合") is True
        assert collections.list() == []
        assert db.get_file_by_path(file_path) is not None
        assert file_path.exists()
    finally:
        db.close()


def test_rebuild_and_clear_keep_source_files(tmp_path, corpus):
    db = IndexDatabase(tmp_path / "maintenance.sqlite3")
    try:
        write_file(corpus, "keep.txt", "维护操作内容\n")
        scheduler = IndexScheduler(db)
        scheduler.scan([str(corpus)])
        rebuild = scheduler.rebuild([str(corpus)])
        assert rebuild.removed_files == 1
        assert rebuild.index is not None and rebuild.index.added == 1
        assert SearchEngine(db).search("维护操作").hits

        removed = scheduler.clear_root(str(corpus))
        assert removed == 1
        assert db.file_count() == 0
        assert (corpus / "keep.txt").exists()

        scheduler.scan([str(corpus)])
        assert db.file_count() == 1
        db.drop_all_index_data()
        assert db.file_count() == 0
        assert (corpus / "keep.txt").read_text(encoding="utf-8") == "维护操作内容\n"
    finally:
        db.close()


def test_remove_root_does_not_delete_source(tmp_path, corpus):
    db = IndexDatabase(tmp_path / "roots.sqlite3")
    try:
        write_file(corpus, "x.txt", "根目录移除\n")
        scheduler = IndexScheduler(db)
        scheduler.add_root(corpus)
        scheduler.scan(None)
        assert db.file_count() == 1
        removed = db.remove_root(corpus)
        assert removed == 1
        assert db.file_count() == 0
        assert db.list_roots() == []
        assert (corpus / "x.txt").exists()
    finally:
        db.close()


def test_index_version_mismatch_requires_reset(tmp_path):
    path = tmp_path / "version.sqlite3"
    db = IndexDatabase(path)
    db.set_meta("schema_version", "999")
    db.close()
    with pytest.raises(IndexVersionError):
        IndexDatabase(path)
    reset = IndexDatabase(path, allow_version_reset=True)
    try:
        assert reset.file_count() == 0
        assert reset.schema_version != 999
        reset.integrity_check()
    finally:
        reset.close()


def test_favorite_missing_file_returns_none(tmp_path, corpus):
    db = IndexDatabase(tmp_path / "fav.sqlite3")
    try:
        favorites = FavoritesStore(db)
        assert favorites.add_file(str(corpus / "not_indexed.txt")) is None
        assert favorites.list() == []
    finally:
        db.close()
