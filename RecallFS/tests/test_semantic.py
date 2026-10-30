from __future__ import annotations

import pytest

from helpers import write_file

from recallfs.index_scheduler import IndexScheduler
from recallfs.project_collections import RelatedFiles
from recallfs.search_engine import SearchEngine
from recallfs.semantic_index import MODEL_LABEL, MODEL_NAME, SemanticIndex


def test_semantic_fallback_without_vectors(corpus, scheduler, engine):
    write_file(corpus, "fallback.md", "回退检索关键词 全文命中\n")
    scheduler.scan([str(corpus)])
    result = engine.search("回退检索关键词", mode="semantic")
    assert result.mode == "semantic-fallback-fulltext"
    assert result.hits
    assert "回退" in (result.warning or "")
    assert result.semantic["available"] is False
    assert result.semantic["fallback"] == "fulltext"
    assert result.semantic["label"] == MODEL_LABEL
    assert result.semantic["is_neural_model"] is False


def test_semantic_build_search_and_clear(corpus, scheduler, db):
    write_file(corpus, "a.md", "本地离线检索应用 支持中文检索与证据定位\n")
    write_file(corpus, "b.txt", "完全不同的内容 alpha beta gamma delta\n")
    scheduler.scan([str(corpus)])
    semantic = SemanticIndex(db)
    report = semantic.build()
    assert report["model"] == MODEL_NAME
    assert report["is_neural_model"] is False
    assert "非语义模型" in report["label"]
    assert report["indexed_chunks"] >= 1
    assert report["coverage"] > 0

    stats = semantic.stats()
    assert stats["available"] is True
    assert stats["dim"] == 256
    assert stats["network_required"] is False
    assert stats["auto_download"] is False

    engine = SearchEngine(db, semantic=semantic)
    result = engine.search("中文检索", mode="semantic")
    assert result.mode == "semantic-local-hash"
    assert result.hits
    assert result.hits[0].layer == "semantic"
    assert result.semantic["label"] == MODEL_LABEL
    assert result.semantic["is_neural_model"] is False
    assert "不代表事实" in result.semantic.get("notes", "")

    removed = semantic.clear()
    assert removed >= 1
    assert semantic.coverage()["available"] is False
    fallback = engine.search("中文检索", mode="semantic")
    assert fallback.mode == "semantic-fallback-fulltext"


def test_semantic_resource_limits(corpus, scheduler, db):
    for index in range(5):
        write_file(corpus, f"doc_{index}.md", f"资源限制测试文档 {index} 内容内容内容\n")
    scheduler.scan([str(corpus)])
    semantic = SemanticIndex(db, dim=64, max_chunks=2, max_chars=100)
    report = semantic.build()
    assert report["dim"] == 64
    assert report["indexed_chunks"] == 2
    assert report["truncated_by_limit"] is True
    assert semantic.stats()["max_chunks"] == 2


def test_hash_vector_is_not_neural_model():
    semantic = SemanticIndex.__new__(SemanticIndex)  # 仅检查类属性，不连接数据库
    assert semantic.is_neural_model is False
    assert "非语义" in semantic.label


def test_related_files_with_reasons(corpus, scheduler, db):
    base = write_file(
        corpus,
        "base.md",
        "alpha beta gamma delta epsilon 基础文档内容\n",
    )
    duplicate = corpus / "copy_base.txt"
    duplicate.write_bytes(base.read_bytes())
    reference = write_file(corpus, "ref.md", "请参见 base.md 中的说明与示例\n")
    shared = write_file(corpus, "shared.md", "alpha beta gamma zeta 共同主题\n")
    unrelated = write_file(corpus, "unrelated.md", "完全无关的其他内容 omega\n")
    scheduler.scan([str(corpus)])

    base_row = db.get_file_by_path(base)
    related = RelatedFiles(db).related(int(base_row["id"]), limit=10)
    names = {item["name"] for item in related}
    assert "copy_base.txt" in names
    assert "ref.md" in names
    assert "shared.md" in names
    assert "unrelated.md" not in names

    by_name = {item["name"]: item for item in related}
    assert any("哈希" in reason for reason in by_name["copy_base.txt"]["reasons"])
    assert any("引用" in reason for reason in by_name["ref.md"]["reasons"])
    assert any("共同关键词" in reason for reason in by_name["shared.md"]["reasons"])
    assert all(item["score"] > 0 for item in related)


def test_related_unknown_file_returns_empty(db):
    assert RelatedFiles(db).related(999999) == []
