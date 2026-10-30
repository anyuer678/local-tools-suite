"""PySide6 桌面界面（仅在启动 GUI 时导入）。

五个功能区：运行时版本、项目环境档案、常用变量库、系统环境与 PATH、健康体检。
所有耗时操作（PATH 探测、体检）在独立线程执行，主线程不阻塞。
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..audit import AuditLog, install_crash_hook
from ..db import Database, DatabaseError, default_data_dir, default_db_path
from ..doctor import Doctor, summarize
from ..envstore import EnvService
from ..envvars import SCOPES, dedupe_path, scope_label
from ..errors import EnvForgeError
from ..library import CATEGORIES, all_presets, get as get_preset, search as search_presets
from ..models import RUNTIME_KINDS, looks_sensitive_key
from ..profiles import ProfileService, ProjectService
from ..runtimes import RuntimeService
from ..secrets import REDACTED

_STYLE = """
QWidget { background: #f7f9fc; color: #1f2933; font-family: "Microsoft YaHei UI"; font-size: 13px; }
QTabWidget::pane { border: 1px solid #dfe6ee; background: #ffffff; border-radius: 6px; }
QTabBar::tab { padding: 7px 18px; background: #eef3f8; border: 1px solid #dfe6ee;
               border-bottom: none; border-top-left-radius: 6px; border-top-right-radius: 6px;
               margin-right: 2px; color: #52606d; }
QTabBar::tab:selected { background: #ffffff; color: #1668dc; font-weight: 600; }
QTableWidget { background: #ffffff; border: 1px solid #e3e9f0; gridline-color: #eef2f6;
               selection-background-color: #e6f1ff; selection-color: #1f2933; }
QHeaderView::section { background: #f2f6fb; padding: 6px; border: none;
                       border-bottom: 1px solid #e3e9f0; color: #52606d; font-weight: 600; }
QPushButton { background: #1668dc; color: #ffffff; border: none; border-radius: 4px;
              padding: 6px 14px; }
QPushButton:hover { background: #3c86e0; }
QPushButton:disabled { background: #c9d3df; color: #ffffff; }
QPushButton#secondary { background: #ffffff; color: #1668dc; border: 1px solid #b7cdf0; }
QPushButton#secondary:hover { background: #eef5ff; }
QPushButton#danger { background: #d4380d; }
QPushButton#danger:hover { background: #e35b33; }
QLineEdit, QComboBox { background: #ffffff; border: 1px solid #d5dee8; border-radius: 4px;
                       padding: 4px 6px; }
QListWidget { background: #ffffff; border: 1px solid #e3e9f0; border-radius: 4px; }
QPlainTextEdit { background: #ffffff; border: 1px solid #e3e9f0; border-radius: 4px;
                 font-family: Consolas, "Microsoft YaHei UI"; }
QLabel#hint { color: #7b8794; }
QLabel#title { font-size: 15px; font-weight: 600; color: #1f2933; }
"""


def _confirm(
    parent: QWidget,
    title: str,
    text: str,
    informative: str = "",
    *,
    yes_text: str = "确认",
    danger: bool = False,
) -> bool:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(QMessageBox.Warning if danger else QMessageBox.Question)
    box.setText(text)
    if informative:
        box.setInformativeText(informative)
    box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
    box.setDefaultButton(QMessageBox.No)
    box.button(QMessageBox.Yes).setText(yes_text)
    box.button(QMessageBox.No).setText("取消")
    return box.exec() == QMessageBox.Yes


def _table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    table.setAlternatingRowColors(True)
    return table


def _fill(table: QTableWidget, rows: list[list[Any]]) -> None:
    table.setRowCount(0)
    for row in rows:
        index = table.rowCount()
        table.insertRow(index)
        for column, value in enumerate(row):
            text = "" if value is None else str(value)
            item = QTableWidgetItem(text)
            item.setToolTip(text)
            if column == 0:
                item.setData(Qt.UserRole, value)
            table.setItem(index, column, item)


def _fmt_ts(value: str | None) -> str:
    """把 UTC ISO 时间转换为本地时区的可读格式。"""
    if not value:
        return "-"
    try:
        from datetime import datetime

        return datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return value


def _selected_id(table: QTableWidget) -> Any:
    row = table.currentRow()
    if row < 0:
        return None
    item = table.item(row, 0)
    return item.data(Qt.UserRole) if item else None


class _Worker(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, func, parent=None) -> None:
        super().__init__(parent)
        self._func = func

    def run(self) -> None:
        try:
            self.succeeded.emit(self._func())
        except Exception:
            self.failed.emit(traceback.format_exc())


class _TextDialog(QDialog):
    def __init__(self, title: str, text: str, parent: QWidget | None = None, note: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(820, 520)
        layout = QVBoxLayout(self)
        if note:
            label = QLabel(note)
            label.setObjectName("hint")
            label.setWordWrap(True)
            layout.addWidget(label)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setPlainText(text)
        layout.addWidget(self.text)
        buttons = QDialogButtonBox()
        copy_button = buttons.addButton("复制到剪贴板", QDialogButtonBox.ActionRole)
        close_button = buttons.addButton("关闭", QDialogButtonBox.AcceptRole)
        copy_button.clicked.connect(self._copy)
        close_button.clicked.connect(self.accept)
        layout.addWidget(buttons)

    def _copy(self) -> None:
        QApplication.clipboard().setText(self.text.toPlainText())


class _ProjectDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("添加项目")
        self.resize(560, 180)
        layout = QFormLayout(self)
        self.name = QLineEdit()
        self.name.setPlaceholderText("例如 demo")
        self.path = QLineEdit()
        self.path.setPlaceholderText("项目目录，例如 D:\\work\\demo")
        browse = QPushButton("浏览…")
        browse.setObjectName("secondary")
        browse.clicked.connect(self._browse)
        path_row = QHBoxLayout()
        path_row.addWidget(self.path)
        path_row.addWidget(browse)
        layout.addRow("项目名称", self.name)
        layout.addRow("项目路径", path_row)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("添加")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _browse(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择项目目录")
        if directory:
            self.path.setText(directory)
            if not self.name.text():
                self.name.setText(Path(directory).name)

    def values(self) -> tuple[str, str]:
        name = self.name.text().strip() or Path(self.path.text().strip()).name
        return name, self.path.text().strip()


class _VarDialog(QDialog):
    def __init__(self, parent: QWidget | None = None, *, key: str = "", value: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("设置环境变量")
        self.resize(560, 200)
        layout = QFormLayout(self)
        self.key = QLineEdit(key)
        self.key.setPlaceholderText("例如 DB_PASSWORD")
        self.value = QLineEdit(value)
        self.value.setPlaceholderText("变量值")
        self.sensitive = QCheckBox("按敏感值加密存储（含 TOKEN/PASSWORD 等名称时自动勾选）")
        self.sensitive.setChecked(bool(key and looks_sensitive_key(key)))
        self.key.textChanged.connect(self._autodetect)
        layout.addRow("变量名", self.key)
        layout.addRow("变量值", self.value)
        layout.addRow("", self.sensitive)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def _autodetect(self, text: str) -> None:
        if looks_sensitive_key(text):
            self.sensitive.setChecked(True)

    def values(self) -> tuple[str, str, bool]:
        return self.key.text().strip(), self.value.text(), self.sensitive.isChecked()


class _PresetDialog(QDialog):
    """把变量库中的预设加入某个项目档案。"""

    def __init__(self, preset, projects: list[str], profiles_for, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.preset = preset
        self._profiles_for = profiles_for
        self.setWindowTitle(f"加入档案 · {preset.key}")
        self.resize(600, 320)
        form = QFormLayout(self)

        key = QLabel(preset.key)
        key.setObjectName("title")
        form.addRow("变量", key)
        description = QLabel(preset.description)
        description.setWordWrap(True)
        form.addRow("说明", description)

        self.project = QComboBox()
        self.project.addItems(projects)
        self.project.currentTextChanged.connect(self._reload)
        form.addRow("项目", self.project)

        self.profile = QComboBox()
        form.addRow("档案", self.profile)

        self.value = QLineEdit(preset.example)
        form.addRow("变量值", self.value)

        self.sensitive = QCheckBox("按敏感值加密存储")
        self.sensitive.setChecked(preset.sensitive)
        if preset.sensitive:
            self.sensitive.setEnabled(False)
            self.sensitive.setToolTip("变量名命中敏感规则，强制加密存储。")
        form.addRow("", self.sensitive)

        self.hint = QLabel("该项目还没有档案，请先在「项目与档案」新建（如 dev / test / prod）。")
        self.hint.setObjectName("hint")
        self.hint.setWordWrap(True)
        form.addRow("", self.hint)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.button(QDialogButtonBox.Ok).setText("加入档案")
        self.buttons.button(QDialogButtonBox.Cancel).setText("取消")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        form.addRow(self.buttons)
        self._reload()

    def _reload(self, *_args) -> None:
        self.profile.clear()
        project = self.project.currentText()
        if project:
            for name in self._profiles_for(project):
                self.profile.addItem(name)
        has_profile = self.profile.count() > 0
        self.hint.setVisible(not has_profile)
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(has_profile)

    def choice(self) -> tuple[str, str, str, bool]:
        return (
            self.project.currentText(),
            self.profile.currentText(),
            self.value.text(),
            self.sensitive.isChecked(),
        )


class MainWindow(QMainWindow):
    def __init__(self, db: Database, audit: AuditLog | None = None) -> None:
        super().__init__()
        self.db = db
        self.audit = audit
        self.projects = ProjectService(db)
        self.profiles = ProfileService(db, self.projects, audit=audit)
        self.runtimes = RuntimeService(db)
        self.env = EnvService(db, audit=audit)
        self.doctor = Doctor(db, self.env, self.runtimes, self.projects)
        self._workers: list[_Worker] = []
        self._reveal_secrets = False
        self._reveal_env = False

        self.setWindowTitle(f"EnvForge 环境工坊 v{__version__}")
        self.resize(1120, 720)
        self.setMinimumSize(1000, 640)
        self.setStyleSheet(_STYLE)

        central = QWidget()
        layout = QVBoxLayout(central)
        header = QHBoxLayout()
        title = QLabel("EnvForge · 运行时版本 / 项目环境档案 / 变量库 / 系统环境")
        title.setObjectName("title")
        header.addWidget(title)
        header.addStretch(1)
        hint = QLabel("本地运行 · 不上传任何数据")
        hint.setObjectName("hint")
        hint.setToolTip(f"数据目录：{default_data_dir()}")
        header.addWidget(hint)
        layout.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_runtime_tab(), "运行时版本")
        self.tabs.addTab(self._build_profile_tab(), "项目与档案")
        self.tabs.addTab(self._build_library_tab(), "变量库")
        self.tabs.addTab(self._build_env_tab(), "系统环境")
        self.tabs.addTab(self._build_doctor_tab(), "健康体检")
        layout.addWidget(self.tabs)

        self.setCentralWidget(central)
        self.statusBar().showMessage(f"数据库：{db.path}")
        self._refresh_all()

    # ------------------------------------------------------------------ 通用
    def _run_worker(self, func, on_success) -> None:
        worker = _Worker(func, self)
        worker.succeeded.connect(on_success)
        worker.failed.connect(self._show_worker_error)
        worker.finished.connect(lambda: self._workers.remove(worker) if worker in self._workers else None)
        self._workers.append(worker)
        worker.start()

    def _show_worker_error(self, text: str) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("后台任务失败")
        box.setIcon(QMessageBox.Critical)
        box.setText("任务执行失败，界面状态未改变。")
        box.setDetailedText(text)
        box.setStandardButtons(QMessageBox.Ok)
        box.button(QMessageBox.Ok).setText("知道了")
        box.exec()

    def _error(self, exc: Exception) -> None:
        if isinstance(exc, EnvForgeError):
            text = str(exc)
        else:
            text = f"{exc}"
        QMessageBox.warning(self, "操作未完成", text)

    def _refresh_all(self) -> None:
        self._refresh_runtimes()
        self._refresh_projects()
        self._refresh_profiles()
        self._refresh_env()
        self._refresh_snapshots()

    # ------------------------------------------------------------------ 运行时
    def _build_runtime_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        bar = QHBoxLayout()
        self.runtime_status = QLabel("尚未登记运行时。扫描 PATH 可自动发现已安装的 Python / Node / JDK。")
        self.runtime_status.setObjectName("hint")
        bar.addWidget(self.runtime_status, 1)
        detect = QPushButton("扫描 PATH 并登记")
        detect.clicked.connect(self._detect_runtimes)
        add = QPushButton("手动登记…")
        add.setObjectName("secondary")
        add.clicked.connect(self._add_runtime)
        remove = QPushButton("移除所选")
        remove.setObjectName("danger")
        remove.clicked.connect(self._remove_runtime)
        script = QPushButton("生成激活脚本")
        script.setObjectName("secondary")
        script.clicked.connect(self._runtime_script)
        for button in (detect, add, remove, script):
            bar.addWidget(button)
        layout.addLayout(bar)

        self.runtime_table = _table(["ID", "类型", "版本", "可执行文件", "来源", "登记时间"])
        runtime_header = self.runtime_table.horizontalHeader()
        runtime_header.setSectionResizeMode(3, QHeaderView.Stretch)
        self.runtime_table.doubleClicked.connect(lambda _index: self._runtime_script())
        layout.addWidget(self.runtime_table, 1)
        note = QLabel(
            "说明：激活脚本只把所选运行时目录前置到当前终端会话的 PATH，不会修改系统环境变量；"
            "默认每个类型使用已登记的最高版本。"
        )
        note.setObjectName("hint")
        note.setWordWrap(True)
        layout.addWidget(note)
        return page

    def _refresh_runtimes(self) -> None:
        entries = self.runtimes.list()
        _fill(
            self.runtime_table,
            [
                [
                    entry.id,
                    entry.kind,
                    entry.version or "未知",
                    entry.exe_path,
                    entry.source,
                    _fmt_ts(entry.created_at),
                ]
                for entry in entries
            ],
        )
        defaults = {entry.kind: entry for entry in self.runtimes.default_entries()}
        if entries:
            summary = "，".join(f"{kind} {entry.version or '未知'}" for kind, entry in defaults.items())
            self.runtime_status.setText(f"已登记 {len(entries)} 条；激活默认：{summary}")
        else:
            self.runtime_status.setText("尚未登记运行时。扫描 PATH 可自动发现已安装的 Python / Node / JDK。")

    def _detect_runtimes(self) -> None:
        self.runtime_status.setText("正在扫描 PATH，请稍候…")
        self._run_worker(self.runtimes.detect_and_register, self._on_detected)

    def _on_detected(self, result: dict[str, Any]) -> None:
        self._refresh_runtimes()
        detected = result.get("detected", [])
        if not detected:
            QMessageBox.information(
                self,
                "扫描完成",
                "未在 PATH 中发现可登记的运行时。可手动登记 python.exe / node.exe / java.exe。",
            )
        else:
            self.statusBar().showMessage(
                f"新增 {result['added']} 条，更新 {result['updated']} 条。", 5000
            )

    def _add_runtime(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("手动登记运行时")
        dialog.resize(560, 200)
        form = QFormLayout(dialog)
        kind = QComboBox()
        kind.addItems(list(RUNTIME_KINDS))
        path = QLineEdit()
        path.setPlaceholderText("例如 C:\\Python312\\python.exe")
        browse = QPushButton("浏览…")
        browse.setObjectName("secondary")
        browse.clicked.connect(
            lambda: path.setText(
                QFileDialog.getOpenFileName(
                    dialog, "选择可执行文件", "", "可执行文件 (*.exe);;所有文件 (*.*)"
                )[0]
            )
        )
        row = QHBoxLayout()
        row.addWidget(path)
        row.addWidget(browse)
        version = QLineEdit()
        version.setPlaceholderText("留空则自动探测")
        form.addRow("类型", kind)
        form.addRow("可执行文件", row)
        form.addRow("版本", version)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("登记")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            entry = self.runtimes.register(
                kind.currentText(),
                path.text().strip(),
                version.text().strip() or None,
            )
        except Exception as exc:
            self._error(exc)
            return
        self._refresh_runtimes()
        self.statusBar().showMessage(f"已登记 {entry.kind} {entry.version or '未知'}", 5000)

    def _remove_runtime(self) -> None:
        runtime_id = _selected_id(self.runtime_table)
        if runtime_id is None:
            QMessageBox.information(self, "未选择", "请先在表格中选择一条运行时记录。")
            return
        row = self.runtime_table.currentRow()
        label = self.runtime_table.item(row, 1).text() + " " + self.runtime_table.item(row, 2).text()
        if not _confirm(
            self,
            "移除运行时记录",
            f"确定移除 {label} 的登记记录吗？",
            "只删除 EnvForge 中的记录，不会卸载该软件或删除文件。",
            danger=True,
        ):
            return
        try:
            self.runtimes.remove(int(runtime_id))
        except Exception as exc:
            self._error(exc)
            return
        self._refresh_runtimes()

    def _runtime_script(self) -> None:
        entries = self.runtimes.default_entries()
        if not entries:
            QMessageBox.information(self, "暂无可用运行时", "请先扫描或手动登记运行时。")
            return
        script = self.runtimes.activation_script()
        _TextDialog(
            "运行时激活脚本",
            script,
            self,
            note="在 PowerShell 中执行以下内容即可让当前终端优先使用这些版本；关闭终端后失效。",
        ).exec()

    # ------------------------------------------------------------------ 项目档案
    def _build_profile_tab(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        splitter = QSplitter(Qt.Horizontal)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel("项目"))
        self.project_list = QListWidget()
        self.project_list.currentItemChanged.connect(lambda *_: self._refresh_profiles())
        left_layout.addWidget(self.project_list, 1)
        project_bar = QHBoxLayout()
        add = QPushButton("添加项目")
        add.clicked.connect(self._add_project)
        remove = QPushButton("删除项目")
        remove.setObjectName("danger")
        remove.clicked.connect(self._remove_project)
        project_bar.addWidget(add)
        project_bar.addWidget(remove)
        left_layout.addLayout(project_bar)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        profile_bar = QHBoxLayout()
        profile_bar.addWidget(QLabel("档案"))
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(180)
        self.profile_combo.currentIndexChanged.connect(lambda _i: self._refresh_profile_vars())
        profile_bar.addWidget(self.profile_combo)
        new_profile = QPushButton("新建档案")
        new_profile.setObjectName("secondary")
        new_profile.clicked.connect(self._new_profile)
        remove_profile = QPushButton("删除档案")
        remove_profile.setObjectName("danger")
        remove_profile.clicked.connect(self._delete_profile)
        profile_bar.addWidget(new_profile)
        profile_bar.addWidget(remove_profile)
        profile_bar.addStretch(1)
        right_layout.addLayout(profile_bar)
        self.profile_hint = QLabel("敏感值以 DPAPI 密文存储，默认只显示 ***。")
        self.profile_hint.setObjectName("hint")
        right_layout.addWidget(self.profile_hint)

        self.var_table = _table(["变量名", "值", "类型", "存储状态"])
        self.var_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        right_layout.addWidget(self.var_table, 1)

        actions = QHBoxLayout()
        set_var = QPushButton("设置变量…")
        set_var.clicked.connect(self._set_var)
        unset_var = QPushButton("删除变量")
        unset_var.setObjectName("danger")
        unset_var.clicked.connect(self._unset_var)
        self.reveal_check = QCheckBox("显示敏感值")
        self.reveal_check.toggled.connect(self._toggle_reveal)
        export = QPushButton("导出 .env")
        export.setObjectName("secondary")
        export.clicked.connect(self._export_profile)
        activate = QPushButton("生成激活脚本")
        activate.setObjectName("secondary")
        activate.clicked.connect(self._activate_profile)
        for widget in (set_var, unset_var, self.reveal_check, export, activate):
            actions.addWidget(widget)
        actions.addStretch(1)
        right_layout.addLayout(actions)
        splitter.addWidget(right)
        splitter.setSizes([300, 800])
        outer.addWidget(splitter, 1)
        return page

    def _current_project(self) -> str | None:
        item = self.project_list.currentItem()
        return item.data(Qt.UserRole) if item else None

    def _refresh_projects(self) -> None:
        current = self._current_project()
        self.project_list.blockSignals(True)
        self.project_list.clear()
        for project in self.projects.list():
            from PySide6.QtWidgets import QListWidgetItem

            item = QListWidgetItem(project.name)
            item.setData(Qt.UserRole, project.name)
            item.setToolTip(project.path)
            self.project_list.addItem(item)
            if project.name == current:
                self.project_list.setCurrentItem(item)
        self.project_list.blockSignals(False)
        if self.project_list.count() and self.project_list.currentRow() < 0:
            self.project_list.setCurrentRow(0)
        self._refresh_profiles()

    def _refresh_profiles(self) -> None:
        project = self._current_project()
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        if project:
            try:
                for profile in self.profiles.list_profiles(project):
                    label = profile.name + ("（当前）" if profile.active else "")
                    self.profile_combo.addItem(label, profile.name)
            except EnvForgeError:
                pass
        self.profile_combo.blockSignals(False)
        self._refresh_profile_vars()

    def _refresh_profile_vars(self) -> None:
        project = self._current_project()
        profile = self.profile_combo.currentData()
        self.var_table.setRowCount(0)
        if not project or not profile:
            self.profile_hint.setText("请先添加项目并创建档案（如 dev / test / prod）。")
            return
        try:
            variables = self.profiles.list_vars(project, profile, reveal=self._reveal_secrets)
        except EnvForgeError as exc:
            self.profile_hint.setText(str(exc))
            return
        _fill(
            self.var_table,
            [
                [
                    var.key,
                    var.value,
                    "敏感（加密）" if var.sensitive and var.encrypted else ("敏感" if var.sensitive else "普通"),
                    "已存储" if var.stored else "未存储",
                ]
                for var in variables
            ],
        )
        self.profile_hint.setText(
            f"项目 {project} / 档案 {profile}：{len(variables)} 个变量；敏感值默认脱敏显示。"
        )

    def _add_project(self) -> None:
        dialog = _ProjectDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        name, path = dialog.values()
        if not name or not path:
            QMessageBox.information(self, "信息不完整", "项目名称与路径都不能为空。")
            return
        try:
            entry = self.projects.add(name, path)
        except Exception as exc:
            self._error(exc)
            return
        self._refresh_projects()
        for row in range(self.project_list.count()):
            item = self.project_list.item(row)
            if item.data(Qt.UserRole) == entry.name:
                self.project_list.setCurrentRow(row)
                break

    def _remove_project(self) -> None:
        project = self._current_project()
        if not project:
            QMessageBox.information(self, "未选择", "请先选择一个项目。")
            return
        if not _confirm(
            self,
            "删除项目",
            f"删除项目 {project} 会同时删除它的全部环境档案与变量，且不可撤销。",
            "项目目录中的文件不会被删除。",
            danger=True,
            yes_text="删除",
        ):
            return
        try:
            self.projects.remove(project)
        except Exception as exc:
            self._error(exc)
            return
        self._refresh_projects()

    def _new_profile(self) -> None:
        project = self._current_project()
        if not project:
            QMessageBox.information(self, "未选择", "请先添加并选择一个项目。")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("新建环境档案")
        dialog.resize(480, 160)
        form = QFormLayout(dialog)
        name = QLineEdit()
        name.setPlaceholderText("例如 dev / test / prod")
        description = QLineEdit()
        description.setPlaceholderText("说明（可选）")
        form.addRow("档案名称", name)
        form.addRow("说明", description)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("创建")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.Accepted or not name.text().strip():
            return
        try:
            self.profiles.upsert_profile(project, name.text().strip(), description.text().strip())
        except Exception as exc:
            self._error(exc)
            return
        self._refresh_profiles()
        index = self.profile_combo.findData(name.text().strip())
        if index >= 0:
            self.profile_combo.setCurrentIndex(index)

    def _delete_profile(self) -> None:
        project = self._current_project()
        profile = self.profile_combo.currentData()
        if not project or not profile:
            QMessageBox.information(self, "未选择", "请先选择项目与档案。")
            return
        if not _confirm(
            self,
            "删除档案",
            f"删除档案 {profile} 会丢失其中全部变量，且不可撤销。",
            danger=True,
            yes_text="删除",
        ):
            return
        try:
            self.profiles.remove_profile(project, profile)
        except Exception as exc:
            self._error(exc)
            return
        self._refresh_profiles()

    def _set_var(self) -> None:
        project = self._current_project()
        profile = self.profile_combo.currentData()
        if not project or not profile:
            QMessageBox.information(self, "未选择", "请先选择项目与档案。")
            return
        dialog = _VarDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        key, value, sensitive = dialog.values()
        try:
            _var, warning = self.profiles.set_var(project, profile, key, value, sensitive)
        except Exception as exc:
            self._error(exc)
            return
        self._refresh_profile_vars()
        if warning:
            QMessageBox.warning(self, "敏感值未保存", warning)

    def _unset_var(self) -> None:
        project = self._current_project()
        profile = self.profile_combo.currentData()
        row = self.var_table.currentRow()
        if not project or not profile or row < 0:
            QMessageBox.information(self, "未选择", "请先选择要删除的变量。")
            return
        key = self.var_table.item(row, 0).text()
        if not _confirm(self, "删除变量", f"确定删除变量 {key} 吗？", danger=True, yes_text="删除"):
            return
        try:
            self.profiles.unset_var(project, profile, key)
        except Exception as exc:
            self._error(exc)
            return
        self._refresh_profile_vars()

    def _toggle_reveal(self, checked: bool) -> None:
        if checked and not _confirm(
            self,
            "显示敏感值",
            "即将在界面中显示已加密的敏感值明文。",
            "请确认屏幕周围没有无关人员；显示结果不会写入日志。",
            yes_text="显示",
        ):
            self.reveal_check.blockSignals(True)
            self.reveal_check.setChecked(False)
            self.reveal_check.blockSignals(False)
            self._reveal_secrets = False
            self._refresh_profile_vars()
            return
        self._reveal_secrets = checked
        self._refresh_profile_vars()

    def _export_profile(self) -> None:
        project = self._current_project()
        profile = self.profile_combo.currentData()
        if not project or not profile:
            QMessageBox.information(self, "未选择", "请先选择项目与档案。")
            return
        reveal = self._reveal_secrets and _confirm(
            self,
            "导出明文敏感值",
            "导出的 .env 将包含敏感值明文。",
            "请把文件保存在不会被提交到版本库的位置。",
            danger=True,
            yes_text="导出明文",
        )
        text = self.profiles.export_dotenv(project, profile, reveal=reveal)
        path, _ = QFileDialog.getSaveFileName(self, "导出 .env", f"{project}-{profile}.env", "环境文件 (*.env)")
        if not path:
            return
        Path(path).write_text(text, encoding="utf-8")
        self.statusBar().showMessage(f"已导出到 {path}", 5000)

    def _activate_profile(self) -> None:
        project = self._current_project()
        profile = self.profile_combo.currentData()
        if not project or not profile:
            QMessageBox.information(self, "未选择", "请先选择项目与档案。")
            return
        if not _confirm(
            self,
            "生成激活脚本",
            f"将解密 {project} / {profile} 的敏感值并写入脚本内容。",
            "脚本只在当前终端会话生效，不会修改系统环境变量。",
            yes_text="生成",
        ):
            return
        try:
            env, warnings = self.profiles.activation(project, profile, confirm=True)
        except Exception as exc:
            self._error(exc)
            return
        project_path = env.pop("ENVFORGE_PROJECT", None)
        env.pop("ENVFORGE_PROFILE", None)
        script = self.runtimes.activation_script(project_path=project_path, extra_vars=env)
        note = "脚本包含敏感值明文，请勿提交到版本库。"
        if warnings:
            note += "\n警告：" + "；".join(warnings)
        _TextDialog("激活脚本", script, self, note=note).exec()

    # ------------------------------------------------------------------ 变量库
    def _build_library_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        bar = QHBoxLayout()
        self.library_summary = QLabel(
            f"内置 {len(all_presets())} 个常用环境变量，覆盖 {len(CATEGORIES)} 个分类；"
            "只提供说明与示例，不写入任何真实值。"
        )
        self.library_summary.setObjectName("hint")
        bar.addWidget(self.library_summary, 1)
        layout.addLayout(bar)

        filter_bar = QHBoxLayout()
        self.library_search = QLineEdit()
        self.library_search.setPlaceholderText("搜索变量名、分类或说明…")
        self.library_search.setClearButtonEnabled(True)
        self.library_search.textChanged.connect(lambda _t: self._refresh_library())
        filter_bar.addWidget(self.library_search, 1)
        filter_bar.addWidget(QLabel("分类"))
        self.library_category = QComboBox()
        self.library_category.addItem("全部分类", None)
        for name in CATEGORIES:
            self.library_category.addItem(name, name)
        self.library_category.currentIndexChanged.connect(lambda _i: self._refresh_library())
        filter_bar.addWidget(self.library_category)
        self.library_count = QLabel("")
        self.library_count.setObjectName("hint")
        filter_bar.addWidget(self.library_count)
        layout.addLayout(filter_bar)

        self.library_table = _table(["变量名", "分类", "示例", "说明", "敏感"])
        header = self.library_table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Interactive)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.library_table.setColumnWidth(2, 260)
        self.library_table.doubleClicked.connect(lambda *_: self._show_preset_detail())
        layout.addWidget(self.library_table, 1)

        actions = QHBoxLayout()
        copy_button = QPushButton("复制 KEY=示例")
        copy_button.setObjectName("secondary")
        copy_button.clicked.connect(self._copy_preset)
        add_button = QPushButton("加入档案…")
        add_button.clicked.connect(self._add_preset_to_profile)
        detail = QPushButton("查看说明")
        detail.setObjectName("secondary")
        detail.clicked.connect(self._show_preset_detail)
        actions.addWidget(copy_button)
        actions.addWidget(add_button)
        actions.addWidget(detail)
        actions.addStretch(1)
        hint = QLabel("加入档案时会沿用示例值，请在「项目与档案」中改成真实值；敏感值自动加密。")
        hint.setObjectName("hint")
        actions.addWidget(hint)
        layout.addLayout(actions)
        self._refresh_library()
        return page

    def _refresh_library(self) -> None:
        presets = search_presets(
            category=self.library_category.currentData(), keyword=self.library_search.text()
        )
        _fill(
            self.library_table,
            [
                [
                    item.key,
                    item.category,
                    item.example,
                    item.description,
                    "敏感" if item.sensitive else "普通",
                ]
                for item in presets
            ],
        )
        for row, item in enumerate(presets):
            cell = self.library_table.item(row, 4)
            if cell and item.sensitive:
                cell.setForeground(QColor("#d46b08"))
        self.library_count.setText(f"显示 {len(presets)} / 共 {len(all_presets())} 个")

    def _selected_preset(self):
        row = self.library_table.currentRow()
        if row < 0:
            return None
        cell = self.library_table.item(row, 0)
        return get_preset(cell.text()) if cell else None

    def _copy_preset(self) -> None:
        preset = self._selected_preset()
        if preset is None:
            QMessageBox.information(self, "未选择", "请先在变量库中选择一行。")
            return
        QApplication.clipboard().setText(f"{preset.key}={preset.example}")
        self.statusBar().showMessage(f"已复制 {preset.key} 的示例", 4000)

    def _show_preset_detail(self) -> None:
        preset = self._selected_preset()
        if preset is None:
            QMessageBox.information(self, "未选择", "请先在变量库中选择一行。")
            return
        lines = [
            f"变量：{preset.key}",
            f"分类：{preset.category}",
            f"说明：{preset.description}",
            f"示例：{preset.key}={preset.example}",
            f"敏感：{'是，按密钥处理，默认脱敏' if preset.sensitive else '否'}",
        ]
        if preset.note:
            lines.append(f"注意：{preset.note}")
        _TextDialog(f"{preset.key} · {preset.category}", "\n".join(lines), self).exec()

    def _add_preset_to_profile(self) -> None:
        preset = self._selected_preset()
        if preset is None:
            QMessageBox.information(self, "未选择", "请先在变量库中选择一行。")
            return
        projects = [item.name for item in self.projects.list()]
        if not projects:
            QMessageBox.information(self, "没有项目", "请先在「项目与档案」中登记项目，再为档案添加变量。")
            return
        dialog = _PresetDialog(
            preset,
            projects,
            lambda name: [item.name for item in self.profiles.list_profiles(name)],
            self,
        )
        if dialog.exec() != QDialog.Accepted:
            return
        project, profile, value, sensitive = dialog.choice()
        if not project or not profile:
            return
        try:
            _var, warning = self.profiles.set_var(project, profile, preset.key, value, sensitive)
        except Exception as exc:
            self._error(exc)
            return
        self._refresh_profiles()
        self.statusBar().showMessage(f"已加入 {project} / {profile}：{preset.key}", 5000)
        if warning:
            QMessageBox.warning(self, "敏感值未保存", warning)

    # ------------------------------------------------------------------ 系统环境
    def _build_env_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("作用域"))
        self.scope_combo = QComboBox()
        for scope in SCOPES:
            self.scope_combo.addItem(scope_label(scope), scope)
        self.scope_combo.currentIndexChanged.connect(lambda _i: self._refresh_env())
        bar.addWidget(self.scope_combo)
        self.env_filter = QLineEdit()
        self.env_filter.setPlaceholderText("按变量名筛选…")
        self.env_filter.setClearButtonEnabled(True)
        self.env_filter.textChanged.connect(lambda _t: self._refresh_env_vars())
        bar.addWidget(self.env_filter, 1)
        self.env_reveal = QCheckBox("显示敏感值")
        self.env_reveal.toggled.connect(self._toggle_env_reveal)
        bar.addWidget(self.env_reveal)
        refresh = QPushButton("刷新")
        refresh.setObjectName("secondary")
        refresh.clicked.connect(self._refresh_env)
        bar.addWidget(refresh)
        history = QPushButton("操作历史")
        history.setObjectName("secondary")
        history.clicked.connect(self._show_audit)
        bar.addWidget(history)
        layout.addLayout(bar)

        splitter = QSplitter(Qt.Vertical)
        env_box = QWidget()
        env_layout = QVBoxLayout(env_box)
        env_layout.setContentsMargins(0, 0, 0, 0)
        env_layout.addWidget(QLabel("环境变量"))
        self.env_table = _table(["变量名", "值"])
        env_layout.addWidget(self.env_table, 1)
        env_actions = QHBoxLayout()
        edit_var = QPushButton("新增 / 修改变量…")
        edit_var.clicked.connect(self._edit_env_var)
        delete_var = QPushButton("删除变量")
        delete_var.setObjectName("danger")
        delete_var.clicked.connect(self._delete_env_var)
        dedupe = QPushButton("PATH 一键去重")
        dedupe.setObjectName("secondary")
        dedupe.clicked.connect(self._dedupe_path)
        self.env_write_hint = QLabel("")
        self.env_write_hint.setObjectName("hint")
        env_actions.addWidget(edit_var)
        env_actions.addWidget(delete_var)
        env_actions.addWidget(dedupe)
        env_actions.addWidget(self.env_write_hint, 1)
        env_layout.addLayout(env_actions)
        splitter.addWidget(env_box)

        path_box = QWidget()
        path_layout = QVBoxLayout(path_box)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.addWidget(QLabel("PATH 分析"))
        self.path_table = _table(["序号", "状态", "目录"])
        path_layout.addWidget(self.path_table, 1)
        splitter.addWidget(path_box)
        splitter.setSizes([380, 220])
        layout.addWidget(splitter, 1)

        snapshot_bar = QHBoxLayout()
        snapshot_bar.addWidget(QLabel("环境快照（变更历史与回滚）"))
        snapshot_bar.addStretch(1)
        create = QPushButton("创建快照")
        create.clicked.connect(self._create_snapshot)
        compare = QPushButton("对比所选两个")
        compare.setObjectName("secondary")
        compare.clicked.connect(self._diff_snapshots)
        restore = QPushButton("回滚到快照")
        restore.setObjectName("danger")
        restore.clicked.connect(self._restore_snapshot)
        export = QPushButton("导出 JSON")
        export.setObjectName("secondary")
        export.clicked.connect(self._export_snapshot)
        for widget in (create, compare, restore, export):
            snapshot_bar.addWidget(widget)
        layout.addLayout(snapshot_bar)
        self.snapshot_table = _table(["ID", "作用域", "创建时间", "标签", "变量数"])
        self.snapshot_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.snapshot_table.setMaximumHeight(180)
        layout.addWidget(self.snapshot_table)
        return page

    def _current_scope(self) -> str:
        return self.scope_combo.currentData() or "user"

    def _show_audit(self) -> None:
        if self.audit is None:
            QMessageBox.information(self, "操作历史", "本次运行未启用审计日志。")
            return
        entries = self.audit.recent(50)
        dialog = _TextDialog(
            "操作历史（最近 50 条）",
            self.audit.format_lines(entries),
            self,
            f"只记录动作、变量名与结果，不含变量值。日志目录：{self.audit.dir}",
        )
        dialog.exec()

    def _refresh_env(self) -> None:
        scope = self._current_scope()
        writable = scope == "user"
        self.env_write_hint.setText(
            "用户作用域可写：所有写入都会先自动备份，可回滚。" if writable
            else ("系统作用域为只读；如需修改请在系统设置中操作。" if scope == "system" else "当前进程作用域只读，仅用于查看。")
        )
        self._refresh_env_vars()
        self._refresh_path()
        self._refresh_snapshots()

    def _refresh_env_vars(self) -> None:
        scope = self._current_scope()
        try:
            snapshot = self.env.current(scope)
        except EnvForgeError as exc:
            self.env_table.setRowCount(0)
            self.path_table.setRowCount(0)
            self.statusBar().showMessage(str(exc), 8000)
            return
        needle = self.env_filter.text().strip().lower()
        rows = []
        for key in sorted(snapshot.values):
            if needle and needle not in key.lower():
                continue
            value = snapshot.values[key]
            if looks_sensitive_key(key) and not self._reveal_env:
                value = REDACTED
            rows.append([key, value])
        _fill(self.env_table, rows)
        self.env_table.resizeColumnToContents(0)

    def _toggle_env_reveal(self, checked: bool) -> None:
        if checked and not _confirm(
            self,
            "显示环境中的敏感值",
            "即将显示名称中包含 TOKEN / PASSWORD / KEY 等变量的明文。",
            yes_text="显示",
        ):
            self.env_reveal.blockSignals(True)
            self.env_reveal.setChecked(False)
            self.env_reveal.blockSignals(False)
            self._reveal_env = False
        else:
            self._reveal_env = checked
        self._refresh_env_vars()

    def _refresh_path(self) -> None:
        scope = self._current_scope()
        try:
            report = self.env.path_report(scope)
        except EnvForgeError:
            self.path_table.setRowCount(0)
            return
        labels = {"ok": "正常", "missing": "不存在", "duplicate": "重复", "empty": "空段"}
        colors = {"ok": "#237804", "missing": "#d4380d", "duplicate": "#d46b08", "empty": "#d46b08"}
        _fill(
            self.path_table,
            [[item["index"], labels.get(item["status"], item["status"]), item["raw"] or "<空段>"] for item in report],
        )
        for row, item in enumerate(report):
            cell = self.path_table.item(row, 1)
            if cell:
                cell.setForeground(QColor(colors.get(item["status"], "#1f2933")))

    def _require_user_scope(self) -> bool:
        scope = self._current_scope()
        if scope != "user":
            QMessageBox.information(
                self, "该作用域只读", "只有“用户”作用域可以写入；系统与当前进程作用域仅供查看。"
            )
            return False
        return True

    def _edit_env_var(self) -> None:
        if not self._require_user_scope():
            return
        row = self.env_table.currentRow()
        key = self.env_table.item(row, 0).text() if row >= 0 else ""
        dialog = _VarDialog(self, key=key)
        if dialog.exec() != QDialog.Accepted:
            return
        name, value, _sensitive = dialog.values()
        if not name:
            QMessageBox.information(self, "变量名为空", "请输入变量名。")
            return
        if not _confirm(
            self,
            "写入用户环境变量",
            f"将写入用户环境变量 {name}。",
            "此操作会影响之后启动的程序；EnvForge 会先创建回滚快照。",
            danger=True,
            yes_text="写入",
        ):
            return
        try:
            result = self.env.apply("user", {name: value}, confirm=True, label="界面写入")
        except Exception as exc:
            self._error(exc)
            return
        self._refresh_env()
        self.statusBar().showMessage(
            f"已写入 {name}；回滚快照 #{result['pre_snapshot_id']}", 6000
        )

    def _delete_env_var(self) -> None:
        if not self._require_user_scope():
            return
        row = self.env_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "未选择", "请先选择要删除的变量。")
            return
        key = self.env_table.item(row, 0).text()
        if not _confirm(
            self,
            "删除用户环境变量",
            f"将删除用户环境变量 {key}。",
            "删除后无法通过 EnvForge 撤销，但会保留回滚快照。",
            danger=True,
            yes_text="删除",
        ):
            return
        try:
            self.env.apply("user", {key: None}, confirm=True, label=f"界面删除 {key}")
        except Exception as exc:
            self._error(exc)
            return
        self._refresh_env()

    def _dedupe_path(self) -> None:
        if not self._require_user_scope():
            return
        snapshot = self.env.current("user")
        path_value = ""
        for key, value in snapshot.values.items():
            if key.upper() == "PATH":
                path_value = value
                break
        cleaned, removed = dedupe_path(path_value)
        if removed == 0:
            QMessageBox.information(self, "无需去重", "用户 PATH 中没有空段或重复目录。")
            return
        if not _confirm(
            self,
            "PATH 去重",
            f"用户 PATH 中有 {removed} 个空段或重复目录，是否写入去重结果？",
            "写入前会自动创建回滚快照；不会删除任何目录内容。",
            danger=True,
            yes_text="写入",
        ):
            return
        try:
            result = self.env.apply("user", {"PATH": cleaned}, confirm=True, label="PATH 去重")
        except Exception as exc:
            self._error(exc)
            return
        self._refresh_env()
        self.statusBar().showMessage(f"已写入去重后的 PATH；回滚快照 #{result['pre_snapshot_id']}", 6000)

    # ------------------------------------------------------------------ 快照
    def _refresh_snapshots(self) -> None:
        try:
            snapshots = self.env.list(limit=50)
        except EnvForgeError:
            self.snapshot_table.setRowCount(0)
            return
        _fill(
            self.snapshot_table,
            [
                [item.id, scope_label(item.scope), _fmt_ts(item.created_at), item.label or "-", len(item.values)]
                for item in snapshots
            ],
        )

    def _create_snapshot(self) -> None:
        scope = self._current_scope()
        try:
            snapshot = self.env.create(scope, label="手动快照")
        except Exception as exc:
            self._error(exc)
            return
        self._refresh_snapshots()
        self.statusBar().showMessage(
            f"已创建快照 #{snapshot.id}（{scope_label(scope)}，{len(snapshot.values)} 个变量）", 6000
        )

    def _selected_snapshots(self) -> list[int]:
        rows = sorted({index.row() for index in self.snapshot_table.selectedIndexes()})
        ids: list[int] = []
        for row in rows:
            item = self.snapshot_table.item(row, 0)
            if item:
                ids.append(int(item.data(Qt.UserRole)))
        return ids

    def _diff_snapshots(self) -> None:
        ids = self._selected_snapshots()
        if len(ids) != 2:
            QMessageBox.information(self, "需要选择两个快照", "请在快照列表中选择两条记录再对比。")
            return
        before, after = sorted(ids)
        try:
            data = self.env.diff(before, after, reveal=False)
        except Exception as exc:
            self._error(exc)
            return
        lines = [f"快照 #{before} → #{after}"]
        for key, value in data["added"].items():
            lines.append(f"+ {key}={value}")
        for key, value in data["removed"].items():
            lines.append(f"- {key}={value}")
        for item in data["changed"]:
            lines.append(f"~ {item['key']}: {item['before']} -> {item['after']}")
        if data["path_added"]:
            lines.append("")
            lines.append("PATH 新增目录：")
            lines.extend(f"  + {entry}" for entry in data["path_added"])
        if data["path_removed"]:
            lines.append("PATH 移除目录：")
            lines.extend(f"  - {entry}" for entry in data["path_removed"])
        if len(lines) == 1:
            lines.append("两个快照没有差异。")
        _TextDialog("快照对比", "\n".join(lines), self, note="敏感变量以 *** 显示。").exec()

    def _restore_snapshot(self) -> None:
        ids = self._selected_snapshots()
        if len(ids) != 1:
            QMessageBox.information(self, "需要选择一个快照", "请在快照列表中选择一条记录。")
            return
        snapshot_id = ids[0]
        try:
            snapshot = self.env.get(snapshot_id)
        except Exception as exc:
            self._error(exc)
            return
        if not _confirm(
            self,
            "回滚环境变量",
            f"将把{scope_label(snapshot.scope)}环境变量恢复到快照 #{snapshot_id}（{snapshot.created_at}）。",
            "回滚前会自动创建备份快照；快照中不存在的变量默认保留。",
            danger=True,
            yes_text="回滚",
        ):
            return
        try:
            result = self.env.restore(snapshot_id, confirm=True)
        except Exception as exc:
            self._error(exc)
            return
        self._refresh_env()
        if not result["changed"] and not result["deleted"]:
            self.statusBar().showMessage("当前环境已与快照一致。", 6000)
        else:
            self.statusBar().showMessage(
                f"已回滚 {len(result['changed'])} 个变量；回滚前备份 #{result['pre_snapshot_id']}", 8000
            )

    def _export_snapshot(self) -> None:
        ids = self._selected_snapshots()
        if len(ids) != 1:
            QMessageBox.information(self, "需要选择一个快照", "请在快照列表中选择一条记录。")
            return
        data = self.env.export(ids[0], reveal=False)
        path, _ = QFileDialog.getSaveFileName(
            self, "导出快照", f"envforge-snapshot-{ids[0]}.json", "JSON (*.json)"
        )
        if not path:
            return
        import json

        Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.statusBar().showMessage(f"已导出到 {path}", 5000)

    # ------------------------------------------------------------------ 体检
    def _build_doctor_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        bar = QHBoxLayout()
        self.doctor_summary = QLabel("尚未体检。检查项：PATH 重复/失效、运行时一致性、项目路径、敏感值存储。")
        self.doctor_summary.setObjectName("hint")
        bar.addWidget(self.doctor_summary, 1)
        run = QPushButton("开始体检")
        run.clicked.connect(self._run_doctor)
        bar.addWidget(run)
        layout.addLayout(bar)
        self.doctor_table = _table(["级别", "问题", "详情", "建议"])
        doctor_header = self.doctor_table.horizontalHeader()
        doctor_header.setSectionResizeMode(2, QHeaderView.Stretch)
        doctor_header.setSectionResizeMode(3, QHeaderView.Stretch)
        layout.addWidget(self.doctor_table, 1)
        return page

    def _run_doctor(self) -> None:
        self.doctor_summary.setText("正在体检…")
        self._run_worker(lambda: self.doctor.run(["user", "system"]), self._on_doctor)

    def _on_doctor(self, findings) -> None:
        labels = {"error": "错误", "warn": "警告", "info": "提示"}
        colors = {"error": "#d4380d", "warn": "#d46b08", "info": "#52606d"}
        _fill(
            self.doctor_table,
            [[labels.get(item.severity, item.severity), item.title, item.detail, item.suggestion] for item in findings],
        )
        for row, item in enumerate(findings):
            cell = self.doctor_table.item(row, 0)
            if cell:
                cell.setForeground(QColor(colors.get(item.severity, "#1f2933")))
        counts = summarize(findings)
        if not findings:
            self.doctor_summary.setText("未发现问题。")
        else:
            self.doctor_summary.setText(
                f"合计：错误 {counts['error']}，警告 {counts['warn']}，提示 {counts['info']}。"
                "双击条目可查看完整建议。"
            )
            self.doctor_table.resizeColumnsToContents()


def launch(argv: list[str] | None = None) -> int:
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("EnvForge")
    font = QFont("Microsoft YaHei UI", 9)
    app.setFont(font)
    audit = AuditLog.for_database(os.environ.get("ENVFORGE_DB") or default_db_path())
    install_crash_hook(audit.dir)
    db = Database(os.environ.get("ENVFORGE_DB") or default_db_path())
    if not db.integrity.get("ok"):
        QMessageBox.warning(
            None,
            "数据库完整性异常",
            f"数据库自检发现问题：{db.integrity['detail']}\n\n"
            f"请先从 {Path(db.path).parent / 'backups'} 恢复最近的备份，再继续使用，"
            "避免写入进一步扩大损坏。",
        )
    try:
        db.backup_daily()
    except DatabaseError as exc:
        print(f"警告：每日自动备份失败（{exc}）。", file=sys.stderr)
    audit.append("gui.start", result="ok", detail=f"v{__version__}")
    window = MainWindow(db, audit)
    window.show()
    code = app.exec()
    db.close()
    return code
