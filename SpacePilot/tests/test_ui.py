"""GUI 冒烟测试：离屏模式下构建主窗口，验证目录树与标签页。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from spacepilot.scanner import Scanner
from spacepilot.snapshot_store import SnapshotStore
from spacepilot.ui.qt_app import MainWindow


def write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def test_main_window_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    root = tmp_path / "root"
    root.mkdir()
    write_bytes(root / "a.txt", b"hello" * 100)
    write_bytes(root / "sub" / "b.bin", b"x" * 1000)

    db = tmp_path / "gui.db"
    store = SnapshotStore(db)
    Scanner(root, store).scan()
    store.close()

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = MainWindow(str(db))
    assert window.tabs.count() == 6
    assert window.watch_button.isEnabled()
    assert window.dir_tree.topLevelItemCount() == 1
    assert "文件" in window.summary_label.text()
    window.close()
