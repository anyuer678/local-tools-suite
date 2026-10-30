"""有限窗口的流式日志缓冲。

读取线程把输出追加到内存窗口；达到阈值或定时器触发时，批量写入 SQLite。
内存中只保留固定大小窗口，完整日志由存储层保存。
"""

from __future__ import annotations

import threading
from collections.abc import Callable

AppendCallback = Callable[[str, str], None]


class StreamCapture:
    def __init__(
        self,
        writer: AppendCallback,
        *,
        max_buffer_chars: int = 64 * 1024,
        flush_interval: float = 0.25,
    ) -> None:
        self._writer = writer
        self._max_buffer = max(1024, int(max_buffer_chars))
        self._flush_interval = max(0.05, float(flush_interval))
        self._lock = threading.RLock()
        self._buffers: dict[str, str] = {"stdout": "", "stderr": ""}
        self._closed = False
        self._error: BaseException | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start_auto_flush(self) -> None:
        """启动后台定时 flush，使小步输出也能及时进入数据库。"""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._auto_flush_loop, name="runtrace-log-flush", daemon=True
        )
        self._thread.start()

    def _auto_flush_loop(self) -> None:
        while not self._stop_event.wait(self._flush_interval):
            try:
                self.flush()
            except BaseException as exc:  # 保留错误给 close() 调用方感知
                self._error = exc
                return

    def feed(self, stream: str, text: str) -> None:
        if stream not in ("stdout", "stderr"):
            raise ValueError(f"未知输出流：{stream}")
        if not text:
            return
        to_write = ""
        with self._lock:
            if self._closed:
                return
            self._buffers[stream] += text
            if len(self._buffers[stream]) >= self._max_buffer:
                to_write = self._buffers[stream]
                self._buffers[stream] = ""
        if to_write:
            self._writer(stream, to_write)

    def flush(self, stream: str | None = None) -> None:
        with self._lock:
            streams = [stream] if stream else ["stdout", "stderr"]
            pending: list[tuple[str, str]] = []
            for name in streams:
                text = self._buffers.get(name, "")
                if text:
                    pending.append((name, text))
                    self._buffers[name] = ""
        for name, text in pending:
            self._writer(name, text)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._stop_event.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self.flush()
        if self._error is not None:
            raise self._error

    @property
    def pending_chars(self) -> dict[str, int]:
        with self._lock:
            return {name: len(text) for name, text in self._buffers.items()}
