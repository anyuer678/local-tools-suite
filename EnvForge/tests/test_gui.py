from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialogButtonBox, QTableWidget  # noqa: E402

from envforge.library import get as get_preset  # noqa: E402
from envforge.models import Finding  # noqa: E402
from envforge.ui.qt_app import MainWindow, _PresetDialog, _fill, _fmt_ts, _table  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(qapp, db, monkeypatch, tmp_path):
    monkeypatch.setenv("ENVFORGE_DATA_DIR", str(tmp_path / "data"))
    win = MainWindow(db)
    yield win
    win.close()


def test_main_window_has_five_tabs(window):
    assert window.tabs.count() == 5
    titles = [window.tabs.tabText(i) for i in range(5)]
    assert titles == ["运行时版本", "项目与档案", "变量库", "系统环境", "健康体检"]


def test_window_lists_projects_and_profiles(window, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    window.projects.add("demo", str(project))
    window.profiles.upsert_profile("demo", "dev")
    window._refresh_projects()
    assert window.project_list.count() == 1
    assert window.profile_combo.count() == 1
    window.profile_combo.setCurrentIndex(0)
    assert window.profile_combo.currentData() == "dev"


def test_setting_secret_masks_in_table(window, tmp_path):
    project = tmp_path / "proj2"
    project.mkdir()
    window.projects.add("demo2", str(project))
    window.profiles.set_var("demo2", "dev", "DB_PASSWORD", "secret")
    window._refresh_projects()
    window.profile_combo.setCurrentIndex(0)
    assert window.var_table.rowCount() == 1
    assert window.var_table.item(0, 1).text() == "***"
    assert "已存储" in window.var_table.item(0, 3).text()


def test_doctor_results_rendered(window):
    findings = [
        Finding("x", "error", "问题甲", "详情甲", "建议甲"),
        Finding("y", "warn", "问题乙", "详情乙", "建议乙"),
    ]
    window._on_doctor(findings)
    assert window.doctor_table.rowCount() == 2
    assert window.doctor_table.item(0, 0).text() == "错误"
    assert "错误 1" in window.doctor_summary.text()
    window._on_doctor([])
    assert "未发现问题" in window.doctor_summary.text()


def test_scope_switch_updates_write_hint(window):
    window.scope_combo.setCurrentIndex(1)  # system
    assert "只读" in window.env_write_hint.text()


def test_table_helpers_do_not_leak_selection():
    table = _table(["ID", "名称"])
    _fill(table, [[1, "a"], [2, "b"]])
    assert table.rowCount() == 2
    assert table.item(0, 0).data(0x0100) == 1  # Qt.UserRole
    assert isinstance(table, QTableWidget)


def test_snapshot_table_populated(window):
    window.env.create("session", "unit")
    window._refresh_snapshots()
    assert window.snapshot_table.rowCount() >= 1


def test_fmt_ts_is_readable_and_local():
    formatted = _fmt_ts("2026-10-30T05:32:28+00:00")
    assert len(formatted) == 16 and "T" not in formatted
    assert _fmt_ts(None) == "-"
    assert _fmt_ts("not-a-time") == "not-a-time"


def test_library_tab_lists_full_catalog(window):
    assert window.library_table.rowCount() >= 100
    assert "共" in window.library_count.text()
    assert window.library_table.item(0, 4).text() in ("敏感", "普通")


def test_library_filter_by_keyword_and_category(window):
    window.library_search.setText("JAVA_HOME")
    assert window.library_table.rowCount() >= 1
    assert window.library_table.item(0, 0).text() == "JAVA_HOME"

    window.library_search.clear()
    index = window.library_category.findData("代理与网络")
    window.library_category.setCurrentIndex(index)
    assert window.library_table.rowCount() >= 1
    for row in range(window.library_table.rowCount()):
        assert window.library_table.item(row, 1).text() == "代理与网络"


def test_preset_dialog_requires_existing_profile(window):
    preset = get_preset("JAVA_HOME")
    dialog = _PresetDialog(preset, ["demo"], lambda _name: [], window)
    assert dialog.profile.count() == 0
    assert not dialog.buttons.button(QDialogButtonBox.Ok).isEnabled()
