from __future__ import annotations

import time

from helpers import make_pdf, write_file

from recallfs.index_scheduler import IndexScheduler
from recallfs.search_engine import SearchEngine


def _bulk_files(root, count: int) -> None:
    for index in range(count):
        write_file(
            root,
            f"bulk/file_{index:04d}.txt",
            f"批量文件 {index} 内容 " + "填充文本 " * 40 + "\n",
        )


def test_async_index_completes(tmp_path, db):
    root = tmp_path / "async_root"
    root.mkdir()
    _bulk_files(root, 20)
    scheduler = IndexScheduler(db)
    scheduler.start_async([str(root)])
    assert scheduler.wait(60)
    status = scheduler.status()
    assert status["state"] == "completed"
    assert status["last_stats"]["added"] == 20
    assert db.file_count() == 20


def test_pause_resume_cancel(tmp_path, db):
    root = tmp_path / "control_root"
    root.mkdir()
    _bulk_files(root, 300)
    scheduler = IndexScheduler(db)
    scheduler.start_async([str(root)])
    scheduler.pause()

    deadline = time.time() + 30
    paused_state = None
    while time.time() < deadline:
        status = scheduler.status()
        paused_state = status["state"]
        if paused_state == "paused":
            break
        if not scheduler.is_running():
            break
        time.sleep(0.02)
    assert paused_state == "paused", "索引任务应进入暂停状态"

    time.sleep(0.3)
    processed_a = scheduler.status()["processed"]
    time.sleep(0.2)
    processed_b = scheduler.status()["processed"]
    assert processed_a == processed_b, "暂停期间不应继续处理文件"

    scheduler.resume()
    assert scheduler.status()["state"] == "running"
    scheduler.cancel()
    assert scheduler.wait(60)
    status = scheduler.status()
    assert not scheduler.is_running()
    assert status["state"] in ("cancelled", "completed")
    assert db.integrity_check().ok

    stats = scheduler.scan([str(root)])
    assert stats.added >= 0
    assert db.file_count() == 300


def test_retry_failed_after_fixing_file(tmp_path, db):
    root = tmp_path / "retry_root"
    root.mkdir()
    broken = write_file(root, "doc.pdf", b"%PDF-1.4 broken")
    scheduler = IndexScheduler(db)
    scheduler.scan([str(root)])
    row = db.get_file_by_path(broken)
    assert row["parse_status"] == "error"
    make_pdf(broken, ["Recovered pdf text keyword"])
    report = scheduler.retry_failed()
    assert report["attempted"] == 1
    row = db.get_file_by_path(broken)
    assert row["parse_status"] == "ok"
    assert SearchEngine(db).search("Recovered").hits


def test_scan_without_roots_returns_zero(tmp_path, db):
    scheduler = IndexScheduler(db)
    stats = scheduler.scan(None)
    assert stats.roots == 0
    assert stats.files_scanned == 0
