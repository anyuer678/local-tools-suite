from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

from helpers import write_file

from recallfs import cli

CORE_MODULES = [
    "index_scheduler.py",
    "filesystem_watcher.py",
    "document_parser.py",
    "content_chunker.py",
    "fulltext_index.py",
    "semantic_index.py",
    "search_engine.py",
    "evidence_locator.py",
    "project_collections.py",
    "cli.py",
    "__main__.py",
]


def test_core_modules_do_not_use_network_imports():
    src_dir = Path(__file__).resolve().parents[1] / "src" / "recallfs"
    forbidden = ("requests", "urllib", "http.client", "httpx", "aiohttp", "socket")
    for name in CORE_MODULES:
        source = (src_dir / name).read_text(encoding="utf-8")
        lowered = source.casefold()
        for token in forbidden:
            assert f"import {token}" not in lowered, f"{name} 不应导入网络模块 {token}"
            assert f"from {token}" not in lowered, f"{name} 不应从 {token} 导入"
    assert "requests" not in sys.modules


def test_index_and_search_without_network(corpus, tmp_path, monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("核心检索流程不应访问网络")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)

    db_path = tmp_path / "offline.sqlite3"
    write_file(corpus, "offline.md", "离线检索 不访问网络 本地索引\n")
    code = cli.main(["--db", str(db_path), "index", "--root", str(corpus)])
    assert code == 0
    result = cli.main(["--db", str(db_path), "search", "离线检索", "--json"])
    assert result == 0


def test_cli_help_and_version(capsys):
    assert cli.main(["--help"]) == 0
    output = capsys.readouterr().out
    assert "RecallFS" in output
    assert "index" in output and "search" in output
    assert cli.main(["--version"]) == 0
    assert "0.1.0" in capsys.readouterr().out


def test_cli_end_to_end(corpus, tmp_path, capsys):
    db_path = str(tmp_path / "cli.sqlite3")
    note = write_file(corpus, "cli笔记.md", "第一行\n命令行检索关键词 在此\n第三行\n")

    assert cli.main(["--db", db_path, "index", "--root", str(corpus)]) == 0
    out = capsys.readouterr().out
    assert "索引完成" in out and "新增 1" in out

    assert cli.main(["--db", db_path, "search", "命令行检索关键词"]) == 0
    out = capsys.readouterr().out
    assert "命令行检索关键词" in out
    assert "【命令行检索关键词】" in out or "命令行检索关键词" in out
    assert str(note) in out

    assert cli.main(["--db", db_path, "search", "不存在的查询词xyz"]) == 1
    assert "空结果" in capsys.readouterr().out

    assert cli.main(["--db", db_path, "show", str(note)]) == 0
    out = capsys.readouterr().out
    assert "文件:" in out and "chunk #" in out
    assert "line=" in out

    assert cli.main(["--db", db_path, "status", "--json"]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["files"] == 1
    assert stats["optional_parsers"]["pypdf"] in (True, False)

    assert cli.main(["--db", db_path, "verify"]) == 0
    assert "完整性" in capsys.readouterr().out

    assert cli.main(["--db", db_path, "collections", "create", "资料", "--kind", "project", "--root", str(corpus)]) == 0
    capsys.readouterr()
    assert cli.main(["--db", db_path, "collections", "list", "--json"]) == 0
    collections = json.loads(capsys.readouterr().out)
    assert collections[0]["name"] == "资料"
    assert collections[0]["file_count"] == 1

    assert cli.main(["--db", db_path, "favorites", "add-file", str(note), "--note", "重要"]) == 0
    capsys.readouterr()
    assert cli.main(["--db", db_path, "favorites", "list", "--json"]) == 0
    favorites = json.loads(capsys.readouterr().out)
    assert favorites[0]["path"] == str(note)

    assert cli.main(["--db", db_path, "search", "命令行检索关键词"]) == 0
    capsys.readouterr()
    assert cli.main(["--db", db_path, "recent", "--json"]) == 0
    recent = json.loads(capsys.readouterr().out)
    assert recent and recent[0]["query"] == "命令行检索关键词"

    assert cli.main(["--db", db_path, "related", str(note), "--json"]) == 0
    assert isinstance(json.loads(capsys.readouterr().out), list)

    assert cli.main(["--db", db_path, "semantic", "build"]) == 0
    out = capsys.readouterr().out
    assert "非语义模型" in out
    assert cli.main(["--db", db_path, "semantic", "stats", "--json"]) == 0
    semantic = json.loads(capsys.readouterr().out)
    assert semantic["is_neural_model"] is False
    assert semantic["label"] == "本地哈希向量（非语义模型）"


def test_cli_search_with_filters(corpus, tmp_path, capsys):
    db_path = str(tmp_path / "filters.sqlite3")
    write_file(corpus, "keep.py", "过滤词 content\n")
    write_file(corpus, "skip.md", "过滤词 content\n")
    cli.main(["--db", db_path, "index", "--root", str(corpus)])
    capsys.readouterr()
    assert cli.main(["--db", db_path, "search", "过滤词", "--ext", ".py", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["hits"]) == 1
    assert payload["hits"][0]["name"] == "keep.py"


def test_cli_bad_root(capsys):
    code = cli.main(["index", "--root", "C:/definitely/not/existing/path"])
    assert code == 2
    assert "错误" in capsys.readouterr().err


def test_module_help_via_subprocess():
    import subprocess

    project_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(project_root / "src")
    completed = subprocess.run(
        [sys.executable, "-m", "recallfs", "--help"],
        cwd=str(project_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0
    assert "usage: recallfs" in completed.stdout
    assert "search" in completed.stdout
