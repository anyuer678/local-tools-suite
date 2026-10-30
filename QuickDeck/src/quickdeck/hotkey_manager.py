"""Windows 全局快捷键：解析 + RegisterHotKey 消息循环。

快捷键解析是纯逻辑，可在任意平台测试；
真实注册只在 Windows 上尝试，失败会抛出明确异常，绝不伪装成功。
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

_MODIFIER_ALIASES = {
    "alt": (MOD_ALT, "Alt"),
    "ctrl": (MOD_CONTROL, "Ctrl"),
    "control": (MOD_CONTROL, "Ctrl"),
    "shift": (MOD_SHIFT, "Shift"),
    "win": (MOD_WIN, "Win"),
    "windows": (MOD_WIN, "Win"),
    "super": (MOD_WIN, "Win"),
    "meta": (MOD_WIN, "Win"),
}

_VK_NAMES: dict[str, tuple[int, str]] = {
    "SPACE": (0x20, "Space"),
    "SPACEBAR": (0x20, "Space"),
    "ENTER": (0x0D, "Enter"),
    "RETURN": (0x0D, "Enter"),
    "TAB": (0x09, "Tab"),
    "ESC": (0x1B, "Esc"),
    "ESCAPE": (0x1B, "Esc"),
    "BACKSPACE": (0x08, "Backspace"),
    "DELETE": (0x2E, "Delete"),
    "DEL": (0x2E, "Delete"),
    "INSERT": (0x2D, "Insert"),
    "INS": (0x2D, "Insert"),
    "HOME": (0x24, "Home"),
    "END": (0x23, "End"),
    "PAGEUP": (0x21, "PageUp"),
    "PAGEDOWN": (0x22, "PageDown"),
    "UP": (0x26, "Up"),
    "DOWN": (0x28, "Down"),
    "LEFT": (0x25, "Left"),
    "RIGHT": (0x27, "Right"),
    "COMMA": (0xBC, "Comma"),
    "PERIOD": (0xBE, "Period"),
    "DOT": (0xBE, "Period"),
    "SLASH": (0xBF, "Slash"),
    "SEMICOLON": (0xBA, "Semicolon"),
    "QUOTE": (0xDE, "Quote"),
    "LBRACKET": (0xDB, "BracketLeft"),
    "RBRACKET": (0xDD, "BracketRight"),
    "BACKSLASH": (0xDC, "Backslash"),
    "MINUS": (0xBD, "Minus"),
    "EQUAL": (0xBB, "Equal"),
    "EQUALS": (0xBB, "Equal"),
}
for _letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    _VK_NAMES[_letter] = (ord(_letter), _letter)
for _digit in "0123456789":
    _VK_NAMES[_digit] = (ord(_digit), _digit)
for _index in range(1, 25):
    _VK_NAMES[f"F{_index}"] = (0x6F + _index, f"F{_index}")


class HotkeyError(RuntimeError):
    pass


class HotkeyParseError(HotkeyError):
    pass


class HotkeyUnavailable(HotkeyError):
    pass


class HotkeyRegistrationError(HotkeyError):
    pass


@dataclass(frozen=True)
class HotkeySpec:
    modifiers: int
    vk: int
    key: str
    display: str

    def as_tuple(self) -> tuple[int, int]:
        return self.modifiers, self.vk


def parse_hotkey(value: str) -> HotkeySpec:
    """解析 ``Ctrl+Alt+Space`` 这类键位描述。"""

    if not isinstance(value, str) or not value.strip():
        raise HotkeyParseError("快捷键不能为空")
    parts = [part.strip() for part in value.split("+")]
    if any(not part for part in parts):
        raise HotkeyParseError("快捷键格式无效，请使用 Ctrl+Alt+Space 形式")
    modifiers = 0
    modifier_names: list[str] = []
    key: tuple[int, str] | None = None
    for part in parts:
        normalized = part.casefold()
        if normalized in _MODIFIER_ALIASES:
            flag, display = _MODIFIER_ALIASES[normalized]
            if modifiers & flag:
                raise HotkeyParseError(f"修饰键重复：{display}")
            modifiers |= flag
            modifier_names.append(display)
            continue
        upper = part.upper()
        target = _VK_NAMES.get(upper)
        if target is None and len(part) == 1:
            target = _VK_NAMES.get(part.upper())
        if target is None:
            raise HotkeyParseError(f"不支持的按键：{part}")
        if key is not None:
            raise HotkeyParseError("快捷键只能包含一个主键")
        key = target
    if key is None:
        raise HotkeyParseError("快捷键缺少主键（例如 Space 或 F8）")
    order = {"Ctrl": 0, "Alt": 1, "Shift": 2, "Win": 3}
    modifier_names.sort(key=lambda name: order[name])
    display = "+".join(modifier_names + [key[1]])
    return HotkeySpec(modifiers=modifiers, vk=key[0], key=key[1], display=display)


def format_hotkey(spec: HotkeySpec) -> str:
    return spec.display


class HotkeyManager:
    """在独立线程注册全局热键并运行 Win32 消息循环。"""

    HOTKEY_ID = 1

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._ready = threading.Event()
        self._running = False
        self._spec: HotkeySpec | None = None
        self._callback: Callable[[], None] | None = None
        self._registration_error: str | None = None

    @property
    def is_registered(self) -> bool:
        with self._lock:
            return bool(self._running and self._thread and self._spec)

    @property
    def spec(self) -> HotkeySpec | None:
        return self._spec

    def register(self, hotkey: str | HotkeySpec, callback: Callable[[], None]) -> HotkeySpec:
        if sys.platform != "win32":
            raise HotkeyUnavailable("全局快捷键仅支持 Windows（Win32 RegisterHotKey）。")
        spec = hotkey if isinstance(hotkey, HotkeySpec) else parse_hotkey(hotkey)
        if not callable(callback):
            raise HotkeyRegistrationError("快捷键回调必须可调用")
        with self._lock:
            already_registered = self.is_registered
        if already_registered:
            self.unregister()
        with self._lock:
            self._ready.clear()
            self._thread_id = None
            self._registration_error = None
            self._spec = spec
            self._callback = callback
            self._running = True
            thread = threading.Thread(
                target=self._message_loop,
                name="quickdeck-hotkey",
                daemon=True,
            )
            self._thread = thread
            thread.start()
        if not self._ready.wait(timeout=5):
            with self._lock:
                self._running = False
            raise HotkeyRegistrationError("注册全局快捷键超时，消息线程未就绪。")
        if self._registration_error:
            with self._lock:
                self._running = False
            raise HotkeyRegistrationError(self._registration_error)
        return spec

    def _message_loop(self) -> None:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        try:
            self._thread_id = int(kernel32.GetCurrentThreadId())
            assert self._spec is not None
            registered = user32.RegisterHotKey(
                None,
                self.HOTKEY_ID,
                self._spec.modifiers | MOD_NOREPEAT,
                self._spec.vk,
            )
            if not registered:
                error_code = ctypes.get_last_error()
                detail = ""
                try:
                    detail = f"：{ctypes.WinError(error_code)}"
                except Exception:
                    detail = f"（错误码 {error_code}）"
                self._registration_error = (
                    f"RegisterHotKey 失败，快捷键 {self._spec.display} 可能已被占用{detail}"
                )
                self._ready.set()
                return
            self._ready.set()
            message = wintypes.MSG()
            while self._running:
                result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    logger.error("GetMessageW 失败")
                    break
                if message.message == WM_HOTKEY and int(message.wParam) == self.HOTKEY_ID:
                    callback = self._callback
                    if callback is not None:
                        try:
                            callback()
                        except Exception:
                            logger.exception("全局快捷键回调异常")
        finally:
            try:
                user32.UnregisterHotKey(None, self.HOTKEY_ID)
            except Exception:
                pass
            with self._lock:
                self._running = False
            self._thread_id = None

    def unregister(self) -> bool:
        with self._lock:
            was_active = self.is_registered
            if not was_active:
                return False
            self._running = False
            thread = self._thread
            thread_id = self._thread_id
        if thread_id:
            try:
                ctypes.windll.user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
            except Exception:
                logger.exception("PostThreadMessageW 失败")
        if thread is not None:
            thread.join(timeout=2)
        with self._lock:
            self._thread = None
            self._spec = None
            self._callback = None
        return True

    stop = unregister
