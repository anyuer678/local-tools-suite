"""策略与建议测试：排除规则、关键路径、分类、增长观察、建议证据。"""

from __future__ import annotations

import os
from pathlib import Path

from spacepilot.duplicates import find_duplicates_from_store
from spacepilot.policy import (
    ExcludeRules,
    add_watch_target,
    classify_file,
    evaluate_watch_targets,
    generate_recommendations,
    is_critical_path,
)
from spacepilot.scanner import Scanner
from spacepilot.snapshot_store import SnapshotStore


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_exclude_rules_normalization() -> None:
    rules = ExcludeRules(dir_names=frozenset({"Node_Modules"}), suffixes=frozenset({"TMP", "log"}))
    assert rules.should_exclude_dir("node_modules") is True
    assert rules.should_exclude_dir("src") is False
    assert rules.should_exclude_file("a.TMP", 10)[0] is True
    assert rules.should_exclude_file("b.log", 10)[0] is True
    assert rules.should_exclude_file("c.txt", 10)[0] is False
    assert rules.should_exclude_file("small.txt", 2)[0] is False


def test_critical_paths(tmp_path: Path) -> None:
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
    assert is_critical_path(windir) is True
    assert is_critical_path(os.path.join(windir, "System32")) is True
    assert is_critical_path(os.environ.get("ProgramFiles", r"C:\Program Files")) is True
    assert is_critical_path(os.environ.get("USERPROFILE", str(Path.home()))) is True
    assert is_critical_path(tmp_path / "ordinary.txt") is False


def test_classify_file_rules() -> None:
    temp = classify_file("Temp/cache.tmp", "cache.tmp", 2048)
    assert temp is not None
    assert temp[0] == "临时文件"
    assert temp[1] == "low"
    assert temp[2] == "quarantine"

    log = classify_file("backup/app.log", "app.log", 4096)
    assert log is not None
    assert log[1] == "medium"
    assert log[2] == "quarantine"

    huge = classify_file("unknown.bin", "unknown.bin", 2 * 1024 * 1024 * 1024)
    assert huge is not None
    assert huge[1] == "high"
    assert huge[2] == "review"

    assert classify_file("notes.txt", "notes.txt", 100) is None


def test_watch_target_detects_growth(tmp_path: Path) -> None:
    db = SnapshotStore(tmp_path / "policy.db")
    root = tmp_path / "root"
    root.mkdir()
    write_bytes(root / "data" / "base.bin", b"d" * 100)

    first = Scanner(root, db).scan()
    target = add_watch_target(db, "data", threshold_bytes=50, scan_id=first.scan_id)
    assert target.baseline_bytes == 100
    alerts = evaluate_watch_targets(db, first.scan_id)
    assert alerts[0].exceeded is False

    write_bytes(root / "data" / "extra.bin", b"e" * 100)
    second = Scanner(root, db).scan()
    alerts = evaluate_watch_targets(db, second.scan_id)
    assert alerts[0].baseline_bytes == 100
    assert alerts[0].current_bytes == 200
    assert alerts[0].growth == 100
    assert alerts[0].exceeded is True

    recommendations = generate_recommendations(db, second.scan_id, min_benefit=1)
    growth_recs = [rec for rec in recommendations if rec.category == "增长异常"]
    assert growth_recs
    assert growth_recs[0].requires_review is True
    assert growth_recs[0].evidence
    db.close()


def test_duplicate_recommendation_has_evidence(tmp_path: Path) -> None:
    db = SnapshotStore(tmp_path / "policy_dup.db")
    root = tmp_path / "root"
    root.mkdir()
    write_bytes(root / "one.bin", b"same" * 10)
    write_bytes(root / "two.bin", b"same" * 10)
    write_bytes(root / "Cache" / "temp.tmp", b"t" * 4096)

    scan = Scanner(root, db).scan()
    report = find_duplicates_from_store(db, scan.scan_id, min_size=1)
    recommendations = generate_recommendations(db, scan.scan_id, duplicates=report)

    duplicate_recs = [rec for rec in recommendations if rec.category == "重复文件"]
    assert duplicate_recs
    assert duplicate_recs[0].action == "quarantine"
    assert duplicate_recs[0].risk == "low"
    assert duplicate_recs[0].benefit_bytes == 40
    assert "SHA-256" in duplicate_recs[0].evidence

    temp_recs = [rec for rec in recommendations if rec.category == "临时文件"]
    assert temp_recs
    assert temp_recs[0].action == "quarantine"
    assert temp_recs[0].evidence
    db.close()
