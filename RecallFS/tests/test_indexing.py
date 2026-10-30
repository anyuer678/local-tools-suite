from __future__ import annotations

import os
import time

from helpers import make_pdf, write_file

from recallfs.search_engine import SearchFilters


def test_index_and_search_known_content(corpus, scheduler, engine, db):
    note = write_file(
        corpus,
        "项目笔记.md",
        "# 项目笔记\n\nRecallFS 支持中文检索。\n证据定位应当回到原始文件的第 4 行。\n",
    )
    special = write_file(corpus, "特殊 #1 & [x].txt", "特殊字符测试 & <tag> 100% 完成。\n")
    empty = write_file(corpus, "empty.txt", "")
    long_line = write_file(corpus, "long.txt", "A" * 4900 + "超长行标记" + "B" * 100 + "\n")
    broken = make_pdf(corpus / "broken.pdf", ["placeholder"])  # 先写一个合法 PDF，再破坏它
    broken.write_bytes(b"%PDF-1.4\nthis is not a real pdf body\n")

    stats = scheduler.scan([str(corpus)])
    assert stats.added == 5
    assert stats.files_scanned == 5
    assert stats.parse_errors >= 1
    assert stats.errors == []
    # 索引库自身不会被索引
    assert db.file_count() == 5

    result = engine.search("中文检索")
    assert result.empty is False
    assert result.hits[0].path == str(note)
    assert "<mark>中文检索</mark>" in result.hits[0].snippet
    assert result.hits[0].kind in ("raw_text", "metadata")

    line_result = engine.search("证据定位")
    assert line_result.hits
    hit = line_result.hits[0]
    assert hit.start_line == 1
    assert hit.end_line >= 4
    evidence = engine.evidence(hit)
    assert evidence["stale"] is False
    assert evidence["content_verified"] is True
    text_lines = [line for _no, line in evidence["live_lines"]]
    assert any("证据定位" in line for line in text_lines)

    assert engine.search("特殊字符测试").hits
    assert engine.search("100%").hits
    exact = engine.search("特殊 #1 & [x]")
    assert exact.hits[0].layer == "exact_name"
    assert exact.hits[0].path == str(special)

    empty_hits = engine.search("empty")
    assert empty_hits.hits
    assert empty_hits.hits[0].path == str(empty)
    assert empty_hits.hits[0].parse_status == "ok"

    long_hits = engine.search("超长行标记")
    assert long_hits.hits
    assert long_hits.hits[0].path == str(long_line)
    assert long_hits.hits[0].start_line == 1

    broken_hits = engine.search("broken")
    assert broken_hits.hits
    assert broken_hits.hits[0].path == str(broken)
    assert broken_hits.hits[0].parse_status == "error"
    assert "PDF" in (broken_hits.hits[0].parse_error or "")


def test_line_numbers_are_accurate_for_multichunk_file(corpus, scheduler, engine):
    lines = [f"第 {index} 行 普通内容" for index in range(1, 121)]
    lines[99] = "第 100 行 需要精确命中的唯一关键词 探测器"
    path = write_file(corpus, "many_lines.txt", "\n".join(lines) + "\n")
    scheduler.scan([str(corpus)])
    result = engine.search("探测器")
    assert result.hits
    hit = result.hits[0]
    assert hit.path == str(path)
    assert hit.start_line <= 100 <= hit.end_line
    evidence = engine.evidence(hit)
    assert evidence["stale"] is False
    assert evidence["content_verified"] is True
    assert any("100" in str(number) for number, _text in evidence["live_lines"])
    assert any("探测器" in text for _number, text in evidence["live_lines"])


def test_incremental_add_modify_delete_move(corpus, scheduler, engine, db):
    first = write_file(corpus, "a.txt", "旧内容 alpha 第一版\n")
    stats = scheduler.scan([str(corpus)])
    assert stats.added == 1
    assert len(engine.search("旧内容").hits) == 1
    source_before = first.read_bytes()

    # 修改：不同长度，确保 mtime/size 都变化
    time.sleep(0.02)
    first.write_text("新内容 beta 第二版内容更长一些\n", encoding="utf-8")
    stats = scheduler.scan([str(corpus)])
    assert stats.modified == 1
    assert engine.search("旧内容").empty
    assert engine.search("旧内容", SearchFilters(include_stale=True)).empty  # 旧 chunk 已被替换
    assert engine.search("新内容").hits
    # 绝不修改源文件
    assert first.read_text(encoding="utf-8") == "新内容 beta 第二版内容更长一些\n"

    # 新增
    second = write_file(corpus, "sub/b.txt", "移动测试内容 gamma\n")
    stats = scheduler.scan([str(corpus)])
    assert stats.added == 1
    assert stats.unchanged >= 1

    # 移动：同 size + 同 hash
    old_row = db.get_file_by_path(str(second))
    assert old_row is not None
    target = corpus / "moved" / "b2.txt"
    target.parent.mkdir()
    os.rename(second, target)
    stats = scheduler.scan([str(corpus)])
    assert stats.moved == 1
    assert stats.deleted == 0
    moved_result = engine.search("移动测试内容")
    assert moved_result.hits
    assert moved_result.hits[0].path == str(target)
    assert moved_result.hits[0].file_id == int(old_row["id"])

    # 删除
    os.remove(first)
    stats = scheduler.scan([str(corpus)])
    assert stats.deleted == 1
    assert engine.search("新内容").empty


def test_query_time_staleness_filters_and_marks(corpus, scheduler, engine):
    path = write_file(corpus, "stale.txt", "稳定的关键词 原始版本\n")
    scheduler.scan([str(corpus)])
    assert engine.search("稳定的关键词").hits

    time.sleep(0.02)
    path.write_text("已经变化的内容 更长版本，替换原来的文本\n", encoding="utf-8")

    fresh = engine.search("稳定的关键词")
    assert fresh.empty
    assert fresh.empty_reason == "all_results_stale"

    stale = engine.search("稳定的关键词", SearchFilters(include_stale=True))
    assert stale.hits
    assert stale.hits[0].stale is True
    assert "失效" in (stale.hits[0].stale_reason or "")

    evidence = engine.evidence(stale.hits[0])
    assert evidence["stale"] is True
    assert evidence["stale_reason"]


def test_rescan_is_incremental_and_counts_unchanged(corpus, scheduler):
    for index in range(5):
        write_file(corpus, f"file_{index}.txt", f"内容 {index}\n")
    first = scheduler.scan([str(corpus)])
    assert first.added == 5
    second = scheduler.scan([str(corpus)])
    assert second.added == 0
    assert second.modified == 0
    assert second.deleted == 0
    assert second.unchanged == 5
    assert second.files_scanned == 5


def test_stats_and_integrity_after_noop_scan(corpus, scheduler, db):
    write_file(corpus, "ok.txt", "完整性检查内容\n")
    scheduler.scan([str(corpus)])
    report = scheduler.verify_integrity()
    assert report["ok"] is True
    assert report["files_missing_on_disk"] == 0
    assert db.integrity_check().ok is True
