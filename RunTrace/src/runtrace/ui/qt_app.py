"""PySide6 桌面界面（仅在启动 GUI 时导入）。

界面提供：任务表单与安全预览、实时 stdout/stderr、历史检索、两次运行对比、
模板保存/复制/再次运行、导出、复现包生成与显式确认执行、只读 Git 上下文。
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .. import __version__
from ..command_builder import ConfigError, RunConfig, build_command
from ..diff_engine import compare_runs, render_comparison
from ..process_runner import ProcessRunner, RunOutcome
from ..project_context import get_git_context, project_label
from ..replay_manager import ReplayError, ReplayManager
from ..run_store import RunStore, default_data_dir, recover_interrupted_runs

_STATUS_LABELS = {
    "running": "运行中",
    "success": "成功",
    "failed": "失败",
    "timeout": "超时",
    "cancelled": "已停止",
    "interrupted": "异常中断",
}
_STATUS_FILTERS = [
    ("全部状态", None),
    ("成功", "success"),
    ("失败", "failed"),
    ("超时", "timeout"),
    ("已停止", "cancelled"),
    ("异常中断", "interrupted"),
    ("运行中", "running"),
]


def _status_label(status: str | None) -> str:
    return _STATUS_LABELS.get(status or "", status or "")


def _confirm(
    parent: QWidget,
    title: str,
    text: str,
    informative_text: str = "",
    *,
    yes_text: str = "确认",
    icon: QMessageBox.Icon = QMessageBox.Question,
) -> bool:
    """带中文按钮文案的确认框，避免 Qt 标准按钮显示英文 Yes/No。"""
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setIcon(icon)
    box.setText(text)
    if informative_text:
        box.setInformativeText(informative_text)
    box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
    box.setDefaultButton(QMessageBox.No)
    box.button(QMessageBox.Yes).setText(yes_text)
    box.button(QMessageBox.No).setText("取消")
    return box.exec() == QMessageBox.Yes


class RunWorker(QThread):
    output = Signal(str, str)
    started_run = Signal(str)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, store: RunStore, config: RunConfig, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self.config = config
        self.cancel_event = threading.Event()
        self.run_id: str | None = None
        self.runner: ProcessRunner | None = None

    def run(self) -> None:
        try:
            runner = ProcessRunner(self.store)
            self.runner = runner
            outcome = runner.run(
                self.config,
                cancel_event=self.cancel_event,
                on_output=lambda stream, text: self.output.emit(stream, text),
                on_started=self._on_started,
            )
            self.completed.emit(outcome.to_dict())
        except Exception:
            self.failed.emit(traceback.format_exc())

    def _on_started(self, run_id: str) -> None:
        self.run_id = run_id
        self.started_run.emit(run_id)

    def stop(self) -> None:
        self.cancel_event.set()
        if self.runner is not None and self.run_id:
            self.runner.stop(self.run_id)


class PackageExecWorker(QThread):
    output = Signal(str, str)
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, store: RunStore, package_path: str, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self.package_path = package_path

    def run(self) -> None:
        try:
            outcome = ReplayManager(self.store).execute_package(
                self.package_path,
                confirmed=True,
                on_output=lambda stream, text: self.output.emit(stream, text),
            )
            self.completed.emit(outcome.to_dict())
        except Exception:
            self.failed.emit(traceback.format_exc())


class MainWindow(QMainWindow):
    def __init__(self, store: RunStore, recovered: int = 0) -> None:
        super().__init__()
        self.store = store
        self.worker: RunWorker | None = None
        self.package_worker: PackageExecWorker | None = None
        self.current_run_id: str | None = None
        self._run_started_monotonic = 0.0
        self.elapsed_timer = QTimer(self)
        self.elapsed_timer.setInterval(500)
        self.elapsed_timer.timeout.connect(self._tick_elapsed)
        self.setWindowTitle(f"RunTrace {__version__} — 本地任务运行历史与复现")
        self.resize(1240, 860)
        self._build_ui()
        self._apply_style()
        self.refresh_templates()
        self.refresh_history()
        self.refresh_git()

        self.statusBar().showMessage(
            f"数据目录: {default_data_dir()}    启动恢复: {recovered} 条 running → interrupted"
        )

    # ---------- UI 构建 ----------
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        title_row = QHBoxLayout()
        title = QLabel("RunTrace")
        title_font = QFont()
        title_font.setPointSize(18)
        title_font.setBold(True)
        title.setFont(title_font)
        subtitle = QLabel("命令运行记录 · 日志对比 · 复现包")
        subtitle.setStyleSheet("color:#667085;")
        title_row.addWidget(title)
        title_row.addWidget(subtitle)
        title_row.addStretch(1)
        self.global_status = QLabel("就绪")
        self.global_status.setStyleSheet("color:#1677ff; font-weight:600;")
        title_row.addWidget(self.global_status)
        root.addLayout(title_row)

        form_group = QGroupBox("新建运行")
        form = QGridLayout(form_group)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(6)
        form.addWidget(QLabel("名称"), 0, 0)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如：单元测试 / 构建任务")
        form.addWidget(self.name_edit, 0, 1, 1, 3)

        form.addWidget(QLabel("工作目录"), 1, 0)
        self.cwd_edit = QLineEdit(str(Path.cwd()))
        form.addWidget(self.cwd_edit, 1, 1, 1, 2)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse_cwd)
        form.addWidget(browse, 1, 3)
        self.git_label = QLabel("Git: 未检测")
        self.git_label.setStyleSheet("color:#667085;")
        form.addWidget(self.git_label, 2, 1, 1, 2)
        git_button = QPushButton("刷新 Git")
        git_button.clicked.connect(self.refresh_git)
        form.addWidget(git_button, 2, 3)

        form.addWidget(QLabel("命令"), 3, 0)
        self.command_edit = QLineEdit()
        self.command_edit.setPlaceholderText('可执行程序，例如 python、git 或完整路径')
        form.addWidget(self.command_edit, 3, 1, 1, 3)

        form.addWidget(QLabel("参数数组"), 4, 0)
        self.args_edit = QLineEdit("[]")
        self.args_edit.setPlaceholderText('JSON 数组，例如 ["-c", "print(1)"]；无需 shell 转义')
        form.addWidget(self.args_edit, 4, 1, 1, 3)

        form.addWidget(QLabel("环境覆盖"), 5, 0)
        self.env_edit = QPlainTextEdit()
        self.env_edit.setFixedHeight(54)
        self.env_edit.setPlaceholderText("每行一个 KEY=VALUE；敏感键会自动脱敏")
        form.addWidget(self.env_edit, 5, 1, 1, 2)
        timeout_row = QHBoxLayout()
        timeout_row.addWidget(QLabel("超时(秒)"))
        self.timeout_spin = QDoubleSpinBox()
        self.timeout_spin.setRange(0, 86400)
        self.timeout_spin.setDecimals(1)
        self.timeout_spin.setSingleStep(5)
        self.timeout_spin.setSpecialValueText("无超时")
        timeout_row.addWidget(self.timeout_spin)
        self.shell_check = QCheckBox("显式 shell 模式（有风险）")
        self.shell_check.toggled.connect(self._update_preview)
        timeout_row.addWidget(self.shell_check)
        timeout_row.addStretch(1)
        form.addLayout(timeout_row, 5, 3)

        self.preview_label = QLabel("请填写命令后预览")
        self.preview_label.setWordWrap(True)
        self.preview_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.preview_label.setStyleSheet(
            "background:#f6f8fa; border:1px solid #d0d7de; border-radius:4px; padding:6px;"
        )
        form.addWidget(self.preview_label, 6, 0, 1, 4)

        button_row = QHBoxLayout()
        self.run_button = QPushButton("运行")
        self.run_button.setObjectName("runButton")
        self.run_button.clicked.connect(self.start_run)
        self.stop_button = QPushButton("停止进程树")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_run)
        save_tpl = QPushButton("保存为模板")
        save_tpl.clicked.connect(self.save_current_template)
        button_row.addWidget(self.run_button)
        button_row.addWidget(self.stop_button)
        button_row.addWidget(save_tpl)
        button_row.addStretch(1)
        self.template_combo = QComboBox()
        self.template_combo.setMinimumWidth(180)
        button_row.addWidget(QLabel("模板:"))
        button_row.addWidget(self.template_combo)
        load_tpl = QPushButton("加载")
        load_tpl.clicked.connect(self.load_template)
        copy_tpl = QPushButton("复制")
        copy_tpl.clicked.connect(self.copy_template)
        delete_tpl = QPushButton("删除")
        delete_tpl.clicked.connect(self.delete_template)
        button_row.addWidget(load_tpl)
        button_row.addWidget(copy_tpl)
        button_row.addWidget(delete_tpl)
        form.addLayout(button_row, 7, 0, 1, 4)
        root.addWidget(form_group)

        for widget in (self.command_edit, self.cwd_edit, self.args_edit):
            widget.textChanged.connect(self._update_preview)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)
        splitter.addWidget(self._build_history_panel())
        splitter.addWidget(self._build_detail_panel())
        splitter.setSizes([540, 700])

    def _build_history_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        header = QHBoxLayout()
        header.addWidget(QLabel("运行历史"))
        header.addStretch(1)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(lambda: self.refresh_history())
        header.addWidget(refresh)
        layout.addLayout(header)
        filter_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索命令、参数、路径或日志…")
        self.search_edit.returnPressed.connect(lambda: self.refresh_history())
        self.search_edit.textChanged.connect(self._schedule_search)
        self.status_filter = QComboBox()
        for label, value in _STATUS_FILTERS:
            self.status_filter.addItem(label, value)
        self.status_filter.currentIndexChanged.connect(lambda _: self.refresh_history())
        filter_row.addWidget(self.search_edit, 1)
        filter_row.addWidget(self.status_filter)
        layout.addLayout(filter_row)
        project_time_row = QHBoxLayout()
        self.project_filter = QLineEdit()
        self.project_filter.setPlaceholderText("按项目/cwd 路径筛选（留空为全部）")
        self.project_filter.textChanged.connect(self._schedule_search)
        self.time_filter = QComboBox()
        self.time_filter.addItem("全部时间", None)
        self.time_filter.addItem("今天", "today")
        self.time_filter.addItem("最近 7 天", "7d")
        self.time_filter.addItem("最近 30 天", "30d")
        self.time_filter.currentIndexChanged.connect(lambda _: self.refresh_history())
        project_time_row.addWidget(self.project_filter, 1)
        project_time_row.addWidget(self.time_filter)
        layout.addLayout(project_time_row)
        self.history_list = QListWidget()
        self.history_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.history_list.setAlternatingRowColors(True)
        self.history_list.itemSelectionChanged.connect(self._history_selection_changed)
        self.history_list.itemDoubleClicked.connect(lambda _: self.load_run_into_form())
        layout.addWidget(self.history_list, 1)
        grid = QGridLayout()
        detail_button = QPushButton("查看详情")
        detail_button.clicked.connect(self.show_selected_detail)
        load_button = QPushButton("载入表单")
        load_button.clicked.connect(self.load_run_into_form)
        rerun_button = QPushButton("再次运行")
        rerun_button.clicked.connect(self.rerun_selected)
        compare_button = QPushButton("对比两次")
        compare_button.clicked.connect(self.compare_selected)
        export_button = QPushButton("导出…")
        export_button.clicked.connect(self.export_selected)
        replay_button = QPushButton("生成复现包…")
        replay_button.clicked.connect(self.replay_selected)
        inspect_button = QPushButton("检查/执行复现包…")
        inspect_button.clicked.connect(self.inspect_or_execute_package)
        grid.addWidget(detail_button, 0, 0)
        grid.addWidget(load_button, 0, 1)
        grid.addWidget(rerun_button, 0, 2)
        grid.addWidget(compare_button, 1, 0)
        grid.addWidget(export_button, 1, 1)
        grid.addWidget(replay_button, 1, 2)
        grid.addWidget(inspect_button, 2, 0, 1, 3)
        layout.addLayout(grid)
        return panel

    def _build_detail_panel(self) -> QWidget:
        self.right_tabs = QTabWidget()
        output_page = QWidget()
        output_layout = QVBoxLayout(output_page)
        output_layout.setContentsMargins(0, 0, 0, 0)
        self.output_status = QLabel("输出会在运行时实时追加；完整日志保存在 SQLite。")
        self.output_status.setStyleSheet("color:#667085;")
        output_layout.addWidget(self.output_status)
        self.stream_tabs = QTabWidget()
        self.stdout_edit = QPlainTextEdit()
        self.stdout_edit.setReadOnly(True)
        self.stdout_edit.setMaximumBlockCount(300000)
        self.stdout_edit.setFont(QFont("Consolas", 9))
        self.stderr_edit = QPlainTextEdit()
        self.stderr_edit.setReadOnly(True)
        self.stderr_edit.setMaximumBlockCount(300000)
        self.stderr_edit.setFont(QFont("Consolas", 9))
        self.stream_tabs.addTab(self.stdout_edit, "stdout")
        self.stream_tabs.addTab(self.stderr_edit, "stderr")
        output_layout.addWidget(self.stream_tabs, 1)
        self.right_tabs.addTab(output_page, "输出")

        self.detail_view = QTextBrowser()
        self.right_tabs.addTab(self.detail_view, "详情")

        self.compare_view = QTextBrowser()
        self.compare_view.setFont(QFont("Consolas", 9))
        self.right_tabs.addTab(self.compare_view, "对比结果")
        return self.right_tabs

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow { background: #ffffff; }
            QGroupBox { font-weight: 600; border: 1px solid #d0d7de; border-radius: 6px;
                        margin-top: 8px; padding-top: 10px; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
            QPushButton { padding: 5px 10px; border: 1px solid #d0d7de;
                          border-radius: 4px; background: #f6f8fa; }
            QPushButton:hover { background: #eef1f5; }
            QPushButton:disabled { color: #8c959f; }
            QPushButton#runButton { background: #1677ff; color: white; border: none;
                                    font-weight: 600; padding: 6px 18px; }
            QPushButton#runButton:hover { background: #0e63d6; }
            QLineEdit, QPlainTextEdit, QComboBox, QDoubleSpinBox {
                border: 1px solid #d0d7de; border-radius: 4px; padding: 3px; }
            QListWidget { border: 1px solid #d0d7de; border-radius: 4px; }
            """
        )

    # ---------- 表单 ----------
    def _browse_cwd(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择工作目录", self.cwd_edit.text())
        if chosen:
            self.cwd_edit.setText(chosen)
            self.refresh_git()

    def _parse_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        for line in self.env_edit.toPlainText().splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                raise ConfigError(f"环境覆盖格式应为 KEY=VALUE：{stripped}")
            key, value = stripped.split("=", 1)
            if not key:
                raise ConfigError("环境变量名不能为空")
            env[key] = value
        return env

    def _collect_config(self) -> RunConfig:
        command = self.command_edit.text().strip()
        if not command:
            raise ConfigError("命令不能为空")
        args_text = self.args_edit.text().strip() or "[]"
        try:
            args = json.loads(args_text)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"参数数组不是合法 JSON：{exc}") from exc
        if not isinstance(args, list):
            raise ConfigError("参数必须是 JSON 数组")
        args = [str(arg) for arg in args]
        timeout = self.timeout_spin.value()
        return RunConfig(
            name=self.name_edit.text().strip() or Path(command).name or "run",
            cwd=self.cwd_edit.text().strip(),
            command=command,
            args=args,
            env_overrides=self._parse_env(),
            timeout=timeout if timeout > 0 else None,
            shell=self.shell_check.isChecked(),
        )

    def _update_preview(self) -> None:
        try:
            config = self._collect_config()
            build = build_command(config)
            warning = f"\n风险: {build.warning}" if build.warning else ""
            mode = "shell（有风险）" if build.shell else "参数数组（shell=False）"
            self.preview_label.setText(
                f"命令: {build.display}\n目录: {build.cwd}\n模式: {mode}{warning}"
            )
            if build.shell or build.warning:
                self.preview_label.setStyleSheet(
                    "background:#fff4e5; border:1px solid #f0b429; color:#7a4b00;"
                    "border-radius:4px; padding:6px;"
                )
            else:
                self.preview_label.setStyleSheet(
                    "background:#f6f8fa; border:1px solid #d0d7de;"
                    "border-radius:4px; padding:6px;"
                )
        except Exception as exc:
            self.preview_label.setText(f"配置预览不可用: {exc}")
            self.preview_label.setStyleSheet(
                "background:#fff0f0; border:1px solid #d1242f; color:#a40e26;"
                "border-radius:4px; padding:6px;"
            )

    def refresh_git(self) -> None:
        context = get_git_context(self.cwd_edit.text().strip())
        if context.get("is_git"):
            dirty = "dirty" if context.get("dirty") else "clean"
            self.git_label.setText(
                f"Git: {context.get('branch') or 'detached'} @ "
                f"{(context.get('commit') or '')[:12]} ({dirty})"
            )
            self.git_label.setToolTip(context.get("root") or "")
        else:
            self.git_label.setText("Git: 非 Git 项目（只读检测）")
            self.git_label.setToolTip(context.get("error") or "")

    # ---------- 运行 ----------
    def _set_running(self, running: bool) -> None:
        self.run_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        if running:
            self._run_started_monotonic = time.monotonic()
            self.elapsed_timer.start()
            self.global_status.setText("运行中… 0.0s")
        else:
            self.elapsed_timer.stop()
            self.global_status.setText("就绪")

    def _tick_elapsed(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            elapsed = time.monotonic() - self._run_started_monotonic
            self.global_status.setText(f"运行中… {elapsed:.1f}s")

    def start_run(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, "正在运行", "请先停止或等待当前任务结束。")
            return
        if self.package_worker is not None and self.package_worker.isRunning():
            QMessageBox.information(self, "复现执行中", "请等待当前复现任务结束。")
            return
        try:
            config = self._collect_config()
            build = build_command(config)
        except Exception as exc:
            QMessageBox.warning(self, "配置错误", str(exc))
            return

        details = [
            f"命令: {build.display}",
            f"工作目录: {build.cwd}",
            f"模式: {'shell（有风险）' if build.shell else '参数数组（shell=False）'}",
            f"超时: {config.timeout if config.timeout is not None else '无'}",
        ]
        if build.warning:
            details.append(f"风险提示: {build.warning}")
        if not _confirm(
            self,
            "确认运行",
            "即将执行以下任务",
            "\n".join(details),
            yes_text="运行",
            icon=QMessageBox.Warning if build.shell or build.warning else QMessageBox.Information,
        ):
            return

        self._clear_output()
        self._set_running(True)
        self.output_status.setText("运行中：实时日志")
        self.right_tabs.setCurrentIndex(0)
        self.worker = RunWorker(self.store, config, self)
        self.worker.output.connect(self._append_output)
        self.worker.started_run.connect(self._on_started)
        self.worker.completed.connect(self._on_run_completed)
        self.worker.failed.connect(self._on_run_failed)
        self.worker.finished.connect(self._on_worker_thread_finished)
        self.worker.start()

    def stop_run(self) -> None:
        if self.worker is not None and self.worker.isRunning():
            self.worker.stop()
            self.elapsed_timer.stop()
            self.stop_button.setEnabled(False)
            self.output_status.setText("正在终止子进程树…")
            self.global_status.setText("停止中…")

    def _on_started(self, run_id: str) -> None:
        self.current_run_id = run_id
        self.output_status.setText(f"运行中：{run_id}")

    def _append_output(self, stream: str, text: str) -> None:
        editor = self.stdout_edit if stream == "stdout" else self.stderr_edit
        cursor = editor.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(text)
        editor.setTextCursor(cursor)
        editor.ensureCursorVisible()

    def _clear_output(self) -> None:
        self.stdout_edit.clear()
        self.stderr_edit.clear()

    def _on_run_completed(self, result: dict[str, Any]) -> None:
        self._set_running(False)
        self.current_run_id = result["run_id"]
        status = _status_label(result["status"])
        self.output_status.setText(
            f"{status} · 退出码 {result['exit_code']} · {result['duration_ms']} ms · "
            f"{result['run_id']}"
        )
        self.refresh_history(select_id=result["run_id"])
        if result.get("error"):
            self.output_status.setText(self.output_status.text() + f" · {result['error']}")
        if result["status"] != "success":
            self.right_tabs.setCurrentIndex(0)

    def _on_run_failed(self, error_text: str) -> None:
        self._set_running(False)
        self.output_status.setText("执行线程异常")
        QMessageBox.critical(self, "执行异常", error_text[-3000:])
        self.refresh_history()

    def _on_worker_thread_finished(self) -> None:
        worker = self.worker
        if worker is not None:
            worker.deleteLater()
        self.worker = None

    # ---------- 历史 ----------
    def _schedule_search(self) -> None:
        # QLineEdit 的 textChanged 直接刷新在数据量大时会卡顿；用短延迟合并。
        if getattr(self, "_search_timer", None) is None:
            self._search_timer = QTimer(self)
            self._search_timer.setSingleShot(True)
            self._search_timer.setInterval(250)
            self._search_timer.timeout.connect(self.refresh_history)
        self._search_timer.start()

    def refresh_history(self, select_id: str | None = None) -> None:
        from datetime import datetime, timedelta, timezone

        searched = self.search_edit.text().strip() or None
        status = self.status_filter.currentData()
        project = self.project_filter.text().strip() or None
        time_range = self.time_filter.currentData()
        since = None
        now = datetime.now(timezone.utc)
        if time_range == "today":
            since = datetime(now.year, now.month, now.day, tzinfo=timezone.utc).isoformat()
        elif time_range == "7d":
            since = (now - timedelta(days=7)).isoformat()
        elif time_range == "30d":
            since = (now - timedelta(days=30)).isoformat()
        try:
            runs = self.store.list_runs(
                search=searched,
                status=status,
                project=project,
                since=since,
                limit=500,
            )
        except Exception as exc:
            QMessageBox.critical(self, "读取历史失败", str(exc))
            return
        self.history_list.blockSignals(True)
        self.history_list.clear()
        for run in runs:
            exit_text = "-" if run.get("exit_code") is None else str(run.get("exit_code"))
            duration = run.get("duration_ms")
            duration_text = f"{duration}ms" if duration is not None else "-"
            started = (run.get("started_at") or "").replace("T", " ")[:19]
            text = (
                f"{run['name']}\n"
                f"{_status_label(run['status'])} · exit {exit_text} · {duration_text} · {started}\n"
                f"{run['cwd']}"
            )
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, run["id"])
            item.setToolTip(
                f"Run ID: {run['id']}\n命令: {run['command']}\n"
                f"参数: {json.dumps(run['args'], ensure_ascii=False)}"
            )
            self.history_list.addItem(item)
            if select_id and run["id"] == select_id:
                self.history_list.setCurrentItem(item)
        self.history_list.blockSignals(False)
        if self.history_list.currentItem() is not None:
            self._history_selection_changed()

    def _selected_ids(self) -> list[str]:
        return [
            item.data(Qt.UserRole)
            for item in self.history_list.selectedItems()
        ]

    def _history_selection_changed(self) -> None:
        ids = self._selected_ids()
        if not ids:
            self.current_run_id = None
            return
        self.current_run_id = ids[0]
        self._display_run(ids[0])

    def _display_run(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        if run is None:
            return
        env_lines = [f"  {key}={value}" for key, value in sorted((run.get("env") or {}).items())]
        git = run.get("git_context") or {}
        meta = run.get("metadata") or {}
        lines = [
            f"Run ID: {run['id']}",
            f"名称: {run['name']}",
            f"状态: {_status_label(run['status'])}    退出码: {run['exit_code']}    "
            f"耗时: {run['duration_ms']} ms",
            f"命令: {run['command']}",
            f"参数: {json.dumps(run['args'], ensure_ascii=False)}",
            f"工作目录: {run['cwd']}",
            f"模式: {'shell（有风险）' if run['shell'] else '参数数组（shell=False）'}",
            f"超时: {run['timeout']}",
            f"开始: {run['started_at']}",
            f"结束: {run['ended_at']}",
            f"项目: {run.get('project_path')}",
            (
                f"Git: branch={git.get('branch')} commit={git.get('commit')} "
                f"dirty={git.get('dirty')}"
                if git.get("is_git")
                else "Git: 非 Git 项目"
            ),
            f"工具版本: {run.get('tool_version')}",
            "",
            "环境（已脱敏）:",
            *env_lines,
            "",
            "元数据:",
            json.dumps(meta, ensure_ascii=False, indent=2),
        ]
        self.detail_view.setPlainText("\n".join(lines))
        for stream_name, editor in (("stdout", self.stdout_edit), ("stderr", self.stderr_edit)):
            text = self.store.get_logs(run_id, stream_name)
            if len(text) > 2_000_000:
                text = "...[界面仅显示末尾 2,000,000 字符，完整日志可导出]...\n" + text[-2_000_000:]
            editor.setPlainText(text)

    def show_selected_detail(self) -> None:
        ids = self._selected_ids()
        if not ids:
            QMessageBox.information(self, "未选择", "请先在运行历史中选择一条记录。")
            return
        self._display_run(ids[0])
        self.right_tabs.setCurrentIndex(1)

    def load_run_into_form(self) -> None:
        ids = self._selected_ids()
        if not ids:
            QMessageBox.information(self, "未选择", "请先在运行历史中选择一条记录。")
            return
        try:
            config = self.store.config_from_run(ids[0], name_suffix="")
        except KeyError as exc:
            QMessageBox.warning(self, "载入失败", str(exc))
            return
        run = self.store.get_run(ids[0]) or {}
        self._load_config_to_form(config)
        redacted = run.get("metadata", {}).get("redacted_env_override_keys") or []
        if redacted:
            QMessageBox.information(
                self,
                "环境变量已脱敏",
                "以下环境覆盖值不会自动还原，请在表单中重新填写：\n" + ", ".join(redacted),
            )

    def _load_config_to_form(self, config: RunConfig) -> None:
        self.name_edit.setText(config.name)
        self.cwd_edit.setText(config.cwd)
        self.command_edit.setText(config.command)
        self.args_edit.setText(json.dumps(config.args, ensure_ascii=False))
        self.env_edit.setPlainText(
            "\n".join(f"{key}={value}" for key, value in config.env_overrides.items())
        )
        self.timeout_spin.setValue(config.timeout or 0)
        self.shell_check.setChecked(config.shell)
        self.refresh_git()
        self._update_preview()

    def rerun_selected(self) -> None:
        self.load_run_into_form()
        if self.command_edit.text().strip():
            self.start_run()

    def compare_selected(self) -> None:
        ids = self._selected_ids()
        if len(ids) != 2:
            QMessageBox.information(self, "选择两条", "请按住 Ctrl 选择恰好两条运行记录。")
            return
        a = self.store.get_run(ids[0])
        b = self.store.get_run(ids[1])
        if a is None or b is None:
            return
        # 以开始时间较早者为 A，减少方向歧义。
        if (a.get("started_at") or "") > (b.get("started_at") or ""):
            a, b = b, a
        report = compare_runs(
            a,
            b,
            stdout_a=self.store.get_logs(a["id"], "stdout"),
            stdout_b=self.store.get_logs(b["id"], "stdout"),
            stderr_a=self.store.get_logs(a["id"], "stderr"),
            stderr_b=self.store.get_logs(b["id"], "stderr"),
        )
        self.compare_view.setPlainText(render_comparison(report))
        self.right_tabs.setCurrentIndex(2)

    def export_selected(self) -> None:
        ids = self._selected_ids()
        if not ids:
            QMessageBox.information(self, "未选择", "请先选择一条运行记录。")
            return
        run_id = ids[0]
        path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "导出运行记录",
            f"runtrace_{run_id}.json",
            "JSON (*.json);;文本 (*.txt);;ZIP 归档 (*.zip)",
        )
        if not path:
            return
        suffix = Path(path).suffix.lower()
        if suffix == ".txt" or "文本" in selected_filter:
            fmt = "text"
            if suffix not in (".txt", ""):
                pass
        elif suffix == ".zip" or "ZIP" in selected_filter:
            fmt = "zip"
        else:
            fmt = "json"
        if Path(path).suffix.lower() not in (".json", ".txt", ".zip"):
            path += ".zip" if fmt == "zip" else (".txt" if fmt == "text" else ".json")
        try:
            result = self.store.export_run(run_id, path, fmt)
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        message = f"已导出到:\n{result['path']}\n\nSHA-256:\n{result['sha256']}"
        if result["sensitive_findings"]:
            message += (
                f"\n\n安全提示: 发现 {len(result['sensitive_findings'])} 处疑似敏感内容，"
                "请检查导出文件后再分享。"
            )
        QMessageBox.information(self, "导出完成", message)

    def replay_selected(self) -> None:
        ids = self._selected_ids()
        if not ids:
            QMessageBox.information(self, "未选择", "请先选择一条运行记录。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "生成复现包", f"runtrace_replay_{ids[0]}.zip", "ZIP (*.zip)"
        )
        if not path:
            return
        if not path.lower().endswith(".zip"):
            path += ".zip"
        try:
            info = ReplayManager(self.store).build_package(ids[0], path)
        except Exception as exc:
            QMessageBox.critical(self, "生成复现包失败", str(exc))
            return
        warning = "\n".join(f"• {item}" for item in info.warnings)
        gaps = "\n".join(f"• {item}" for item in info.environment_gaps)
        details = (
            f"复现包: {info.path}\n\n警告:\n{warning}\n\n未记录/无法保证:\n{gaps}\n\n"
            "复现包不会自动执行；执行前需再次显式确认。"
        )
        box = QMessageBox(self)
        box.setWindowTitle("复现包已生成")
        box.setText(f"Run {info.run_id} 复现包已生成")
        box.setInformativeText(details)
        if info.sensitive_findings:
            box.setIcon(QMessageBox.Warning)
            box.setDetailedText(
                json.dumps(info.sensitive_findings, ensure_ascii=False, indent=2)
            )
        box.exec()

    def inspect_or_execute_package(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择复现包", "", "RunTrace 复现包 (*.zip)"
        )
        if not path:
            return
        try:
            info = ReplayManager(self.store).inspect_package(path)
        except ReplayError as exc:
            QMessageBox.critical(self, "复现包检查失败", str(exc))
            return
        manifest = info["manifest"]
        config = info.get("config") or {}
        warnings = "\n".join(f"• {x}" for x in manifest.get("warnings") or [])
        gaps = "\n".join(f"• {x}" for x in manifest.get("environment_gaps") or [])
        deps = "\n".join(
            f"• {dep.get('name')}: {dep.get('resolved_path') or '未解析'}"
            for dep in manifest.get("external_dependencies") or []
        )
        details = (
            f"命令: {config.get('command')}\n"
            f"参数: {json.dumps(config.get('args') or [], ensure_ascii=False)}\n"
            f"工作目录: {config.get('cwd')}\n"
            f"shell: {config.get('shell')}\n\n"
            f"校验: {'通过' if info['checksums_valid'] else '失败'}\n\n"
            f"警告:\n{warnings}\n\n未记录:\n{gaps}\n\n外部依赖:\n{deps}\n\n"
            "只有确认命令与目录安全后才应执行。"
        )
        box = QMessageBox(self)
        box.setWindowTitle("确认执行复现包")
        box.setIcon(QMessageBox.Warning)
        box.setText("复现包校验通过，是否执行？")
        box.setInformativeText(details)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        box.button(QMessageBox.Yes).setText("执行")
        box.button(QMessageBox.No).setText("取消")
        if box.exec() != QMessageBox.Yes:
            return
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.information(self, "运行中", "请先停止当前任务。")
            return
        self._clear_output()
        self.run_button.setEnabled(False)
        self.global_status.setText("复现执行中…")
        self.output_status.setText("复现包正在执行")
        self.right_tabs.setCurrentIndex(0)
        self.package_worker = PackageExecWorker(self.store, path, self)
        self.package_worker.output.connect(self._append_output)
        self.package_worker.completed.connect(self._on_package_completed)
        self.package_worker.failed.connect(self._on_package_failed)
        self.package_worker.finished.connect(self._on_package_thread_finished)
        self.package_worker.start()

    def _on_package_completed(self, result: dict[str, Any]) -> None:
        self.run_button.setEnabled(True)
        self.global_status.setText("就绪")
        self.output_status.setText(
            f"复现 {_status_label(result['status'])} · exit {result['exit_code']} · "
            f"{result['run_id']}"
        )
        self.refresh_history(select_id=result["run_id"])

    def _on_package_failed(self, error_text: str) -> None:
        self.run_button.setEnabled(True)
        self.global_status.setText("就绪")
        QMessageBox.critical(self, "复现执行异常", error_text[-3000:])

    def _on_package_thread_finished(self) -> None:
        if self.package_worker is not None:
            self.package_worker.deleteLater()
        self.package_worker = None

    # ---------- 模板 ----------
    def refresh_templates(self) -> None:
        current = self.template_combo.currentText()
        self.template_combo.clear()
        for tpl in self.store.list_templates():
            self.template_combo.addItem(tpl["name"])
        if current:
            index = self.template_combo.findText(current)
            if index >= 0:
                self.template_combo.setCurrentIndex(index)

    def save_current_template(self) -> None:
        try:
            config = self._collect_config()
            build_command(config)
        except Exception as exc:
            QMessageBox.warning(self, "模板校验失败", str(exc))
            return
        name = self.name_edit.text().strip()
        if not name:
            name, ok = QInputDialog.getText(self, "模板名称", "请输入模板名称:")
            if not ok or not name.strip():
                return
            name = name.strip()
        if self.store.get_template(name) is not None:
            if not _confirm(self, "覆盖模板", f"模板“{name}”已存在，是否覆盖？", yes_text="覆盖"):
                return
        try:
            self.store.save_template(
                name=name,
                cwd=config.cwd,
                command=config.command,
                args=config.args,
                env_overrides=config.env_overrides,
                timeout=config.timeout,
                shell=config.shell,
            )
        except Exception as exc:
            QMessageBox.critical(self, "保存模板失败", str(exc))
            return
        self.refresh_templates()
        self.template_combo.setCurrentText(name)
        QMessageBox.information(
            self, "模板已保存", f"模板“{name}”已保存；敏感环境值按安全策略脱敏。"
        )

    def load_template(self) -> None:
        name = self.template_combo.currentText()
        if not name:
            return
        try:
            config = self.store.config_from_template(name)
        except KeyError as exc:
            QMessageBox.warning(self, "加载模板失败", str(exc))
            return
        tpl = self.store.get_template(name) or {}
        self._load_config_to_form(config)
        if any(value == "***" for value in tpl.get("env_overrides", {}).values()):
            QMessageBox.information(
                self,
                "模板已脱敏",
                "该模板包含已脱敏的环境变量值，运行前需重新填写。",
            )

    def copy_template(self) -> None:
        name = self.template_combo.currentText()
        if not name:
            return
        new_name, ok = QInputDialog.getText(self, "复制模板", "新模板名称:", text=f"{name} 副本")
        if not ok or not new_name.strip():
            return
        try:
            self.store.copy_template(name, new_name.strip())
        except Exception as exc:
            QMessageBox.critical(self, "复制模板失败", str(exc))
            return
        self.refresh_templates()
        self.template_combo.setCurrentText(new_name.strip())

    def delete_template(self) -> None:
        name = self.template_combo.currentText()
        if not name:
            return
        if not _confirm(self, "删除模板", f"确定删除模板“{name}”？", yes_text="删除"):
            return
        self.store.delete_template(name)
        self.refresh_templates()

    # ---------- 关闭 ----------
    def closeEvent(self, event) -> None:
        if self.worker is not None and self.worker.isRunning():
            if not _confirm(
                self, "任务仍在运行", "任务仍在运行，是否终止并退出？", yes_text="终止并退出",
                icon=QMessageBox.Warning,
            ):
                event.ignore()
                return
            self.worker.stop()
            if not self.worker.wait(5000):
                QMessageBox.warning(self, "无法退出", "进程树尚未终止，请稍后再试。")
                event.ignore()
                return
        if self.package_worker is not None and self.package_worker.isRunning():
            QMessageBox.information(self, "复现执行中", "请等待复现任务结束后再关闭。")
            event.ignore()
            return
        event.accept()


def launch(argv: list[str] | None = None) -> int:
    store = RunStore(default_data_dir() / "runtrace.db")
    recovered = recover_interrupted_runs(store)
    app = QApplication.instance()
    if app is None:
        app = QApplication([__file__, *(argv or [])])
    app.setApplicationName("RunTrace")
    app.setOrganizationName("RunTrace")
    window = MainWindow(store, recovered)
    window.show()
    try:
        return int(app.exec())
    finally:
        store.close()
