"""PySide6 图形界面（惰性导入：只有本模块会 import PySide6）。

功能：
- 选择根目录、排除规则、后台扫描、进度与取消；
- 概览 / 目录与文件 Top / 类型分布；
- 重复文件检测、清理建议；
- 快照对比；
- 隔离计划预览 -> 明确确认 -> 执行 -> 日志 -> 复核。

GUI 中不提供删除按钮，删除必须通过显式 API / CLI 手动开启。
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..diff_engine import compare_scans
from ..duplicates import find_duplicates_from_store
from ..executor import Executor, build_quarantine_plan
from ..models import DuplicateReport, Recommendation, ScanResult, human_size
from ..policy import (
    DEFAULT_EXCLUDE_DIR_NAMES,
    ExcludeRules,
    add_watch_target,
    default_db_path,
    evaluate_watch_targets,
    generate_recommendations,
)
from ..scanner import ScanOptions, Scanner
from ..snapshot_store import SnapshotStore


class ScanWorker(QThread):
    progress = Signal(dict)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, db_path: str, root: str, options: ScanOptions, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.db_path = db_path
        self.root = root
        self.options = options
        self.cancel_event = threading.Event()

    def run(self) -> None:  # noqa: D401
        store: Optional[SnapshotStore] = None
        try:
            store = SnapshotStore(self.db_path)
            scanner = Scanner(
                self.root,
                store,
                options=self.options,
                cancel_event=self.cancel_event,
                progress_callback=self.progress.emit,
            )
            result = scanner.scan()
            self.finished_ok.emit(result)
        except Exception as exc:  # pragma: no cover - GUI 环境
            self.failed.emit(f"{type(exc).__name__}: {exc}")
        finally:
            if store is not None:
                store.close()

    def cancel(self) -> None:
        self.cancel_event.set()


class TaskWorker(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, task: Callable[[], Any], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.task = task

    def run(self) -> None:  # pragma: no cover - GUI 环境
        try:
            self.done.emit(self.task())
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}")


class MainWindow(QMainWindow):
    def __init__(self, db_path: str) -> None:
        super().__init__()
        self.db_path = db_path
        self.state_dir = Path(db_path).parent
        self.store = SnapshotStore(db_path)
        self.current_scan_id: Optional[str] = None
        self.recommendations: list[Recommendation] = []
        self.duplicate_report: Optional[DuplicateReport] = None
        self.scan_worker: Optional[ScanWorker] = None
        self.task_worker: Optional[TaskWorker] = None

        self.setWindowTitle("SpacePilot - 磁盘空间分析与安全治理")
        self.resize(1280, 860)
        self._build_ui()
        self._refresh_snapshots()
        self._refresh_latest()

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)

        title = QLabel("SpacePilot")
        title.setStyleSheet("font-size: 22px; font-weight: 600; color: #1f4e79;")
        subtitle = QLabel("先发现问题 → 再解释原因 → 最后提出方案；默认只读，所有删除/移动/修改必须明确确认。")
        subtitle.setStyleSheet("color: #666;")
        outer.addWidget(title)
        outer.addWidget(subtitle)

        form = QGridLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(8)
        form.addWidget(QLabel("扫描根目录"), 0, 0)
        self.root_edit = QLineEdit()
        self.root_edit.setPlaceholderText("例如 C:\\Users\\你的名字\\Downloads")
        form.addWidget(self.root_edit, 0, 1, 1, 3)
        self.browse_button = QPushButton("浏览…")
        self.browse_button.clicked.connect(self._browse_root)
        form.addWidget(self.browse_button, 0, 4)
        self.scan_button = QPushButton("开始扫描")
        self.scan_button.setDefault(True)
        self.scan_button.clicked.connect(self._start_scan)
        form.addWidget(self.scan_button, 0, 5)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel_scan)
        form.addWidget(self.cancel_button, 0, 6)

        form.addWidget(QLabel("排除目录"), 1, 0)
        self.exclude_dirs_edit = QLineEdit(",".join(DEFAULT_EXCLUDE_DIR_NAMES))
        self.exclude_dirs_edit.setToolTip("逗号分隔的目录名，大小写不敏感")
        form.addWidget(self.exclude_dirs_edit, 1, 1, 1, 2)
        form.addWidget(QLabel("排除后缀"), 1, 3)
        self.exclude_suffix_edit = QLineEdit(".tmp,.log")
        self.exclude_suffix_edit.setToolTip("逗号分隔，例如 .tmp,.log,.bak")
        form.addWidget(self.exclude_suffix_edit, 1, 4, 1, 2)

        self.min_size_spin = QSpinBox()
        self.min_size_spin.setRange(0, 2_000_000_000)
        self.min_size_spin.setSingleStep(1024)
        self.min_size_spin.setSuffix(" B")
        self.min_size_spin.setToolTip("忽略小于该大小的文件（0 表示不过滤）")
        form.addWidget(QLabel("最小文件大小"), 2, 0)
        form.addWidget(self.min_size_spin, 2, 1)
        self.hash_check = QCheckBox("计算 SHA-256（对比改名/移动需要）")
        form.addWidget(self.hash_check, 2, 2, 1, 2)
        self.follow_check = QCheckBox("跟随符号链接（默认关闭）")
        form.addWidget(self.follow_check, 2, 4, 1, 2)
        outer.addLayout(form)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        outer.addWidget(self.progress_bar)
        self.progress_label = QLabel("就绪。选择目录后开始扫描。")
        self.progress_label.setStyleSheet("color: #555;")
        outer.addWidget(self.progress_label)

        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)
        self._build_overview_tab()
        self._build_tree_tab()
        self._build_types_tab()
        self._build_duplicates_tab()
        self._build_suggestions_tab()
        self._build_compare_tab()

        self.statusBar().showMessage("默认只读模式")

    def _table(self, headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        return table

    def _build_overview_tab(self) -> None:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.summary_label = QLabel("尚未扫描。")
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.summary_label.setStyleSheet(
            "background: #f4f7fb; border: 1px solid #d8e2ef; border-radius: 6px; padding: 8px;"
        )
        layout.addWidget(self.summary_label)

        splitter = QSplitter(Qt.Orientation.Vertical)
        dirs_box = QWidget()
        dirs_layout = QVBoxLayout(dirs_box)
        dirs_layout.setContentsMargins(0, 0, 0, 0)
        dirs_layout.addWidget(QLabel("目录聚合大小（Top）"))
        self.dirs_table = self._table(["目录（相对路径）", "大小", "文件数"])
        dirs_layout.addWidget(self.dirs_table)
        files_box = QWidget()
        files_layout = QVBoxLayout(files_box)
        files_layout.setContentsMargins(0, 0, 0, 0)
        files_layout.addWidget(QLabel("最大文件（Top）"))
        self.files_table = self._table(["文件（相对路径）", "大小", "状态"])
        files_layout.addWidget(self.files_table)
        splitter.addWidget(dirs_box)
        splitter.addWidget(files_box)
        splitter.setSizes([330, 330])
        layout.addWidget(splitter, 1)
        self.tabs.addTab(widget, "概览")

    def _build_tree_tab(self) -> None:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        hint = QLabel("目录树按“目录 + 所有子目录内文件”聚合大小；展开可逐层查看空间去向。")
        hint.setStyleSheet("color: #666;")
        layout.addWidget(hint)
        self.dir_tree = QTreeWidget()
        self.dir_tree.setHeaderLabels(["目录", "大小", "文件数"])
        self.dir_tree.setColumnWidth(0, 620)
        self.dir_tree.setColumnWidth(1, 130)
        self.dir_tree.setColumnWidth(2, 100)
        self.dir_tree.setAlternatingRowColors(True)
        layout.addWidget(self.dir_tree, 1)
        self.tabs.addTab(widget, "目录树")

    def _build_types_tab(self) -> None:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.types_table = self._table(["后缀", "总大小", "文件数"])
        layout.addWidget(self.types_table)
        self.tabs.addTab(widget, "类型分布")

    def _build_duplicates_tab(self) -> None:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("最小大小"))
        self.dup_min_spin = QSpinBox()
        self.dup_min_spin.setRange(1, 2_000_000_000)
        self.dup_min_spin.setValue(4096)
        self.dup_min_spin.setSingleStep(1024)
        self.dup_min_spin.setSuffix(" B")
        bar.addWidget(self.dup_min_spin)
        self.dup_button = QPushButton("检测重复文件")
        self.dup_button.clicked.connect(self._detect_duplicates)
        bar.addWidget(self.dup_button)
        bar.addStretch(1)
        layout.addLayout(bar)
        self.dup_summary = QLabel("尚未检测。重复 = 大小相同且 SHA-256 相同；硬链接不计入可回收空间。")
        self.dup_summary.setWordWrap(True)
        layout.addWidget(self.dup_summary)
        self.dup_table = self._table(["大小", "文件数", "可回收", "硬链接", "SHA-256", "示例路径"])
        layout.addWidget(self.dup_table, 1)
        self.tabs.addTab(widget, "重复文件")

    def _build_suggestions_tab(self) -> None:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        bar = QHBoxLayout()
        self.suggest_button = QPushButton("生成清理建议")
        self.suggest_button.clicked.connect(self._generate_suggestions)
        bar.addWidget(self.suggest_button)
        self.quarantine_button = QPushButton("隔离选中项…")
        self.quarantine_button.setEnabled(False)
        self.quarantine_button.clicked.connect(self._quarantine_selected)
        bar.addWidget(self.quarantine_button)
        bar.addStretch(1)
        layout.addLayout(bar)
        self.suggest_summary = QLabel("建议会给出证据、收益与风险等级；未知文件只建议复核。")
        self.suggest_summary.setWordWrap(True)
        layout.addWidget(self.suggest_summary)
        self.suggest_table = self._table(["风险", "动作", "分类", "收益", "路径", "证据"])
        layout.addWidget(self.suggest_table, 1)
        self.tabs.addTab(widget, "清理建议")

    def _build_compare_tab(self) -> None:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("旧快照"))
        self.old_combo = QComboBox()
        bar.addWidget(self.old_combo, 1)
        bar.addWidget(QLabel("新快照"))
        self.new_combo = QComboBox()
        bar.addWidget(self.new_combo, 1)
        self.compare_button = QPushButton("比较")
        self.compare_button.clicked.connect(self._compare_snapshots)
        bar.addWidget(self.compare_button)
        self.watch_button = QPushButton("观察目录…")
        self.watch_button.clicked.connect(self._add_watch_target)
        bar.addWidget(self.watch_button)
        layout.addLayout(bar)
        self.compare_summary = QLabel("需要至少两个快照。移动/改名需要快照包含 SHA-256。")
        self.compare_summary.setWordWrap(True)
        layout.addWidget(self.compare_summary)
        self.compare_table = self._table(["变化", "路径", "大小变化", "证据"])
        layout.addWidget(self.compare_table, 1)
        self.tabs.addTab(widget, "快照对比")

    # ------------------------------------------------------------------ 扫描
    def _browse_root(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择扫描根目录", self.root_edit.text() or "")
        if directory:
            self.root_edit.setText(directory)

    def _collect_rules(self) -> ExcludeRules:
        base = ExcludeRules()
        dirs = {part.strip() for part in self.exclude_dirs_edit.text().split(",") if part.strip()}
        suffixes = {part.strip() for part in self.exclude_suffix_edit.text().split(",") if part.strip()}
        return ExcludeRules(
            dir_names=base.dir_names | frozenset(dirs),
            suffixes=base.suffixes | frozenset(suffixes),
            min_file_size=self.min_size_spin.value(),
        )

    def _start_scan(self) -> None:
        root = self.root_edit.text().strip()
        if not root or not os.path.isdir(root):
            QMessageBox.warning(self, "无法开始", "请选择一个存在的目录作为扫描根目录。")
            return
        if self.scan_worker is not None and self.scan_worker.isRunning():
            return

        options = ScanOptions(
            rules=self._collect_rules(),
            follow_symlinks=self.follow_check.isChecked(),
            hash_files=self.hash_check.isChecked(),
        )
        self.scan_worker = ScanWorker(self.db_path, root, options, self)
        self.scan_worker.progress.connect(self._on_scan_progress)
        self.scan_worker.finished_ok.connect(self._on_scan_finished)
        self.scan_worker.failed.connect(self._on_scan_failed)
        self.scan_worker.finished.connect(self._on_scan_thread_done)

        self._set_scanning(True)
        self.progress_bar.setRange(0, 0)
        self.progress_label.setText("正在扫描…")
        self.scan_worker.start()

    def _cancel_scan(self) -> None:
        if self.scan_worker is not None:
            self.scan_worker.cancel()
            self.cancel_button.setEnabled(False)
            self.progress_label.setText("正在取消，已扫描结果会保存为部分快照…")

    def _set_scanning(self, scanning: bool) -> None:
        self.scan_button.setEnabled(not scanning)
        self.cancel_button.setEnabled(scanning)
        self.browse_button.setEnabled(not scanning)
        self.root_edit.setEnabled(not scanning)

    def _on_scan_progress(self, payload: dict[str, Any]) -> None:
        self.progress_label.setText(
            f"已处理 {payload.get('processed', 0)} 项 | 文件 {payload.get('files_scanned', 0)} | "
            f"目录 {payload.get('dirs_scanned', 0)} | 大小 {human_size(payload.get('total_size', 0))} | "
            f"跳过 {payload.get('skipped', 0)} | 失败 {payload.get('failed', 0)}"
        )

    def _on_scan_finished(self, result: ScanResult) -> None:
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.current_scan_id = result.scan_id
        self._load_scan(result.scan_id)
        self._refresh_snapshots()
        self.statusBar().showMessage(f"扫描完成: {result.scan_id[:12]}")
        self.progress_label.setText(
            f"扫描完成：文件 {result.stats.files_scanned}，大小 {human_size(result.stats.total_size)}，"
            f"失败 {result.stats.failed}，完整度 {result.stats.completeness_label}"
        )

    def _on_scan_failed(self, message: str) -> None:
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_label.setText("扫描失败。")
        QMessageBox.critical(self, "扫描失败", message)

    def _on_scan_thread_done(self) -> None:
        self._set_scanning(False)
        self.scan_worker = None

    # ------------------------------------------------------------------ 数据展示
    def _refresh_snapshots(self) -> None:
        scans = self.store.list_scans(limit=50)
        for combo in (self.old_combo, self.new_combo):
            combo.blockSignals(True)
            combo.clear()
            for scan in scans:
                combo.addItem(
                    f"{scan.scan_id[:10]} | {scan.started_at} | {human_size(scan.stats.total_size)} | {scan.root}",
                    scan.scan_id,
                )
            combo.blockSignals(False)
        if scans:
            self.new_combo.setCurrentIndex(0)
            self.old_combo.setCurrentIndex(1 if len(scans) > 1 else 0)

    def _refresh_latest(self) -> None:
        latest = self.store.latest_scan()
        if latest is not None:
            self.current_scan_id = latest.scan_id
            self._load_scan(latest.scan_id)

    def _load_scan(self, scan_id: str) -> None:
        scan = self.store.get_scan(scan_id)
        if scan is None:
            return
        stats = scan.stats
        self.summary_label.setText(
            f"快照 {scan.scan_id[:12]}    根目录: {scan.root}\n"
            f"时间: {scan.started_at} → {scan.finished_at}    耗时: {stats.duration_seconds:.2f}s\n"
            f"文件: {stats.files_scanned} 个（{human_size(stats.total_size)}）    "
            f"目录: {stats.dirs_scanned} 个    跳过: {stats.skipped}    失败: {stats.failed}    "
            f"0 字节: {stats.zero_byte}    volatile: {stats.volatile}    完整度: {stats.completeness_label}"
        )

        dirs = self.store.largest_dirs(scan_id, limit=50)
        self.dirs_table.setRowCount(len(dirs))
        for row, item in enumerate(dirs):
            values = [item["rel_path"] or "(根目录)", item["size_human"], str(item["file_count"])]
            for col, value in enumerate(values):
                self.dirs_table.setItem(row, col, QTableWidgetItem(value))

        files = self.store.largest_files(scan_id, limit=50)
        self.files_table.setRowCount(len(files))
        for row, item in enumerate(files):
            values = [item["rel_path"], item["size_human"], "volatile" if item["volatile"] else "稳定"]
            for col, value in enumerate(values):
                self.files_table.setItem(row, col, QTableWidgetItem(value))

        types = self.store.extension_stats(scan_id, limit=50)
        self.types_table.setRowCount(len(types))
        for row, item in enumerate(types):
            values = [item["suffix"], item["total_size_human"], str(item["file_count"])]
            for col, value in enumerate(values):
                self.types_table.setItem(row, col, QTableWidgetItem(value))

        self._load_dir_tree(scan_id)

    def _load_dir_tree(self, scan_id: str) -> None:
        self.dir_tree.clear()
        dirs = list(self.store.iter_dirs(scan_id))
        if not dirs:
            return
        root_record = next((record for record in dirs if record.rel_path == ""), dirs[0])
        root_item = QTreeWidgetItem(
            [root_record.path or "(根目录)", human_size(root_record.size), str(root_record.file_count)]
        )
        root_item.setToolTip(0, root_record.path)
        self.dir_tree.addTopLevelItem(root_item)
        items: dict[str, QTreeWidgetItem] = {"": root_item}
        for record in dirs:
            if record.rel_path == "":
                continue
            parent_rel = record.rel_path.rsplit("/", 1)[0] if "/" in record.rel_path else ""
            parent = items.get(parent_rel, root_item)
            name = record.rel_path.rsplit("/", 1)[-1]
            label = f"{name} [失败]" if record.status == "failed" else name
            item = QTreeWidgetItem([label, human_size(record.size), str(record.file_count)])
            item.setToolTip(0, record.path)
            parent.addChild(item)
            items[record.rel_path] = item
        root_item.setExpanded(True)

    # ------------------------------------------------------------------ 重复
    def _detect_duplicates(self) -> None:
        if not self.current_scan_id:
            QMessageBox.information(self, "提示", "请先完成一次扫描。")
            return
        scan_id = self.current_scan_id
        min_size = self.dup_min_spin.value()
        db_path = self.db_path
        self.dup_button.setEnabled(False)

        def task() -> DuplicateReport:
            store = SnapshotStore(db_path)
            try:
                return find_duplicates_from_store(store, scan_id, min_size=min_size)
            finally:
                store.close()

        def done(report: DuplicateReport) -> None:
            self.dup_button.setEnabled(True)
            self.duplicate_report = report
            self.dup_summary.setText(
                f"扫描文件 {report.scanned_files}，候选 {report.candidate_files}，"
                f"重复组 {len(report.groups)}，潜在可回收空间 {human_size(report.total_reclaimable)}，"
                f"无法读取 {len(report.errors)} 个文件。"
            )
            self.dup_table.setRowCount(len(report.groups))
            for row, group in enumerate(report.groups):
                example = group.files[0].rel_path if group.files else ""
                values = [
                    human_size(group.size),
                    str(len(group.files)),
                    human_size(group.reclaimable),
                    str(group.hardlink_skipped),
                    group.sha256[:16] + "…",
                    example,
                ]
                for col, value in enumerate(values):
                    self.dup_table.setItem(row, col, QTableWidgetItem(value))

        self._run_task(task, done, self.dup_button)

    # ------------------------------------------------------------------ 建议
    def _generate_suggestions(self) -> None:
        if not self.current_scan_id:
            QMessageBox.information(self, "提示", "请先完成一次扫描。")
            return
        scan_id = self.current_scan_id
        db_path = self.db_path
        self.suggest_button.setEnabled(False)
        self.quarantine_button.setEnabled(False)

        def task() -> tuple[DuplicateReport, list[Recommendation]]:
            store = SnapshotStore(db_path)
            try:
                report = find_duplicates_from_store(store, scan_id, min_size=4096)
                recs = generate_recommendations(store, scan_id, duplicates=report)
                return report, recs
            finally:
                store.close()

        def done(payload: tuple[DuplicateReport, list[Recommendation]]) -> None:
            self.suggest_button.setEnabled(True)
            report, recs = payload
            self.duplicate_report = report
            self.recommendations = recs
            self.quarantine_button.setEnabled(bool(recs))
            total = sum(rec.benefit_bytes for rec in recs)
            self.suggest_summary.setText(
                f"共 {len(recs)} 条建议，合计潜在收益 {human_size(total)}。"
                "隔离会保留原文件；删除不可撤销，GUI 不提供删除。"
            )
            self.suggest_table.setRowCount(len(recs))
            for row, rec in enumerate(recs):
                values = [rec.risk, rec.action, rec.category, human_size(rec.benefit_bytes), rec.path, rec.evidence]
                for col, value in enumerate(values):
                    self.suggest_table.setItem(row, col, QTableWidgetItem(value))

        self._run_task(task, done, self.suggest_button)

    def _quarantine_selected(self) -> None:
        if not self.current_scan_id:
            return
        rows = sorted({index.row() for index in self.suggest_table.selectedIndexes()})
        if not rows:
            QMessageBox.information(self, "提示", "请先在建议表中选择要隔离的文件行。")
            return
        selected = [self.recommendations[row] for row in rows if row < len(self.recommendations)]
        scan = self.store.get_scan(self.current_scan_id)
        if scan is None or not selected:
            return
        plan = build_quarantine_plan(
            [rec.path for rec in selected],
            quarantine_root=self.state_dir / "quarantine",
            root=scan.root,
            reasons={rec.path: rec.reason for rec in selected},
            risks={rec.path: rec.risk for rec in selected},
        )
        box = QMessageBox(self)
        box.setWindowTitle("确认隔离操作")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText("以下操作只复制文件到隔离目录，原文件保持不变。必须明确确认后才会执行：")
        box.setInformativeText("\n".join(plan.preview_lines()))
        box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        box.button(QMessageBox.StandardButton.Yes).setText("确认隔离")
        box.button(QMessageBox.StandardButton.No).setText("取消")
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        executor = Executor(
            quarantine_root=self.state_dir / "quarantine",
            log_path=self.state_dir / "executions.jsonl",
        )
        try:
            result = executor.execute(plan, confirm=True, token=plan.token)
        except Exception as exc:
            QMessageBox.critical(self, "执行失败", f"{type(exc).__name__}: {exc}")
            return
        QMessageBox.information(
            self,
            "隔离完成",
            f"执行 {result.execution_id}：成功 {result.succeeded}，失败 {result.failed}。\n"
            f"日志: {result.log_path}\n回滚仅对隔离副本有效。",
        )

    # ------------------------------------------------------------------ 对比
    def _add_watch_target(self) -> None:
        latest = self.store.latest_scan()
        if latest is None:
            QMessageBox.information(self, "提示", "请先完成一次扫描，再设置观察目标。")
            return
        rel_path, ok = QInputDialog.getText(
            self, "观察目录", "相对扫描根目录的路径（留空表示根目录）："
        )
        if not ok:
            return
        threshold_mb, ok = QInputDialog.getDouble(
            self, "增长阈值", "目录增长超过多少 MB 时提醒：", 100.0, 0.1, 1_000_000.0, 1
        )
        if not ok:
            return
        try:
            target = add_watch_target(
                self.store, rel_path, int(threshold_mb * 1024 * 1024), scan_id=latest.scan_id
            )
        except ValueError as exc:
            QMessageBox.warning(self, "无法设置", str(exc))
            return
        QMessageBox.information(
            self,
            "已设置观察目标",
            f"{target.rel_path or '根目录'} 基线 {human_size(target.baseline_bytes)}，"
            f"阈值 {threshold_mb:.1f} MB；对比后会在摘要中提示是否超过阈值。",
        )

    def _compare_snapshots(self) -> None:
        old_id = self.old_combo.currentData()
        new_id = self.new_combo.currentData()
        if not old_id or not new_id or old_id == new_id:
            QMessageBox.information(self, "提示", "请选择两个不同的快照。")
            return
        try:
            diff = compare_scans(self.store, old_id, new_id)
        except Exception as exc:
            QMessageBox.critical(self, "对比失败", f"{type(exc).__name__}: {exc}")
            return
        counts = diff.counts
        self.compare_summary.setText(
            f"新增 {counts.get('added', 0)}，删除 {counts.get('deleted', 0)}，"
            f"变大 {counts.get('grown', 0)}，变小 {counts.get('shrunk', 0)}，"
            f"内容修改 {counts.get('modified', 0)}，移动 {counts.get('moved', 0)}，"
            f"改名 {counts.get('renamed', 0)}；净变化 {human_size(diff.net_change)}。\n"
            + ("；".join(f"{note}" for note in diff.notes) if diff.notes else "")
        )
        alerts = [
            alert
            for alert in evaluate_watch_targets(self.store, new_id)
            if alert.growth >= alert.threshold_bytes
        ]
        if alerts:
            summary = self.compare_summary.text()
            detail = "；".join(
                f"{alert.rel_path or '根目录'} 增长 {human_size(alert.growth)}"
                for alert in alerts
            )
            self.compare_summary.setText(summary + f"\n观察目标超过阈值：{detail}")
        changes = sorted(diff.changes, key=lambda c: -abs(c.delta))
        self.compare_table.setRowCount(len(changes))
        for row, change in enumerate(changes):
            path = change.new_path or change.old_path or change.rel_path
            values = [
                change.change_type,
                path,
                f"{human_size(change.old_size)} → {human_size(change.new_size)}",
                change.evidence,
            ]
            for col, value in enumerate(values):
                self.compare_table.setItem(row, col, QTableWidgetItem(value))

    # ------------------------------------------------------------------ 任务线程
    def _run_task(self, task: Callable[[], Any], done: Callable[[Any], None], button: QPushButton) -> None:
        if self.task_worker is not None and self.task_worker.isRunning():
            QMessageBox.information(self, "提示", "已有后台任务在运行，请稍候。")
            return
        self.task_worker = TaskWorker(task, self)
        self.task_worker.done.connect(done)
        self.task_worker.failed.connect(lambda msg: QMessageBox.critical(self, "任务失败", msg))
        self.task_worker.finished.connect(lambda: button.setEnabled(True))
        self.task_worker.finished.connect(self._clear_task_worker)
        self.task_worker.start()

    def _clear_task_worker(self) -> None:
        self.task_worker = None

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.scan_worker is not None and self.scan_worker.isRunning():
            self.scan_worker.cancel()
            self.scan_worker.wait(3000)
        if self.task_worker is not None and self.task_worker.isRunning():
            self.task_worker.wait(3000)
        self.store.close()
        super().closeEvent(event)


def run_gui(db_path: Optional[str] = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("SpacePilot")
    app.setOrganizationName("SpacePilot")
    app.setStyle("Fusion")
    window = MainWindow(db_path or os.environ.get("SPACEPILOT_DB") or str(default_db_path()))
    window.show()
    return int(app.exec())
