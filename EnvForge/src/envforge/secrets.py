"""敏感值存储与脱敏工具。

默认策略：
- 变量名命中密钥/令牌/密码等模式时自动判定为敏感。
- 敏感值使用 Windows DPAPI（CryptProtectData）按当前用户加密后落库，明文不落盘。
- 当前系统不支持 DPAPI 时，拒绝保存明文，只保留“键存在”的占位状态并明确报告。
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

REDACTED = "***"
_ENTROPY = b"EnvForge/secret/v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class CryptoError(RuntimeError):
    """加密或解密失败。"""


class CryptoUnavailable(CryptoError):
    """当前平台不具备 DPAPI 能力。"""


def mask_value(value: str, *, visible_prefix: int = 0, visible_suffix: int = 0) -> str:
    """返回脱敏后的展示值；默认不泄露任何字符。"""
    if not value:
        return REDACTED
    if visible_prefix or visible_suffix:
        if len(value) <= visible_prefix + visible_suffix:
            return REDACTED
        prefix = value[:visible_prefix] if visible_prefix else ""
        suffix = value[-visible_suffix:] if visible_suffix else ""
        return f"{prefix}{REDACTED}{suffix}"
    return REDACTED


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


class SecretCipher:
    """DPAPI 加解密封装。可注入以替换真实平台实现（测试用）。"""

    def __init__(self, entropy: bytes = _ENTROPY) -> None:
        self._entropy = entropy
        self._crypt32: ctypes.WinDLL | None = None
        self._kernel32: ctypes.WinDLL | None = None
        self._reason = ""
        self._load()

    def _load(self) -> None:
        if sys.platform != "win32":
            self._reason = "当前系统不是 Windows，无法使用 DPAPI"
            return
        try:
            crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            crypt32.CryptProtectData.argtypes = [
                ctypes.POINTER(_Blob),
                wintypes.LPCWSTR,
                ctypes.POINTER(_Blob),
                ctypes.c_void_p,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(_Blob),
            ]
            crypt32.CryptProtectData.restype = wintypes.BOOL
            crypt32.CryptUnprotectData.argtypes = [
                ctypes.POINTER(_Blob),
                ctypes.POINTER(wintypes.LPWSTR),
                ctypes.POINTER(_Blob),
                ctypes.c_void_p,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(_Blob),
            ]
            crypt32.CryptUnprotectData.restype = wintypes.BOOL
            kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
            kernel32.LocalFree.restype = wintypes.HLOCAL
        except (OSError, AttributeError) as exc:  # pragma: no cover - 依赖系统库
            self._reason = f"加载 DPAPI 失败：{exc}"
            return
        self._crypt32 = crypt32
        self._kernel32 = kernel32

    @property
    def available(self) -> bool:
        return self._crypt32 is not None and self._kernel32 is not None

    @property
    def unavailable_reason(self) -> str:
        return self._reason

    def status(self) -> str:
        return "DPAPI 可用（当前用户作用域加密）" if self.available else self._reason

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _make_blob(data: bytes) -> tuple[_Blob, ctypes.Array]:
        buf = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def _free(self, blob: _Blob) -> None:
        assert self._kernel32 is not None
        self._kernel32.LocalFree(ctypes.cast(blob.pbData, wintypes.HLOCAL))

    # ------------------------------------------------------------------ 加解密
    def protect(self, value: str) -> bytes:
        if not self.available:
            raise CryptoUnavailable(self._reason or "DPAPI 不可用")
        assert self._crypt32 is not None
        data = value.encode("utf-8")
        in_blob, in_buf = self._make_blob(data)
        entropy_blob, entropy_buf = self._make_blob(self._entropy)
        out_blob = _Blob()
        ok = self._crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            "EnvForge",
            ctypes.byref(entropy_blob),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
        del in_buf, entropy_buf
        if not ok:
            raise CryptoError(f"加密失败：{ctypes.WinError(ctypes.get_last_error())}")
        try:
            result = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            self._free(out_blob)
        return result

    def unprotect(self, blob: bytes) -> str:
        if not self.available:
            raise CryptoUnavailable(self._reason or "DPAPI 不可用")
        assert self._crypt32 is not None
        in_blob, in_buf = self._make_blob(blob)
        entropy_blob, entropy_buf = self._make_blob(self._entropy)
        out_blob = _Blob()
        description = wintypes.LPWSTR()
        ok = self._crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            ctypes.byref(description),
            ctypes.byref(entropy_blob),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
        del in_buf, entropy_buf
        if not ok:
            raise CryptoError(f"解密失败：{ctypes.WinError(ctypes.get_last_error())}")
        try:
            data = ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            self._free(out_blob)
        return data.decode("utf-8", errors="replace")
