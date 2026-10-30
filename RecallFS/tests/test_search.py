from __future__ import annotations

import os
import time

from helpers import write_file

from recallfs.search_engine import SearchFilters


def test_filters_ext_mtime_size_name_path(corpus, scheduler, engine):
    py_file = write_file(corpus, "code/filter.py", "# 筛选关键词 共享\nprint('筛选关键词')\n")
    md_file = write_file(corpus, "docs/filter.md", "筛选关键词 文档\n")
    old_time = time.time() - 60 * 60 * 24 * 30
    os.utime(py_file, (old_time, old_time))
    scheduler.scan([str(corpus)])

    by_ext = engine.search("筛选关键词", SearchFilters(exts=[".py"]))
    assert [hit.path for hit in by_ext.hits] == [str(py_file)]

    by_mtime = engine.search(
        "筛选关键词", SearchFilters(mtime_from=time.time() - 3600)
    )
    assert {hit.path for hit in by_mtime.hits} == {str(md_file)}

    by_size = engine.search("筛选关键词", SearchFilters(size_min=1000))
    assert by_size.empty

    by_name = engine.search("筛选关键词", SearchFilters(name_contains="filter.md"))
    assert {hit.path for hit in by_name.hits} == {str(md_file)}

    by_path = engine.search("筛选关键词", SearchFilters(path_contains="docs"))
    assert {hit.path for hit in by_path.hits} == {str(md_file)}

    both = engine.search(
        "筛选关键词", SearchFilters(exts=[".md"], name_contains="filter", path_contains="docs")
    )
    assert {hit.path for hit in both.hits} == {str(md_file)}


def test_phrase_search(corpus, scheduler, engine):
    write_file(corpus, "phrase.txt", "alpha beta gamma\nsecond line\n")
    scheduler.scan([str(corpus)])
    assert engine.search('"alpha beta"').hits
    assert engine.search('"beta alpha"').empty
    assert engine.search('"alpha beta"').hits[0].matched_terms


def test_chinese_query_and_phrase(corpus, scheduler, engine):
    path = write_file(corpus, "中文.md", "这是一个本地离线检索应用，支持中文分词回退。\n")
    scheduler.scan([str(corpus)])
    for query in ("离线检索", "中文分词", "检索应用", "中文"):
        result = engine.search(query)
        assert result.hits, query
        assert result.hits[0].path == str(path)
    phrase = engine.search('"本地离线检索应用"')
    assert phrase.hits
    single = engine.search("检")
    assert single.hits


def test_bm25_ordering(corpus, scheduler, engine):
    write_file(corpus, "once.txt", "needleword 出现一次\n")
    write_file(
        corpus,
        "many.txt",
        "needleword " * 30 + "\nneedleword 再次出现\nneedleword 第三次\n",
    )
    scheduler.scan([str(corpus)])
    result = engine.search("needleword")
    assert len(result.hits) >= 2
    assert result.hits[0].name == "many.txt"
    assert result.hits[0].score >= result.hits[-1].score


def test_multi_term_and_exclusion(corpus, scheduler, engine):
    write_file(corpus, "both.txt", "alpha beta 同时出现\n")
    write_file(corpus, "only.txt", "alpha 单独出现\n")
    scheduler.scan([str(corpus)])
    result = engine.search("alpha beta")
    assert {hit.name for hit in result.hits} == {"both.txt"}
    excluded = engine.search("alpha -beta")
    assert {hit.name for hit in excluded.hits} == {"only.txt"}


def test_symbol_filter(corpus, scheduler, engine):
    code = write_file(corpus, "symbol.py", "def special_symbol(value):\n    return value\n")
    mention = write_file(corpus, "mention.txt", "这里只是提到了 special_symbol 这个词\n")
    scheduler.scan([str(corpus)])

    all_hits = engine.search("special_symbol")
    assert {hit.path for hit in all_hits.hits} == {str(code), str(mention)}

    filtered = engine.search("special_symbol", SearchFilters(symbols=["special_symbol"]))
    assert filtered.hits
    assert {hit.path for hit in filtered.hits} == {str(code)}
    assert any(hit.kind == "raw_text" for hit in filtered.hits)

    unknown = engine.search("special_symbol", SearchFilters(symbols=["not_a_symbol"]))
    assert unknown.empty


def test_metadata_kind_and_exact_name_layer(corpus, scheduler, engine):
    named = write_file(corpus, "季度报告.md", "正文内容与文件名无关。\n")
    other = write_file(corpus, "notes.txt", "季度报告 出现在正文中。\n")
    scheduler.scan([str(corpus)])

    exact = engine.search("季度报告")
    assert exact.hits
    assert exact.hits[0].layer == "exact_name"
    assert exact.hits[0].path == str(named)
    assert exact.hits[0].score >= 1000

    metadata_only = engine.search("季度报告", SearchFilters(kinds=["metadata"], exact_name=False))
    assert metadata_only.hits
    assert metadata_only.hits[0].kind == "metadata"
    assert {hit.path for hit in metadata_only.hits} == {str(named)}

    # 禁用精确文件名层后，正文命中也应出现
    no_exact = engine.search("季度报告", SearchFilters(exact_name=False))
    assert any(hit.path == str(other) for hit in no_exact.hits)


def test_empty_results_are_explicit(corpus, scheduler, engine):
    write_file(corpus, "a.txt", "abc\n")
    scheduler.scan([str(corpus)])
    result = engine.search("绝对不存在的词汇xyzzy")
    assert result.empty
    assert result.empty_reason == "no_match"
    assert result.total == 0
    empty_query = engine.search("   ")
    assert empty_query.empty_reason == "empty_query"


def test_highlight_markers(corpus, scheduler, engine):
    write_file(corpus, "h.md", "第一段\n高亮关键词出现在这里\n")
    scheduler.scan([str(corpus)])
    hit = engine.search("高亮关键词").hits[0]
    assert "<mark>高亮关键词</mark>" in hit.snippet
    assert hit.matched_terms == ["高亮关键词"]
    assert hit.matched_field == "text"


def test_parse_status_filter(corpus, scheduler, engine):
    write_file(corpus, "bad.pdf", b"%PDF-1.4 broken body")
    write_file(corpus, "good.txt", "正常文档 keyword\n")
    scheduler.scan([str(corpus)])
    errors = engine.search("bad", SearchFilters(parse_status=["error"], exact_name=False))
    assert errors.hits
    assert errors.hits[0].parse_status == "error"
    ok = engine.search("keyword", SearchFilters(parse_status=["ok"]))
    assert {hit.name for hit in ok.hits} == {"good.txt"}


def test_filters_combination_and_offset(corpus, scheduler, engine):
    for index in range(5):
        write_file(corpus, f"combo_{index}.py", f"组合关键词 {index}\n")
    scheduler.scan([str(corpus)])
    result = engine.search("组合关键词", SearchFilters(exts=[".py"]), limit=2, offset=2)
    assert len(result.hits) == 2
    assert result.total >= 5
