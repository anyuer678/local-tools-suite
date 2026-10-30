from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from quickdeck.builtin_actions import ActivityStore, ToolError
from quickdeck.clipboard_service import (
    ClipboardError,
    ClipboardHistoryService,
    MemoryClipboardBackend,
)


def test_clipboard_disabled_by_default() -> None:
    backend = MemoryClipboardBackend("hello")
    service = ClipboardHistoryService(backend=backend, max_items=3)
    assert service.enabled is False
    assert service.capture_once() is None
    assert service.history() == []


def test_clipboard_capture_capacity_exclusion_and_clear() -> None:
    backend = MemoryClipboardBackend()
    service = ClipboardHistoryService(
        backend=backend,
        enabled=True,
        max_items=3,
        exclude_patterns=[r"password|secret"],
    )
    backend.set_text("my secret token")
    assert service.capture_once() is None

    backend.set_text("hello")
    entry = service.capture_once()
    assert entry is not None
    assert entry.text == "hello"
    assert service.capture_once() is None

    backend.set_text("world")
    service.capture_once()
    for text in ("a", "b", "c"):
        backend.set_text(text)
        service.capture_once()
    entries = service.history()
    assert [entry.text for entry in entries] == ["c", "b", "a"]
    assert len(entries) == 3

    assert service.clear() == 3
    assert service.history() == []


def test_clipboard_set_text_and_toggle() -> None:
    backend = MemoryClipboardBackend()
    service = ClipboardHistoryService(backend=backend, enabled=True, max_items=5)
    service.set_text("value")
    assert backend.get_text() == "value"
    assert service.history()[0].text == "value"
    service.set_enabled(False)
    assert service.capture_once() is None
    service.set_text("other")
    assert backend.get_text() == "other"
    assert service.history()[0].text == "value"


def test_clipboard_persistence(tmp_path: Path) -> None:
    db = tmp_path / "q.db"
    backend = MemoryClipboardBackend()
    service = ClipboardHistoryService(backend=backend, enabled=True, db_path=db, max_items=3)
    service.set_text("persisted")
    service.close()
    assert db.exists()

    service2 = ClipboardHistoryService(backend=backend, enabled=False, db_path=db, max_items=3)
    assert [entry.text for entry in service2.history()] == ["persisted"]
    service2.close()


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"max_items": 0}, "上限"),
        ({"exclude_patterns": ["["]}, "正则无效"),
    ],
)
def test_clipboard_config_errors(kwargs, match) -> None:
    with pytest.raises(ClipboardError, match=match):
        ClipboardHistoryService(backend=MemoryClipboardBackend(), **kwargs)


def test_activity_store_records_and_history(tmp_path: Path) -> None:
    store = ActivityStore(tmp_path / "q.db")
    fixed = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc)
    first = store.record("阅读", "第一章", now=fixed)
    assert first.name == "阅读"
    assert first.recorded_at == "2026-01-02T03:04:05+00:00"
    second = store.record("写作", now=fixed + dt.timedelta(seconds=3))
    assert store.last().name == "写作"  # type: ignore[union-attr]
    assert store.last("阅读").id == first.id  # type: ignore[union-attr]
    history = store.history(limit=10)
    assert [record.name for record in history] == ["写作", "阅读"]
    assert store.history("阅读", limit=1)[0].note == "第一章"
    assert set(store.names()) == {"阅读", "写作"}
    assert store.clear("阅读") == 1
    assert store.history("阅读") == []
    store.close()


def test_activity_rejects_empty_name(tmp_path: Path) -> None:
    store = ActivityStore(tmp_path / "q.db")
    with pytest.raises(ToolError, match="不能为空"):
        store.record("   ")
    store.close()
