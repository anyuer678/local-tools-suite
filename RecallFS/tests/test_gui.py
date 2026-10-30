from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from helpers import write_file  # noqa: E402

from recallfs import __main__ as entry  # noqa: E402


def test_fallback_to_cli_when_gui_unavailable(monkeypatch, capsys):
    def missing():
        raise ImportError("PySide6 未安装")

    monkeypatch.setattr(entry, "_load_gui", missing)
    assert entry.main([]) == 0
    captured = capsys.readouterr()
    assert "回退" in captured.err
    assert "usage: recallfs" in captured.out
    assert entry.main(["gui"]) == 0
    assert "usage: recallfs" in capsys.readouterr().out


def test_gui_smoke_offscreen(tmp_path, corpus):
    pytest.importorskip("PySide6.QtWidgets")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from recallfs.ui.qt_app import MainWindow

    write_file(corpus, "gui.md", "界面检索关键词 内容\n")
    window = MainWindow(str(tmp_path / "gui.sqlite3"))
    window.services.scheduler.scan([str(corpus)])
    window.search_input.setText("界面检索关键词")
    window.run_search()
    assert window.results_table.rowCount() >= 1
    window.results_table.selectRow(0)
    window._show_selected_hit()
    detail = window.detail.toPlainText()
    assert "gui.md" in detail or "界面检索关键词" in detail
    window.close()
    app.processEvents()
