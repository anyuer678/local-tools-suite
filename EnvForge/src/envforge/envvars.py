"""环境变量读取与 PATH 分析。

作用域：
- user   当前用户环境变量（HKCU\\Environment，可写）
- system 系统环境变量（HKLM\\...\\Environment，本版本只读）
- session 当前进程环境（os.environ，只影响本进程）
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from typing import Protocol

from .errors import EnvAccessError, ValidationError
from .models import PathEntry

SCOPES = ("user", "system", "session")
_SCOPE_LABELS = {"user": "用户", "system": "系统", "session": "当前进程"}

_CREATE_NO_WINDOW = 0x08000000


def scope_label(scope: str) -> str:
    return _SCOPE_LABELS.get(scope, scope)


def split_path(value: str) -> list[str]:
    if not value:
        return []
    parts = [part.strip().strip('"') for part in value.split(";")]
    return [part for part in parts if part]


def dedupe_path(value: str) -> tuple[str, int]:
    """去掉空段与重复目录（按展开后的路径不区分大小写比较），返回（新值, 删除数）。"""
    kept: list[str] = []
    seen: list[str] = []
    removed = 0
    for part in value.split(";"):
        raw = part.strip()
        if not raw:
            if value.strip():
                removed += 1
            continue
        expanded = os.path.expandvars(raw)
        key = os.path.normcase(os.path.normpath(expanded))
        if key in seen:
            removed += 1
            continue
        seen.append(key)
        kept.append(raw)
    return ";".join(kept), removed


def analyze_path(
    value: str,
    exists_fn: Callable[[str], bool] | None = None,
    expand_fn: Callable[[str], str] | None = None,
) -> list[PathEntry]:
    exists = exists_fn or os.path.isdir
    expand = expand_fn or os.path.expandvars
    entries: list[PathEntry] = []
    first_index: dict[str, int] = {}
    for index, raw in enumerate(part.strip() for part in (value or "").split(";")):
        expanded = expand(raw) if raw else ""
        duplicate_of: int | None = None
        if raw:
            key = os.path.normcase(os.path.normpath(expanded))
            if key in first_index:
                duplicate_of = first_index[key]
            else:
                first_index[key] = index
        try:
            present = bool(raw) and exists(expanded)
        except OSError:
            present = False
        entries.append(
            PathEntry(
                index=index,
                raw=raw,
                expanded=expanded,
                exists=present,
                duplicate_of=duplicate_of,
            )
        )
    return entries


class EnvProvider(Protocol):
    scope: str
    writable: bool

    def list_vars(self) -> dict[str, str]: ...

    def get(self, name: str) -> str | None: ...

    def write(self, updates: Mapping[str, str | None]) -> None: ...


class SessionProvider:
    scope = "session"
    writable = True

    def list_vars(self) -> dict[str, str]:
        return dict(os.environ)

    def get(self, name: str) -> str | None:
        return os.environ.get(name)

    def write(self, updates: Mapping[str, str | None]) -> None:
        for name, value in updates.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class RegistryProvider:
    """Windows 注册表环境变量。user 可写，system 只读（写入需要管理员权限）。"""

    _SUBS = {
        "user": (0x80000001, r"Environment"),  # HKEY_CURRENT_USER
        "system": (
            0x80000002,  # HKEY_LOCAL_MACHINE
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
    }

    def __init__(self, scope: str) -> None:
        if scope not in ("user", "system"):
            raise ValidationError(f"注册表提供者不支持作用域 {scope!r}。")
        self.scope = scope
        self.writable = scope == "user"

    @staticmethod
    def _winreg():
        if sys.platform != "win32":
            raise EnvAccessError("注册表环境变量仅在 Windows 上可用；可用 scope=session 查看当前进程环境。")
        try:
            import winreg
        except ImportError as exc:  # pragma: no cover
            raise EnvAccessError(f"无法加载 winreg：{exc}") from exc
        return winreg

    def _root(self):
        winreg = self._winreg()
        hive, sub = self._SUBS[self.scope]
        return winreg, winreg.HKEY_CURRENT_USER if hive == 0x80000001 else winreg.HKEY_LOCAL_MACHINE, sub

    def list_vars(self) -> dict[str, str]:
        winreg, root, sub = self._root()
        values: dict[str, str] = {}
        try:
            key = winreg.OpenKey(root, sub, 0, winreg.KEY_READ)
        except OSError as exc:
            raise EnvAccessError(
                f"无法读取{scope_label(self.scope)}环境变量键：{exc}。"
                "系统作用域需要相应读取权限。"
            ) from exc
        with key:
            index = 0
            while True:
                try:
                    name, data, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                if isinstance(data, list):
                    values[name] = ";".join(str(item) for item in data)
                elif isinstance(data, bytes):
                    continue
                else:
                    values[name] = str(data)
        return values

    def get(self, name: str) -> str | None:
        return self.list_vars().get(name)

    def write(self, updates: Mapping[str, str | None]) -> None:
        if not self.writable:
            raise EnvAccessError(
                f"{scope_label(self.scope)}环境变量为只读；写入系统变量需要管理员权限，"
                "本版本不提供该操作。"
            )
        winreg, root, sub = self._root()
        try:
            key = winreg.OpenKey(root, sub, 0, winreg.KEY_SET_VALUE | winreg.KEY_READ)
        except OSError as exc:
            raise EnvAccessError(f"无法打开用户环境变量键以写入：{exc}") from exc
        with key:
            for name, value in updates.items():
                try:
                    if value is None:
                        winreg.DeleteValue(key, name)
                    else:
                        kind = winreg.REG_EXPAND_SZ if "%" in value else winreg.REG_SZ
                        winreg.SetValueEx(key, name, 0, kind, value)
                except OSError as exc:
                    raise EnvAccessError(
                        f"写入环境变量 {name!r} 失败：{exc}；请检查注册表权限或变量名是否合法。"
                    ) from exc
        self._broadcast()

    def _broadcast(self) -> None:
        """通知资源管理器刷新环境，使新启动的进程看到变更。失败不影响写入结果。"""
        try:
            import ctypes

            result = ctypes.c_ulong()
            ctypes.windll.user32.SendMessageTimeoutW(
                0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result)
            )
        except Exception:  # pragma: no cover - 广播失败不回滚已完成的写入
            pass


def provider_for(scope: str, providers: Mapping[str, EnvProvider] | None = None) -> EnvProvider:
    if providers and scope in providers:
        return providers[scope]
    if scope == "session":
        return SessionProvider()
    if scope in ("user", "system"):
        return RegistryProvider(scope)
    raise ValidationError(f"未知作用域 {scope!r}；可选：{', '.join(SCOPES)}")
