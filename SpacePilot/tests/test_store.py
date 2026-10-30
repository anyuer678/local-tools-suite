"""数据库测试：schema 迁移、重复打开一致性、索引、导出。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from spacepilot.scanner import Scanner
from spacepilot.snapshot_store import SCHEMA_V1_SQL, SCHEMA_VERSION, SnapshotStore


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_fresh_schema_and_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.db"
    root = tmp_path / "root"
    root.mkdir()
    write_bytes(root / "a.txt", b"hello")
    write_bytes(root / "sub" / "b.bin", b"0123456789")

    store = SnapshotStore(db_path)
    assert store.schema_version() == SCHEMA_VERSION
    assert store.integrity_ok()
    result = Scanner(root, store).scan()
    scan_id = result.scan_id
    store.close()

    reopened = SnapshotStore(db_path)
    latest = reopened.latest_scan()
    assert latest is not None
    assert latest.scan_id == scan_id
    assert latest.stats.files_scanned == 2
    assert latest.stats.total_size == 15
    assert reopened.count_entries(scan_id) == 2
    assert reopened.get_scan(scan_id).stats.complete is True

    indexes = {
        row["name"]
        for row in reopened.connection.execute("PRAGMA index_list('entries')").fetchall()
    }
    assert "idx_entries_scan_rel" in indexes
    assert "idx_entries_scan_size" in indexes

    json_path = tmp_path / "export.json"
    csv_path = tmp_path / "export.csv"
    reopened.export_json(scan_id, json_path, top=5)
    reopened.export_entries_csv(scan_id, csv_path)
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["scan"]["scan_id"] == scan_id
    assert payload["scan"]["stats"]["files_scanned"] == 2
    assert csv_path.read_text(encoding="utf-8-sig").splitlines()[0].startswith("rel_path")
    reopened.close()


def test_migrate_v1_to_v2_preserves_data(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    root = tmp_path / "root"
    root.mkdir()
    legacy_file = root / "legacy.txt"
    write_bytes(legacy_file, b"abcde")

    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_V1_SQL)
    conn.execute(
        "INSERT INTO scans(id, root, started_at, finished_at, files_scanned, dirs_scanned, "
        "total_size, complete) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("legacy-scan", str(root), "2024-01-01T00:00:00+00:00", "2024-01-01T00:00:02+00:00", 1, 1, 5, 1),
    )
    conn.execute(
        "INSERT INTO entries(scan_id, rel_path, path, name, suffix, size, kind, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("legacy-scan", "legacy.txt", str(legacy_file), "legacy.txt", ".txt", 5, "file", "scanned"),
    )
    conn.execute("INSERT INTO dirs(scan_id, rel_path, path, size, file_count) VALUES (?, ?, ?, ?, ?)",
                 ("legacy-scan", "", str(root), 5, 1))
    conn.execute("PRAGMA user_version=1")
    conn.commit()
    conn.close()

    store = SnapshotStore(db_path)
    assert store.schema_version() == SCHEMA_VERSION
    columns = {row[1] for row in store.connection.execute("PRAGMA table_info(entries)").fetchall()}
    assert "hash" in columns
    scan = store.get_scan("legacy-scan")
    assert scan is not None
    assert scan.stats.files_scanned == 1
    assert scan.stats.total_size == 5
    entries = list(store.iter_entries("legacy-scan"))
    assert len(entries) == 1
    assert entries[0].rel_path == "legacy.txt"
    assert entries[0].sha256 is None
    assert store.integrity_ok()
    assert store.list_watch_targets() == []
    store.close()


def test_watch_targets_upsert(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path / "watch.db")
    assert store.list_watch_targets() == []
    store.upsert_watch_target("data", 100, 50, "2024-01-01T00:00:00+00:00")
    store.upsert_watch_target("data", 200, 80, "2024-02-01T00:00:00+00:00")
    targets = store.list_watch_targets()
    assert len(targets) == 1
    assert targets[0].threshold_bytes == 200
    assert targets[0].baseline_bytes == 80
    store.close()
