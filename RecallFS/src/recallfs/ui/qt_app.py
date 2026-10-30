"""PySide6 图形界面（惰性导入：只有本模块才 import PySide6）。

功能：
* 多根目录登记、增量索引、暂停/继续/取消、进度显示；
* 全文 / 语义（本地哈希向量）检索，扩展名/集合/符号/失效片段筛选；
* 结果列表 + 证据面板（真实路径、行号/页码、索引时间、stale 状态）；
* 打开原始文件、收藏、最近搜索、集合管理。

PySide6 缺失时本模块导入会失败，由 ``recallfs.__main__`` 捕获并回退 CLI。
"""

from __future__ import annotations

import html
import os
import sys
import time
from typing import Any

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..cli import Services, build_services
from ..fulltext_index import default_db_path
from ..project_collections import RelatedFiles
from ..summary import build_extract_summary, format_locator
from ..search_engine import SearchFilters, SearchHit


class MainWindow(QMainWindow):
    def __init__(self, db_path: str | None = None) -> None:
        super().__init__()
        self.setWindowTitle("RecallFS — 本地离线检索与证据定位")
        self.resize(1280, 820)
        self.services: Services = build_services(db_path or default_db_path())
        self.scheduler = self.services.scheduler
        self.last_result = None
        self._hits: list[SearchHit] = []
        self._was_running = False
        self._build_ui()
        self._refresh_collections()
        self._refresh_favorites()
        self._refresh_recent()
        self._refresh_status()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll_status)
        self.timer.start(400)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        search_row = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText('检索内容，支持 "短语" 与 -排除词 …')
        self.search_input.returnPressed.connect(self.run_search)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["全文检索", "本地向量（非语义模型）"])
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(1, 200)
        self.limit_spin.setValue(30)
        self.search_button = QPushButton("检索")
        self.search_button.clicked.connect(self.run_search)
        search_row.addWidget(QLabel("查询"))
        search_row.addWidget(self.search_input, 1)
        search_row.addWidget(self.mode_combo)
        search_row.addWidget(QLabel("条数"))
        search_row.addWidget(self.limit_spin)
        search_row.addWidget(self.search_button)
        layout.addLayout(search_row)

        filter_row = QHBoxLayout()
        self.ext_input = QLineEdit()
        self.ext_input.setPlaceholderText("扩展名，如 .py,.md")
        self.collection_combo = QComboBox()
        self.collection_combo.addItem("全部集合", None)
        self.symbol_input = QLineEdit()
        self.symbol_input.setPlaceholderText("代码符号，如 parse_query")
        self.status_combo = QComboBox()
        self.status_combo.addItem("全部状态", None)
        for status in ("ok", "unsupported", "error", "incomplete"):
            self.status_combo.addItem(status, status)
        self.include_stale = QCheckBox("包含失效片段")
        filter_row.addWidget(QLabel("扩展名"))
        filter_row.addWidget(self.ext_input)
        filter_row.addWidget(QLabel("集合"))
        filter_row.addWidget(self.collection_combo)
        filter_row.addWidget(QLabel("符号"))
        filter_row.addWidget(self.symbol_input)
        filter_row.addWidget(self.status_combo)
        filter_row.addWidget(self.include_stale)
        layout.addLayout(filter_row)

        splitter = QSplitter(Qt.Horizontal)
        self.tabs = QTabWidget()
        self.results_table = QTableWidget(0, 5)
        self.results_table.setHorizontalHeaderLabels(["路径", "位置", "层次", "类型", "状态"])
        self.results_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.results_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.results_table.itemSelectionChanged.connect(self._show_selected_hit)
        self.results_table.itemDoubleClicked.connect(lambda *_: self.open_selected_file())
        self.tabs.addTab(self.results_table, "搜索结果")

        collections_tab = QWidget()
        collections_layout = QVBoxLayout(collections_tab)
        self.collections_list = QListWidget()
        collections_layout.addWidget(self.collections_list)
        col_buttons = QHBoxLayout()
        create_collection = QPushButton("新建集合")
        create_collection.clicked.connect(self.create_collection)
        add_collection_root = QPushButton("添加根目录到集合")
        add_collection_root.clicked.connect(self.add_root_to_collection)
        col_buttons.addWidget(create_collection)
        col_buttons.addWidget(add_collection_root)
        collections_layout.addLayout(col_buttons)
        self.tabs.addTab(collections_tab, "集合")

        favorites_tab = QWidget()
        favorites_layout = QVBoxLayout(favorites_tab)
        self.favorites_list = QListWidget()
        favorites_layout.addWidget(self.favorites_list)
        fav_buttons = QHBoxLayout()
        fav_query = QPushButton("收藏当前查询")
        fav_query.clicked.connect(self.favorite_query)
        fav_file = QPushButton("收藏选中文件")
        fav_file.clicked.connect(self.favorite_file)
        fav_remove = QPushButton("删除收藏")
        fav_remove.clicked.connect(self.remove_favorite)
        fav_buttons.addWidget(fav_query)
        fav_buttons.addWidget(fav_file)
        fav_buttons.addWidget(fav_remove)
        favorites_layout.addLayout(fav_buttons)
        self.tabs.addTab(favorites_tab, "收藏")

        recent_tab = QWidget()
        recent_layout = QVBoxLayout(recent_tab)
        self.recent_list = QListWidget()
        self.recent_list.itemDoubleClicked.connect(self._use_recent)
        recent_layout.addWidget(self.recent_list)
        recent_buttons = QHBoxLayout()
        clear_recent = QPushButton("清空最近搜索")
        clear_recent.clicked.connect(self.clear_recent)
        recent_buttons.addWidget(clear_recent)
        recent_layout.addLayout(recent_buttons)
        self.tabs.addTab(recent_tab, "最近搜索")

        self.tabs.setCurrentWidget(self.results_table)
        splitter.addWidget(self.tabs)

        detail_panel = QWidget()
        detail_layout = QVBoxLayout(detail_panel)
        self.detail = QTextBrowser()
        self.detail.setOpenExternalLinks(False)
        self.detail.setHtml("<p style='color:#666'>选中结果查看证据片段与文件状态。</p>")
        detail_layout.addWidget(self.detail, 1)
        detail_buttons = QHBoxLayout()
        open_button = QPushButton("打开原文件")
        open_button.clicked.connect(self.open_selected_file)
        summary_button = QPushButton("摘要")
        summary_button.clicked.connect(self.show_summary)
        related_button = QPushButton("相关文件")
        related_button.clicked.connect(self.show_related)
        copy_button = QPushButton("复制路径")
        copy_button.clicked.connect(self.copy_path)
        detail_buttons.addWidget(open_button)
        detail_buttons.addWidget(summary_button)
        detail_buttons.addWidget(related_button)
        detail_buttons.addWidget(copy_button)
        detail_layout.addLayout(detail_buttons)
        splitter.addWidget(detail_panel)
        splitter.setSizes([760, 520])
        layout.addWidget(splitter, 1)

        bottom = QHBoxLayout()
        self.add_root_button = QPushButton("添加根目录并索引")
        self.add_root_button.clicked.connect(self.add_root_and_index)
        self.scan_button = QPushButton("增量索引")
        self.scan_button.clicked.connect(lambda: self.start_indexing(None))
        self.pause_button = QPushButton("暂停")
        self.pause_button.clicked.connect(self.scheduler.pause)
        self.resume_button = QPushButton("继续")
        self.resume_button.clicked.connect(self.scheduler.resume)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self.scheduler.cancel)
        self.semantic_button = QPushButton("重建本地向量索引")
        self.semantic_button.clicked.connect(self.build_semantic)
        for widget in (
            self.add_root_button,
            self.scan_button,
            self.pause_button,
            self.resume_button,
            self.cancel_button,
            self.semantic_button,
        ):
            bottom.addWidget(widget)
        bottom.addStretch(1)
        self.progress = QProgressBar()
        self.progress.setMinimumWidth(200)
        bottom.addWidget(self.progress)
        layout.addLayout(bottom)

        self.status_label = QLabel("就绪")
        self.statusBar().addWidget(self.status_label)
        self.statusBar().addPermanentWidget(QLabel(f"索引库: {self.services.db.path}"))

    # ------------------------------------------------------------------
    # 索引控制
    # ------------------------------------------------------------------
    def add_root_and_index(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择要索引的根目录")
        if not directory:
            return
        try:
            self.services.scheduler.add_root(directory)
        except Exception as exc:
            QMessageBox.warning(self, "无法添加根目录", str(exc))
            return
        self.start_indexing([directory])

    def start_indexing(self, roots: list[str] | None) -> None:
        if self.scheduler.is_running():
            QMessageBox.information(self, "索引进行中", "当前已有索引任务正在运行。")
            return
        try:
            self.scheduler.start_async(roots)
        except Exception as exc:
            QMessageBox.warning(self, "无法启动索引", str(exc))
            return
        self._was_running = True
        self.status_label.setText("索引中…")

    def _poll_status(self) -> None:
        status = self.scheduler.status()
        state = status.get("state", "idle")
        running = self.scheduler.is_running()
        if state in ("running", "paused", "cancelling"):
            total = max(1, int(status.get("estimated_total") or 1))
            self.progress.setRange(0, total)
            self.progress.setValue(min(total, int(status.get("processed") or 0)))
            message = status.get("message") or state
            current = status.get("current_file")
            self.status_label.setText(f"{message}  {current or ''}")
        else:
            self.progress.setRange(0, 1)
            self.progress.setValue(1 if state == "completed" else 0)
            self.status_label.setText(status.get("message") or state)
        self.pause_button.setEnabled(running and state != "paused")
        self.resume_button.setEnabled(running and state == "paused")
        self.cancel_button.setEnabled(running)
        if self._was_running and not running:
            self._was_running = False
            self._refresh_status()
            self._refresh_collections()
        stats = status.get("last_stats")
        if stats and not running:
            self.status_label.setText(
                f"完成：新增 {stats['added']} 修改 {stats['modified']} 删除 {stats['deleted']} "
                f"移动 {stats['moved']} 跳过 {stats['skipped']} 耗时 {stats['duration_seconds']}s"
            )

    def _refresh_status(self) -> None:
        stats = self.services.db.stats()
        sem = self.services.semantic.stats()
        self.statusBar().showMessage(
            f"文件 {stats['files']} | chunk {stats['chunks']} | 本地向量覆盖率 {sem['coverage']} "
            f"({sem['label']})",
            8000,
        )

    def build_semantic(self) -> None:
        self.status_label.setText("正在构建本地哈希向量…")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            report = self.services.semantic.build(limit=20000)
        except Exception as exc:
            QMessageBox.warning(self, "构建失败", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()
        self._refresh_status()
        QMessageBox.information(
            self,
            "本地向量索引",
            f"模型: {report['model']} v{report['version']}\n"
            f"标注: {report['label']}\n"
            f"已索引 chunk: {report['indexed_chunks']} / {report['candidate_chunks']}\n"
            f"覆盖率: {report['coverage']}\n"
            "注意：这是本地哈希向量，不是神经网络语义模型，也不代表事实。",
        )

    # ------------------------------------------------------------------
    # 搜索
    # ------------------------------------------------------------------
    def _collect_filters(self) -> SearchFilters:
        exts = [part.strip() for part in self.ext_input.text().split(",") if part.strip()]
        collection = self.collection_combo.currentData()
        return SearchFilters(
            exts=exts or None,
            collections=[collection] if collection else None,
            symbols=[self.symbol_input.text().strip()] if self.symbol_input.text().strip() else None,
            parse_status=[self.status_combo.currentData()] if self.status_combo.currentData() else None,
            include_stale=self.include_stale.isChecked(),
        )

    def run_search(self) -> None:
        query = self.search_input.text().strip()
        mode = "semantic" if self.mode_combo.currentIndex() == 1 else "fulltext"
        try:
            result = self.services.engine.search(
                query, self._collect_filters(), mode=mode, limit=self.limit_spin.value()
            )
        except Exception as exc:
            QMessageBox.warning(self, "检索失败", str(exc))
            return
        self.last_result = result
        self._hits = result.hits
        self._fill_results(result)
        self._refresh_recent()

    def _fill_results(self, result) -> None:
        self.results_table.setRowCount(0)
        for hit in result.hits:
            row = self.results_table.rowCount()
            self.results_table.insertRow(row)
            path_item = QTableWidgetItem(hit.path)
            path_item.setData(Qt.UserRole, row)
            self.results_table.setItem(row, 0, path_item)
            location = f"第 {hit.page} 页" if hit.page else (
                f"L{hit.start_line}" if hit.start_line else "-"
            )
            self.results_table.setItem(row, 1, QTableWidgetItem(location))
            self.results_table.setItem(row, 2, QTableWidgetItem(hit.layer))
            self.results_table.setItem(row, 3, QTableWidgetItem(hit.kind))
            state = hit.parse_status if not hit.stale else "STALE"
            self.results_table.setItem(row, 4, QTableWidgetItem(state))
        if not result.hits:
            reason_map = {
                "empty_query": "查询为空",
                "empty_index": "索引为空，请先添加根目录并索引",
                "no_match": "无匹配结果",
                "all_results_stale": "命中的片段均已失效，请重新索引",
            }
            self.detail.setHtml(
                f"<p><b>空结果</b>：{reason_map.get(result.empty_reason or '', result.empty_reason)}</p>"
                + (f"<p>{html.escape(result.warning)}</p>" if result.warning else "")
            )
        else:
            self.results_table.selectRow(0)
        warning = f" | 提示: {result.warning}" if result.warning else ""
        self.status_label.setText(
            f"命中 {result.total} 条，显示 {len(result.hits)} 条，{result.elapsed_ms} ms，模式 {result.mode}{warning}"
        )

    def _current_hit(self) -> SearchHit | None:
        row = self.results_table.currentRow()
        if 0 <= row < len(self._hits):
            return self._hits[row]
        return None

    def _show_selected_hit(self) -> None:
        hit = self._current_hit()
        if hit is None:
            return
        evidence = self.services.engine.evidence(hit)
        location = f"第 {hit.page} 页" if hit.page else f"行 {hit.start_line}-{hit.end_line}"
        parts = [
            f"<h3>{html.escape(hit.name)}</h3>",
            "<table cellspacing='4'>",
            f"<tr><td><b>路径</b></td><td>{html.escape(hit.path)}</td></tr>",
            f"<tr><td><b>位置</b></td><td>{html.escape(location)}</td></tr>",
            f"<tr><td><b>类型/层次</b></td><td>{html.escape(hit.kind)} / {html.escape(hit.layer)}</td></tr>",
            f"<tr><td><b>索引时间</b></td><td>{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(hit.indexed_at or 0))}</td></tr>",
            f"<tr><td><b>解析状态</b></td><td>{html.escape(hit.parse_status)}</td></tr>",
            f"<tr><td><b>当前状态</b></td><td>{'已失效: ' + html.escape(hit.stale_reason or '') if hit.stale else '有效（mtime/size 未变）'}</td></tr>",
            f"<tr><td><b>证据校验</b></td><td>{'内容仍匹配当前文件' if evidence.get('content_verified') else html.escape(str(evidence.get('stale_reason') or '未校验内容'))}</td></tr>",
            "</table>",
            "<hr><b>命中片段（高亮）</b><br>",
            f"<div style='white-space:pre-wrap;font-family:Consolas,monospace'>{hit.snippet}</div>",
        ]
        if hit.parse_error:
            parts.append(f"<hr><b>解析说明</b><br>{html.escape(hit.parse_error)}")
        if evidence.get("live_lines"):
            lines = "<br>".join(
                f"<span style='color:#888'>{number:>5}</span> | {html.escape(text)}"
                for number, text in evidence["live_lines"]
            )
            parts.append(f"<hr><b>当前文件上下文</b><div style='font-family:Consolas,monospace'>{lines}</div>")
        parts.append(
            "<hr><p style='color:#777'>片段来自索引 chunk（kind=raw_text/metadata），"
            "语义模式仅表示向量相似度，不代表事实。</p>"
        )
        self.detail.setHtml("".join(parts))

    def open_selected_file(self) -> None:
        hit = self._current_hit()
        if hit is None:
            return
        if not os.path.exists(hit.path):
            QMessageBox.warning(self, "文件不存在", hit.path)
            return
        try:
            if os.name == "nt":
                os.startfile(hit.path)  # type: ignore[attr-defined]
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(hit.path))
        except Exception as exc:
            QMessageBox.warning(self, "无法打开文件", str(exc))
            return
        location = f"第 {hit.page} 页" if hit.page else f"第 {hit.start_line} 行"
        self.statusBar().showMessage(f"已打开 {hit.path}，定位 {location}", 6000)

    def copy_path(self) -> None:
        hit = self._current_hit()
        if hit is not None:
            QApplication.clipboard().setText(hit.path)
            self.statusBar().showMessage("路径已复制", 3000)

    def show_summary(self) -> None:
        hit = self._current_hit()
        if hit is None:
            return
        row = self.services.db.get_file(hit.file_id)
        if row is None:
            self.detail.setHtml("<p>文件记录不存在。</p>")
            return
        data = build_extract_summary(self.services.db, row, max_chars=600)
        if not data["excerpt"]:
            self.detail.setHtml(
                "<h3>文件摘要</h3><p>不可用："
                + html.escape(data["reason"] or "没有可提取的文本")
                + "</p>"
            )
            return
        locators = "；".join(format_locator(item) for item in data["locators"])
        body = html.escape(data["excerpt"]).replace("\n", "<br>")
        note = html.escape(data["note"])
        if data["truncated"]:
            note += "（已截断，完整内容请用“打开原文件”查看）"
        self.detail.setHtml(
            f"<h3>文件摘要（原文摘录）</h3>"
            f"<p style='white-space:pre-wrap'>{body}</p>"
            f"<p><b>定位:</b> {html.escape(locators)}</p>"
            f"<p><small>{note}</small></p>"
        )

    def show_related(self) -> None:
        hit = self._current_hit()
        if hit is None:
            return
        related = RelatedFiles(self.services.db).related(hit.file_id, limit=10)
        if not related:
            self.detail.setHtml("<p>未发现相关文件。</p>")
            return
        rows = []
        for item in related:
            reasons = "；".join(html.escape(reason) for reason in item["reasons"])
            rows.append(
                f"<li><b>{html.escape(item['name'])}</b>（score {item['score']}）<br>"
                f"<small>{html.escape(item['path'])}</small><br><small>依据: {reasons}</small></li>"
            )
        self.detail.setHtml("<h3>相关文件</h3><ul>" + "".join(rows) + "</ul>")

    # ------------------------------------------------------------------
    # 集合 / 收藏 / 最近
    # ------------------------------------------------------------------
    def _refresh_collections(self) -> None:
        current = self.collection_combo.currentData()
        self.collections_list.clear()
        self.collection_combo.clear()
        self.collection_combo.addItem("全部集合", None)
        try:
            items = self.services.collections.list()
        except Exception:
            items = []
        for item in items:
            text = f"{item['name']} [{item['kind']}] · {item['file_count']} 个文件"
            self.collections_list.addItem(text)
            self.collection_combo.addItem(item["name"], item["name"])
        index = self.collection_combo.findData(current)
        if index >= 0:
            self.collection_combo.setCurrentIndex(index)

    def create_collection(self) -> None:
        name, ok = QInputDialog.getText(self, "新建集合", "集合名称")
        if not ok or not name.strip():
            return
        kind, ok = QInputDialog.getItem(self, "集合类型", "类型", ["project", "course", "custom"], 2, False)
        if not ok:
            return
        try:
            self.services.collections.create(name.strip(), kind)
        except Exception as exc:
            QMessageBox.warning(self, "创建失败", str(exc))
            return
        self._refresh_collections()

    def add_root_to_collection(self) -> None:
        name, ok = QInputDialog.getText(self, "添加根目录", "集合名称")
        if not ok or not name.strip():
            return
        directory = QFileDialog.getExistingDirectory(self, "选择根目录")
        if not directory:
            return
        try:
            self.services.collections.add_root(name.strip(), directory)
        except Exception as exc:
            QMessageBox.warning(self, "添加失败", str(exc))
            return
        self._refresh_collections()

    def _refresh_favorites(self) -> None:
        self.favorites_list.clear()
        try:
            items = self.services.favorites.list()
        except Exception:
            items = []
        for item in items:
            text = f"#{item['id']} [{item['type']}] {item['path'] or item['query']}"
            list_item = QListWidgetItem(text)
            list_item.setData(Qt.UserRole, item["id"])
            self.favorites_list.addItem(list_item)

    def favorite_query(self) -> None:
        query = self.search_input.text().strip()
        if not query:
            return
        self.services.favorites.add_query(query)
        self._refresh_favorites()

    def favorite_file(self) -> None:
        hit = self._current_hit()
        if hit is None:
            return
        fav_id = self.services.favorites.add_file(hit.path)
        if fav_id is None:
            QMessageBox.warning(self, "收藏失败", "文件不在索引中")
            return
        self._refresh_favorites()

    def remove_favorite(self) -> None:
        item = self.favorites_list.currentItem()
        if item is None:
            return
        self.services.favorites.remove(int(item.data(Qt.UserRole)))
        self._refresh_favorites()

    def _refresh_recent(self) -> None:
        self.recent_list.clear()
        try:
            items = self.services.recent.list(30)
        except Exception:
            items = []
        for item in items:
            list_item = QListWidgetItem(f"{item['query']}  ({item['result_count']} 条)")
            list_item.setData(Qt.UserRole, item["query"])
            self.recent_list.addItem(list_item)

    def _use_recent(self, item: QListWidgetItem) -> None:
        self.search_input.setText(str(item.data(Qt.UserRole)))
        self.run_search()

    def clear_recent(self) -> None:
        self.services.recent.clear()
        self._refresh_recent()

    # ------------------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        try:
            if self.scheduler.is_running():
                self.scheduler.cancel()
                self.scheduler.wait(3.0)
            self.services.close()
        finally:
            super().closeEvent(event)


def run_gui(db_path: str | None = None) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow(db_path)
    window.show()
    return int(app.exec())
