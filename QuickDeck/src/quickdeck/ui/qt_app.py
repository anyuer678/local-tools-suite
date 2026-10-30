"""PySide6 GUI。此模块只在 ``run_gui`` 内惰性导入 Qt，避免无 GUI 环境崩溃。"""

from __future__ import annotations

import json
import sys
from typing import Any


def should_start_hidden(start_minimized: bool, behavior: str) -> bool:
    """是否在启动时隐藏主窗口。

    只有显式 --minimized 或启动行为为 background 时才隐藏；手动执行 `gui`
    应当可见。开机自启项本身带 --minimized，因此不受影响。
    """
    return bool(start_minimized) or behavior == "background"


def run_gui(context: Any | None = None, *, start_minimized: bool = False) -> int:
    from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot
    from PySide6.QtGui import QAction, QFont, QKeySequence, QShortcut
    from PySide6.QtWidgets import (
        QApplication,
        QComboBox,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QPushButton,
        QStyle,
        QSystemTrayIcon,
        QVBoxLayout,
        QWidget,
        QMenu,
    )

    from ..launcher_ui import (
        StartupError,
        build_context,
        get_startup_enabled,
        set_startup_enabled,
    )

    owns_context = context is None
    if context is None:
        context = build_context()

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("QuickDeck")
    app.setQuitOnLastWindowClosed(False)
    try:
        app.setStyle("Fusion")
    except Exception:
        pass
    font = QFont("Microsoft YaHei UI", 10)
    app.setFont(font)
    app.setStyleSheet(
        """
        QMainWindow, QWidget { background: #f5f7fb; color: #202636; }
        QLineEdit, QPlainTextEdit, QComboBox, QListWidget {
            background: #ffffff; border: 1px solid #d8deea; border-radius: 8px;
            padding: 6px; selection-background-color: #dbeafe;
        }
        QLineEdit:focus, QPlainTextEdit:focus, QListWidget:focus {
            border: 1px solid #3b82f6;
        }
        QPushButton {
            background: #2563eb; color: white; border: none; border-radius: 8px;
            padding: 8px 16px; font-weight: 600;
        }
        QPushButton:hover { background: #1d4ed8; }
        QPushButton:disabled { background: #9aa8bd; }
        QListWidget::item { padding: 7px; border-radius: 6px; }
        QListWidget::item:selected { background: #dbeafe; color: #1e3a8a; }
        QLabel#title { font-size: 20px; font-weight: 700; color: #1e3a8a; }
        QLabel#hint { color: #667085; }
        """
    )

    class HotkeyBridge(QObject):
        triggered = Signal()

    class ExecutionWorker(QObject):
        finished = Signal(str, bool)

        def __init__(self, registry, command_id, text, params):
            super().__init__()
            self.registry = registry
            self.command_id = command_id
            self.text = text
            self.params = params

        @Slot()
        def run(self) -> None:
            try:
                output = self.registry.execute(self.command_id, self.text, self.params)
                self.finished.emit(str(output), True)
            except Exception as exc:
                self.finished.emit(f"{type(exc).__name__}: {exc}", False)

    class SearchEdit(QLineEdit):
        navigate = Signal(int)
        submit = Signal()

        def keyPressEvent(self, event) -> None:  # type: ignore[no-untyped-def]
            if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                self.navigate.emit(1 if event.key() == Qt.Key.Key_Down else -1)
                event.accept()
                return
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.submit.emit()
                event.accept()
                return
            super().keyPressEvent(event)

    class QuickDeckWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self._commands = []
            self._threads: set[QThread] = set()
            # QThread 只持有 worker 的 C++ 所有权；必须保留 Python 引用，
            # 否则 worker 可能在 started 信号发出前被回收，线程将永远不退出。
            self._workers: set[ExecutionWorker] = set()
            self._tray: QSystemTrayIcon | None = None
            self._hotkey_manager = None
            self._hotkey_bridge: HotkeyBridge | None = None
            self._force_close = False
            self.setWindowTitle("QuickDeck")
            self.resize(760, 600)
            self.setMinimumSize(560, 430)

            central = QWidget()
            layout = QVBoxLayout(central)
            layout.setContentsMargins(18, 16, 18, 16)
            layout.setSpacing(10)

            header = QHBoxLayout()
            title = QLabel("QuickDeck")
            title.setObjectName("title")
            self.hotkey_label = QLabel(f"呼出快捷键：{context.settings.get('hotkey')}")
            self.hotkey_label.setObjectName("hint")
            header.addWidget(title)
            header.addStretch(1)
            header.addWidget(self.hotkey_label)
            layout.addLayout(header)

            search_row = QHBoxLayout()
            self.search = SearchEdit()
            self.search.setPlaceholderText("输入命令、别名或关键词，例如 calc / 文本 / json")
            self.category = QComboBox()
            self.category.addItem("全部分类", None)
            for category in context.registry.categories():
                self.category.addItem(category, category)
            search_row.addWidget(self.search, 1)
            search_row.addWidget(self.category)
            layout.addLayout(search_row)

            self.results = QListWidget()
            self.results.setMaximumHeight(175)
            layout.addWidget(self.results)

            self.input_edit = QPlainTextEdit()
            self.input_edit.setPlaceholderText("在这里输入要处理的文本（计算器可直接输入表达式）")
            self.input_edit.setMaximumHeight(105)
            layout.addWidget(self.input_edit)

            action_row = QHBoxLayout()
            self.params_edit = QLineEdit()
            self.params_edit.setPlaceholderText('可选参数 JSON，例如 {"mode":"upper"}')
            self.run_button = QPushButton("执行 (Ctrl+Enter)")
            action_row.addWidget(self.params_edit, 1)
            action_row.addWidget(self.run_button)
            layout.addLayout(action_row)

            self.output = QPlainTextEdit()
            self.output.setReadOnly(True)
            self.output.setPlaceholderText("执行结果")
            layout.addWidget(self.output, 1)

            hint = QLabel("↑/↓ 选择 · Enter 执行 · Esc 隐藏窗口 · 全局快捷键随时呼出")
            hint.setObjectName("hint")
            layout.addWidget(hint)
            self.setCentralWidget(central)
            self.statusBar().showMessage(
                f"插件目录：{context.plugins_dir}  ·  数据目录：{context.data_dir}"
            )

            self.search.textChanged.connect(self.refresh_results)
            self.search.navigate.connect(self.navigate_results)
            self.search.submit.connect(self.run_selected)
            self.category.currentIndexChanged.connect(self.refresh_results)
            self.results.itemClicked.connect(lambda _item: self.input_edit.setFocus())
            self.run_button.clicked.connect(self.run_selected)
            QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.run_selected)
            QShortcut(QKeySequence("Esc"), self, activated=self.hide)
            QShortcut(QKeySequence("Ctrl+Q"), self, activated=self.quit_app)

            self.clipboard_timer = QTimer(self)
            self.clipboard_timer.setInterval(1200)
            self.clipboard_timer.timeout.connect(self.poll_clipboard)
            if context.toolkit.clipboard.enabled:
                self.clipboard_timer.start()

            self.refresh_results("")

        def refresh_results(self, _text: str | None = None) -> None:
            category = self.category.currentData()
            limit = int(context.settings.get("search", {}).get("max_results", 30))
            try:
                commands = context.registry.search(
                    self.search.text(), category=category, limit=limit
                )
            except Exception as exc:
                self.statusBar().showMessage(f"搜索失败：{exc}")
                commands = []
            self._commands = commands
            self.results.clear()
            if not commands:
                item = QListWidgetItem("没有匹配的命令")
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                self.results.addItem(item)
                return
            for command in commands:
                subtitle = command.description or command.id
                item = QListWidgetItem(f"{command.name}   ·  {command.category}\n{subtitle}")
                item.setData(Qt.ItemDataRole.UserRole, command.id)
                item.setToolTip(f"{command.id}\n别名：{', '.join(command.aliases) or '无'}")
                self.results.addItem(item)
            self.results.setCurrentRow(0)

        def navigate_results(self, delta: int) -> None:
            if not self._commands:
                return
            row = self.results.currentRow()
            row = max(0, min(len(self._commands) - 1, row + delta))
            self.results.setCurrentRow(row)

        def selected_command(self):
            row = self.results.currentRow()
            if row < 0 or row >= len(self._commands):
                return None
            return self._commands[row]

        def parse_params(self) -> dict[str, Any] | None:
            raw = self.params_edit.text().strip()
            if not raw:
                return {}
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                QMessageBox.warning(self, "参数格式错误", f"参数必须是合法 JSON：{exc}")
                return None
            if not isinstance(value, dict):
                QMessageBox.warning(self, "参数格式错误", "参数 JSON 顶层必须是对象。")
                return None
            return value

        def run_selected(self) -> None:
            command = self.selected_command()
            if command is None:
                return
            params = self.parse_params()
            if params is None:
                return
            text = self.input_edit.toPlainText()
            if command.id == "calc.eval" and not text.strip():
                text = self.search.text().strip()
            if command.id == "command.run":
                try:
                    preview = context.toolkit.preview_external(params)
                except Exception as exc:
                    QMessageBox.warning(self, "命令配置错误", str(exc))
                    return
                argv_text = " ".join(preview["argv"])
                cwd_text = preview["cwd"]
                box = QMessageBox(self)
                box.setWindowTitle("确认执行外部命令")
                box.setIcon(QMessageBox.Icon.Question)
                box.setText(
                    f"即将执行：\n{argv_text}\n\n工作目录：{cwd_text}\n\n"
                    "命令以参数数组直接运行，不经过 shell。确认执行？"
                )
                box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                box.setDefaultButton(QMessageBox.StandardButton.No)
                box.button(QMessageBox.StandardButton.Yes).setText("执行")
                box.button(QMessageBox.StandardButton.No).setText("取消")
                if box.exec() != QMessageBox.StandardButton.Yes:
                    return
                params["confirm"] = True
            self.run_button.setEnabled(False)
            self.output.setPlainText("执行中…")
            self.statusBar().showMessage(f"正在执行 {command.name} …")
            thread = QThread(self)
            worker = ExecutionWorker(
                context.registry,
                command.id,
                text,
                params,
            )
            worker.moveToThread(thread)
            self._threads.add(thread)
            self._workers.add(worker)
            worker.finished.connect(lambda: self._workers.discard(worker))
            thread.started.connect(worker.run)
            worker.finished.connect(self.on_execution_finished)
            worker.finished.connect(thread.quit)
            thread.finished.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)
            thread.finished.connect(lambda: self._threads.discard(thread))
            thread.start()

        @Slot(str, bool)
        def on_execution_finished(self, output: str, ok: bool) -> None:
            self.output.setPlainText(output)
            self.run_button.setEnabled(True)
            self.statusBar().showMessage("执行完成" if ok else "执行失败，请查看结果")
            self.output.setStyleSheet("" if ok else "color: #b42318;")

        def poll_clipboard(self) -> None:
            if not context.toolkit.clipboard.enabled:
                return
            try:
                context.toolkit.clipboard.capture_once()
            except Exception as exc:
                self.statusBar().showMessage(f"读取剪贴板失败：{exc}")

        def show_deck(self) -> None:
            self.showNormal()
            self.raise_()
            self.activateWindow()
            self.search.setFocus()
            self.search.selectAll()

        def closeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
            if not self._force_close and self._tray is not None and self._tray.isVisible():
                event.ignore()
                self.hide()
                return
            self.clipboard_timer.stop()
            if self._hotkey_manager is not None:
                self._hotkey_manager.unregister()
                self._hotkey_manager = None
            super().closeEvent(event)

        def quit_app(self) -> None:
            self._force_close = True
            if self._hotkey_manager is not None:
                self._hotkey_manager.unregister()
                self._hotkey_manager = None
            if self._tray is not None:
                self._tray.hide()
            app.quit()

    window = QuickDeckWindow()

    # 托盘
    if QSystemTrayIcon.isSystemTrayAvailable():
        tray = QSystemTrayIcon(
            app.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon),
            window,
        )
        tray.setToolTip("QuickDeck — 全局快捷键呼出")
        menu = QMenu()
        show_action = QAction("显示 QuickDeck", menu)
        show_action.triggered.connect(window.show_deck)
        startup_action = QAction("开机启动", menu)
        startup_action.setCheckable(True)
        try:
            startup_action.setChecked(bool(get_startup_enabled()))
        except StartupError as exc:
            startup_action.setEnabled(False)
            startup_action.setToolTip(str(exc))
        quit_action = QAction("退出", menu)
        quit_action.triggered.connect(window.quit_app)
        menu.addAction(show_action)
        menu.addSeparator()
        menu.addAction(startup_action)
        menu.addAction(quit_action)
        tray.setContextMenu(menu)

        def toggle_startup(checked: bool) -> None:
            try:
                set_startup_enabled(checked)
                context.settings_store.set("startup.open_at_login", checked)
            except StartupError as exc:
                QMessageBox.warning(window, "开机启动失败", str(exc))
                startup_action.blockSignals(True)
                startup_action.setChecked(not checked)
                startup_action.blockSignals(False)

        startup_action.toggled.connect(toggle_startup)
        tray.activated.connect(
            lambda reason: window.show_deck()
            if reason in (
                QSystemTrayIcon.ActivationReason.Trigger,
                QSystemTrayIcon.ActivationReason.DoubleClick,
            )
            else None
        )
        tray.show()
        window._tray = tray
        window._tray_menu = menu
    else:
        QMessageBox.warning(
            window,
            "系统托盘不可用",
            "当前桌面环境没有系统托盘；QuickDeck 窗口将保持常规显示。",
        )

    # 全局快捷键
    from ..hotkey_manager import HotkeyError, HotkeyManager

    bridge = HotkeyBridge()
    bridge.triggered.connect(window.show_deck)
    manager = HotkeyManager()
    try:
        spec = manager.register(context.settings["hotkey"], bridge.triggered.emit)
        window.hotkey_label.setText(f"呼出快捷键：{spec.display}")
        window._hotkey_manager = manager
        window._hotkey_bridge = bridge
    except HotkeyError as exc:
        QMessageBox.warning(
            window,
            "全局快捷键注册失败",
            f"{exc}\n\nQuickDeck 仍可通过托盘或窗口使用。",
        )

    behavior = context.settings.get("launch_behavior", "tray")
    if should_start_hidden(start_minimized, behavior):
        if window._tray is not None:
            window.hide()
        else:
            window.show_deck()
    else:
        window.show_deck()

    try:
        return int(app.exec())
    finally:
        if window._hotkey_manager is not None:
            window._hotkey_manager.unregister()
        if window._tray is not None:
            window._tray.hide()
        if owns_context:
            context.close()
