"""跨进程写入锁：避免两个 EnvForge 实例同时写注册表造成丢失更新。

实现方式：
- 进程内：按锁文件的绝对路径维护一个 threading.Lock，保证同进程多线程互斥。
- 跨进程：Windows 上用 msvcrt.locking 对锁文件首字节做非阻塞加锁并轮询等待；
  非 Windows 或内存数据库场景只做进程内互斥（此时不存在共享注册表写入）。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import EnvAccessError

if sys.platform == "win32":  # pragma: no branch - 平台常量
    import msvcrt
else:  # pragma: no cover - 非 Windows 环境
    msvcrt = None  # type: ignore[assignment]

_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_REGISTRY_LOCK = threading.Lock()


def _process_lock(key: str) -> threading.Lock:
    with _REGISTRY_LOCK:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PROCESS_LOCKS[key] = lock
        return lock


@contextmanager
def file_lock(
    name: str,
    *,
    lock_dir: str | Path | None = None,
    timeout: float = 8.0,
    poll: float = 0.1,
) -> Iterator[Path]:
    """获取命名锁；超时抛 EnvAccessError（中文提示，可直接展示给用户）。"""
    directory = Path(lock_dir) if lock_dir is not None else _default_lock_dir()
    path = directory / f"{name}.lock"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise EnvAccessError(f"无法创建锁目录 {directory}：{exc}") from exc

    key = str(path.resolve())
    process = _process_lock(key)
    deadline = time.monotonic() + max(timeout, 0.0)
    if not process.acquire(timeout=max(timeout, 0.0)):
        raise EnvAccessError(_conflict_message(timeout))

    fd: int | None = None
    file_locked = False
    try:
        if msvcrt is not None:
            try:
                fd = os.open(str(path), os.O_CREAT | os.O_RDWR)
            except OSError as exc:
                raise EnvAccessError(f"无法打开锁文件 {path}：{exc}") from exc
            os.lseek(fd, 0, os.SEEK_SET)
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    file_locked = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise EnvAccessError(_conflict_message(timeout)) from None
                    time.sleep(max(poll, 0.01))
        yield path
    finally:
        if fd is not None:
            if file_locked:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:  # pragma: no cover - 释放失败不影响结果
                    pass
            try:
                os.close(fd)
            except OSError:  # pragma: no cover
                pass
        process.release()


def _conflict_message(timeout: float) -> str:
    return (
        "另一个 EnvForge 窗口或命令行进程正在写入环境变量，为避免互相覆盖已放弃本次写入。"
        f"已等待 {timeout:g} 秒；请关闭其他窗口后重试。"
    )


def _default_lock_dir() -> Path:
    from .db import default_data_dir

    return default_data_dir() / "locks"
