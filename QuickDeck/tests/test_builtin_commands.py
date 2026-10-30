from __future__ import annotations

from pathlib import Path

import pytest

from quickdeck.builtin_actions import BuiltinToolkit, CalculatorError
from quickdeck.clipboard_service import MemoryClipboardBackend
from quickdeck.command_registry import CommandRegistry


def _toolkit(tmp_path: Path) -> tuple[BuiltinToolkit, CommandRegistry]:
    toolkit = BuiltinToolkit(
        tmp_path / "q.db",
        clipboard_backend=MemoryClipboardBackend(),
    )
    registry = CommandRegistry(tmp_path / "q.db")
    toolkit.register_commands(registry)
    return toolkit, registry


def test_all_builtins_registered_and_searchable(tmp_path: Path) -> None:
    toolkit, registry = _toolkit(tmp_path)
    assert registry.count() >= 20
    assert registry.search("url")[0].id.startswith("encoding.url_")
    assert registry.search("计算")[0].id == "calc.eval"
    assert registry.search("json")[0].id.startswith("encoding.json")
    assert {c.category for c in registry.all()} >= {"数学", "文本", "编码", "换算", "时间", "系统", "剪贴板"}
    registry.close()
    toolkit.close()


def test_builtin_tools_through_registry(tmp_path: Path) -> None:
    toolkit, registry = _toolkit(tmp_path)
    assert registry.execute("calc.eval", "2+2") == "4"
    with pytest.raises(CalculatorError):
        registry.execute("calc.eval", "__import__('os')")
    assert registry.execute("text.case", "abc", {"mode": "upper"}) == "ABC"
    assert registry.execute("encoding.base64_encode", "hi") == "aGk="
    assert registry.execute(
        "unit.convert",
        params={"value": 1, "from_unit": "km", "to_unit": "m"},
    ) == "1000 m"
    assert registry.execute("encoding.json_validate", '{"a":1}').startswith("JSON 有效")

    recorded = registry.execute("time.record", "阅读")
    assert "已记录" in recorded
    history = registry.execute("time.history")
    assert "阅读" in history

    assert "未启用" in registry.execute("clipboard.history")
    assert "已清空" in registry.execute("clipboard.clear")
    registry.close()
    toolkit.close()


def test_clipboard_builtin_when_enabled(tmp_path: Path) -> None:
    toolkit, registry = _toolkit(tmp_path)
    toolkit.clipboard.set_enabled(True)
    backend = toolkit.clipboard.backend
    backend.set_text("clip-item")
    toolkit.clipboard.capture_once()
    assert "clip-item" in registry.execute("clipboard.history")
    registry.close()
    toolkit.close()
