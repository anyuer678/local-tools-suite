"""剪贴板历史服务。

默认关闭，只有显式启用后才读取系统剪贴板。测试可注入内存后端，
完全不依赖真实桌面剪贴板。
"""

from __future__ import annotations

import datetime as _dt
import re
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence


class ClipboardError(RuntimeError):
    pass


@dataclass
class ClipboardEntry:
    text: str
    captured_at: str

    def as_dict(self) -> dict[str, str]:
        return {"text": self.text, "captured_at": self.captured_at}


class ClipboardBackend(Protocol):
    def get_text(self) -> str: ...

    def set_text(self, text: str) -> None: ...

    def clear(self) -> None: ...


class MemoryClipboardBackend:
    """测试/无桌面环境使用的内存剪贴板。"""

    def __init__(self, text: str = "") -> None:
        self._text = text

    def get_text(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        self._text = text

    def clear(self) -> None:
        self._text = ""


class WindowsClipboardBackend:
    """真实 Windows 剪贴板（ctypes Win32 API）。"""

    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise ClipboardError("Windows 剪贴板后端仅支持 Windows")
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._user32.OpenClipboard.argtypes = [wintypes.HWND]
        self._user32.OpenClipboard.restype = wintypes.BOOL
        self._user32.CloseClipboard.restype = wintypes.BOOL
        self._user32.EmptyClipboard.restype = wintypes.BOOL
        self._user32.GetClipboardData.argtypes = [wintypes.UINT]
        self._user32.GetClipboardData.restype = wintypes.HANDLE
        self._user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        self._user32.SetClipboardData.restype = wintypes.HANDLE
        self._kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        self._kernel32.GlobalAlloc.restype = wintypes.HANDLE
        self._kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
        self._kernel32.GlobalLock.restype = ctypes.c_void_p
        self._kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
        self._kernel32.GlobalFree.argtypes = [wintypes.HANDLE]

    def _open(self) -> None:
        for _ in range(5):
            if self._user32.OpenClipboard(None):
                return
            time.sleep(0.05)
        raise ClipboardError("无法打开 Windows 剪贴板")

    def get_text(self) -> str:
        self._open()
        try:
            handle = self._user32.GetClipboardData(self.CF_UNICODETEXT)
            if not handle:
                return ""
            pointer = self._kernel32.GlobalLock(handle)
            if not pointer:
                raise ClipboardError("无法锁定剪贴板内存")
            try:
                return self._ctypes.wstring_at(pointer)
            finally:
                self._kernel32.GlobalUnlock(handle)
        finally:
            self._user32.CloseClipboard()

    def set_text(self, text: str) -> None:
        ctypes = self._ctypes
        buffer = ctypes.create_unicode_buffer(str(text))
        size = ctypes.sizeof(buffer)
        handle = self._kernel32.GlobalAlloc(self.GMEM_MOVEABLE, size)
        if not handle:
            raise ClipboardError("无法分配剪贴板内存")
        pointer = self._kernel32.GlobalLock(handle)
        if not pointer:
            self._kernel32.GlobalFree(handle)
            raise ClipboardError("无法锁定剪贴板内存")
        try:
            ctypes.memmove(pointer, buffer, size)
        finally:
            self._kernel32.GlobalUnlock(handle)
        self._open()
        try:
            if not self._user32.EmptyClipboard():
                raise ClipboardError("无法清空剪贴板")
            if not self._user32.SetClipboardData(self.CF_UNICODETEXT, handle):
                self._kernel32.GlobalFree(handle)
                handle = None
                raise ClipboardError("无法写入剪贴板")
            handle = None  # 所有权交给系统
        finally:
            self._user32.CloseClipboard()
            if handle:
                self._kernel32.GlobalFree(handle)

    def clear(self) -> None:
        self._open()
        try:
            if not self._user32.EmptyClipboard():
                raise ClipboardError("无法清空剪贴板")
        finally:
            self._user32.CloseClipboard()


class ClipboardHistoryService:
    """可选启用的剪贴板历史，默认关闭。"""

    def __init__(
        self,
        *,
        backend: ClipboardBackend | None = None,
        enabled: bool = False,
        max_items: int = 50,
        exclude_patterns: Sequence[str] | None = None,
        db_path: str | Path | None = None,
    ) -> None:
        if max_items < 1:
            raise ClipboardError("剪贴板历史上限必须大于 0")
        self._enabled = bool(enabled)
        self.max_items = int(max_items)
        self._backend = backend if backend is not None else self._default_backend()
        self._lock = threading.RLock()
        self._entries: list[ClipboardEntry] = []
        self.db_path = str(db_path) if db_path is not None else None
        self._conn: sqlite3.Connection | None = None
        self._patterns: list[re.Pattern[str]] = []
        for pattern in exclude_patterns or []:
            if not str(pattern):
                continue
            try:
                self._patterns.append(re.compile(str(pattern)))
            except re.error as exc:
                raise ClipboardError(f"排除规则正则无效：{pattern}（{exc}）") from exc
        if self.db_path:
            if self.db_path != ":memory:":
                Path(self.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._create_schema()
            self._load()

    @staticmethod
    def _default_backend() -> ClipboardBackend:
        if sys.platform == "win32":
            try:
                return WindowsClipboardBackend()
            except ClipboardError:
                return MemoryClipboardBackend()
        return MemoryClipboardBackend()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    @property
    def backend(self) -> ClipboardBackend:
        return self._backend

    def _create_schema(self) -> None:
        assert self._conn is not None
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS clipboard_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_clipboard_created_at
                ON clipboard_history(created_at DESC);
            """
        )
        self._conn.commit()

    def _load(self) -> None:
        if self._conn is None:
            return
        rows = self._conn.execute(
            "SELECT text, captured_at FROM clipboard_history "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (self.max_items,),
        ).fetchall()
        self._entries = [
            ClipboardEntry(text=row["text"], captured_at=row["captured_at"]) for row in rows
        ]

    def _is_excluded(self, text: str) -> bool:
        return any(pattern.search(text) for pattern in self._patterns)

    def capture_once(self) -> ClipboardEntry | None:
        """读取一次剪贴板并入库；未启用或内容被排除时返回 ``None``。"""

        if not self._enabled:
            return None
        try:
            text = self._backend.get_text()
        except ClipboardError:
            raise
        except Exception as exc:
            raise ClipboardError(f"读取剪贴板失败：{exc}") from exc
        if not text or not str(text).strip():
            return None
        text = str(text)
        if self._is_excluded(text):
            return None
        with self._lock:
            if self._entries and self._entries[0].text == text:
                return None
            self._entries = [entry for entry in self._entries if entry.text != text]
            captured_at = _dt.datetime.now().astimezone().isoformat(timespec="seconds")
            entry = ClipboardEntry(text=text, captured_at=captured_at)
            self._entries.insert(0, entry)
            self._entries = self._entries[: self.max_items]
            self._persist(entry)
            return entry

    def _persist(self, entry: ClipboardEntry) -> None:
        if self.db_path:
            if self._conn is None:
                self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
                self._create_schema()
                self._load()
            self._conn.execute(
                "INSERT INTO clipboard_history(text, captured_at, created_at) VALUES (?, ?, ?)",
                (entry.text, entry.captured_at, time.time()),
            )
            self._conn.execute(
                "DELETE FROM clipboard_history WHERE id NOT IN "
                "(SELECT id FROM clipboard_history ORDER BY created_at DESC, id DESC LIMIT ?)",
                (self.max_items,),
            )
            self._conn.commit()

    def set_text(self, text: str) -> ClipboardEntry | None:
        """写入系统剪贴板并立即捕获（未启用时仍写入，但不记录历史）。"""

        try:
            self._backend.set_text(str(text))
        except ClipboardError:
            raise
        except Exception as exc:
            raise ClipboardError(f"写入剪贴板失败：{exc}") from exc
        return self.capture_once()

    def history(self, limit: int | None = None) -> list[ClipboardEntry]:
        with self._lock:
            entries = list(self._entries)
        return entries[:limit] if limit is not None else entries

    def clear(self) -> int:
        """清空 QuickDeck 保存的历史，不主动清空系统剪贴板。"""

        with self._lock:
            count = len(self._entries)
            self._entries.clear()
            if self._conn is not None:
                self._conn.execute("DELETE FROM clipboard_history")
                self._conn.commit()
            return count

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
