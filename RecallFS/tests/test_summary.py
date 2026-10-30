from __future__ import annotations

from helpers import write_file

from recallfs.summary import build_extract_summary, format_locator


def test_summary_is_grounded_excerpt(corpus, scheduler, db):
    path = write_file(
        corpus,
        "docs/note.md",
        "# 标题\n\n" + ("第一段内容。" * 8) + "\n\n第二段：这里的内容在截断后不应出现。\n",
    )
    scheduler.scan([str(corpus)])
    row = db.get_file_by_path(path)
    assert row is not None

    data = build_extract_summary(db, row, max_chars=50)
    assert data["excerpt"].startswith("# 标题")
    assert "第一段" in data["excerpt"]
    assert "第二段" not in data["excerpt"]
    assert data["truncated"] is True
    assert data["is_extract"] is True
    assert "原文摘录" in data["note"]
    assert data["locators"]
    assert data["locators"][0]["start_line"] == 1


def test_summary_without_extractable_text(corpus, scheduler, db):
    path = write_file(corpus, "data/unknown.bin", b"\x00\x01\x02\x03")
    scheduler.scan([str(corpus)])
    row = db.get_file_by_path(path)
    assert row is not None

    data = build_extract_summary(db, row)
    assert data["excerpt"] is None
    assert "没有可提取的文本" in data["reason"]
    assert data["locators"] == []


def test_locator_label_combines_page_and_lines():
    locator = {"ordinal": 1, "page": 3, "start_line": 10, "end_line": 12}
    assert format_locator(locator) == "第 3 页 · 第 10–12 行"
