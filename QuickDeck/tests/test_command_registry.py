from __future__ import annotations

from pathlib import Path

import pytest

from quickdeck.command_registry import (
    Command,
    CommandDisabled,
    CommandExecutionError,
    CommandNotFound,
    CommandRegistry,
)


def _add_commands(registry: CommandRegistry) -> None:
    registry.register_command(
        "calc.eval",
        "计算器",
        aliases=["calc", "="],
        category="数学",
        keywords=["expression", "math"],
        description="安全计算表达式",
        handler=lambda ctx: f"{ctx.text}:ok",
    )
    registry.register_command(
        "text.toolkit",
        "文本工具",
        aliases=["txt"],
        category="文本",
        keywords=["text", "dedupe"],
        description="文本处理集合",
        handler=lambda ctx: ctx.text.upper(),
    )
    registry.register_command(
        "encoding.base64",
        "Base64 编码",
        aliases=["b64"],
        category="编码",
        keywords=["encode", "base64"],
        description="Base64",
        handler=lambda ctx: "encoded",
    )
    registry.register_command(
        "time.record",
        "记录时间",
        aliases=["time"],
        category="时间",
        handler=lambda ctx: "recorded",
    )


def test_register_and_search_exact_and_alias(tmp_path: Path) -> None:
    registry = CommandRegistry(tmp_path / "q.db")
    _add_commands(registry)
    assert registry.get("calc.eval").name == "计算器"  # type: ignore[union-attr]
    assert registry.search("calc")[0].id == "calc.eval"
    assert registry.search("=")[0].id == "calc.eval"
    assert registry.search("b64")[0].id == "encoding.base64"
    assert registry.search("记录时间")[0].id == "time.record"
    registry.close()


def test_fuzzy_subsequence_search(tmp_path: Path) -> None:
    registry = CommandRegistry(tmp_path / "q.db")
    _add_commands(registry)
    registry.register_command(
        "fuzzy.demo",
        "Transformer Toolkit",
        category="测试",
        handler=lambda ctx: "fuzzy",
    )
    results = registry.search("trfkit")
    assert results and results[0].id == "fuzzy.demo"
    assert any(command.id == "text.toolkit" for command in registry.search("txt"))
    assert registry.search("b64")[0].id == "encoding.base64"
    registry.close()


def test_category_filter(tmp_path: Path) -> None:
    registry = CommandRegistry(tmp_path / "q.db")
    _add_commands(registry)
    assert {c.id for c in registry.search("", category="文本")} == {"text.toolkit"}
    assert {c.id for c in registry.search("base64", category="编码")} == {"encoding.base64"}
    assert registry.search("nothing", category="文本") == []
    registry.close()


def test_frequency_and_recent_sorting(tmp_path: Path) -> None:
    registry = CommandRegistry(tmp_path / "q.db")
    registry.register_command("a.first", "Alpha Tool", category="测试", handler=lambda ctx: "a")
    registry.register_command("b.second", "Alpha Helper", category="测试", handler=lambda ctx: "b")
    registry.record_use("a.first")
    registry.record_use("a.first")
    registry.record_use("b.second")
    results = registry.search("alpha")
    assert results[0].id == "a.first"
    assert registry.get("a.first").use_count == 2  # type: ignore[union-attr]

    registry.record_use("b.second")
    registry.record_use("b.second")
    # 次数相同后，最近使用优先
    registry.record_use("b.second")
    assert registry.search("alpha")[0].id == "b.second"
    registry.close()


def test_empty_query_uses_usage(tmp_path: Path) -> None:
    registry = CommandRegistry(tmp_path / "q.db")
    _add_commands(registry)
    registry.record_use("text.toolkit")
    assert registry.search("")[0].id == "text.toolkit"
    registry.close()


def test_sqlite_persistence_and_usage_preserved(tmp_path: Path) -> None:
    path = tmp_path / "q.db"
    registry = CommandRegistry(path)
    _add_commands(registry)
    registry.record_use("calc.eval")
    registry.close()

    registry = CommandRegistry(path)
    assert registry.get("calc.eval").use_count == 1  # type: ignore[union-attr]
    registry.register_command(
        "calc.eval",
        "计算器（新）",
        aliases=["calc"],
        category="数学",
    )
    assert registry.get("calc.eval").use_count == 1  # type: ignore[union-attr]
    assert registry.get("calc.eval").name == "计算器（新）"  # type: ignore[union-attr]
    registry.close()


def test_execute_and_errors(tmp_path: Path) -> None:
    registry = CommandRegistry(tmp_path / "q.db")
    _add_commands(registry)
    assert registry.execute("text.toolkit", "abc") == "ABC"
    assert registry.get("text.toolkit").use_count == 1  # type: ignore[union-attr]
    with pytest.raises(CommandNotFound):
        registry.execute("missing")
    with pytest.raises(CommandExecutionError):
        registry.register_command("no.handler", "无处理器")
        registry.execute("no.handler")
    registry.set_enabled("calc.eval", False)
    with pytest.raises(CommandDisabled):
        registry.execute("calc.eval")
    assert all(c.id != "calc.eval" for c in registry.search("calc"))
    assert any(c.id == "calc.eval" for c in registry.search("calc", include_disabled=True))
    registry.close()


def test_unregister_and_categories(tmp_path: Path) -> None:
    registry = CommandRegistry(tmp_path / "q.db")
    _add_commands(registry)
    assert set(registry.categories()) >= {"数学", "文本", "编码", "时间"}
    assert registry.unregister("time.record")
    assert registry.get("time.record") is None
    assert registry.unregister_source("builtin") == 3
    assert registry.count() == 0
    registry.close()
