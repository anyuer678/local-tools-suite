"""真实子进程执行、流式采集与进程树终止。"""

from __future__ import annotations

import locale
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .command_builder import BuildResult, RunConfig, build_command, validate_config
from .project_context import get_git_context
from .run_store import RunStore, utc_now_iso
from .stream_capture import StreamCapture

OutputCallback = Callable[[str, str], None]
StartedCallback = Callable[[str], None]


def _decode_output(raw: bytes, fallback: str) -> str:
    """逐段解码：优先 UTF-8，失败时回退到系统本地编码（如中文 Windows 的 GBK）。

    逐行判断可以让同一任务里 UTF-8 与本地编码的输出都尽量正确解码，
    而不是整条流一次性替换成 U+FFFD。
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return raw.decode(fallback)
        except (LookupError, UnicodeDecodeError):
            return raw.decode("utf-8", "replace")


def _decode_buffer(buffer: bytes, fallback: str, *, max_hold: int = 64 * 1024) -> tuple[str, bytes]:
    """按行解码，保留最后一个未完成的行，避免把多字节字符截断到两段。

    每一行独立尝试 UTF-8 与本地编码，因此同一任务混合编码的输出也能正确还原。
    """
    cut = max(buffer.rfind(b"\n"), buffer.rfind(b"\r"))
    if cut >= 0:
        head = buffer[: cut + 1]
        text = "".join(_decode_output(line, fallback) for line in head.splitlines(keepends=True))
        return text, buffer[cut + 1 :]
    if len(buffer) >= max_hold:
        return _decode_output(buffer, fallback), b""
    return "", buffer


@dataclass(slots=True)
class RunOutcome:
    run_id: str
    name: str
    status: str
    exit_code: int | None
    duration_ms: int
    started_at: str
    ended_at: str
    command_display: str
    cwd: str
    timed_out: bool = False
    cancelled: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "name": self.name,
            "status": self.status,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "command_display": self.command_display,
            "cwd": self.cwd,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "error": self.error,
        }


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:  # 便于测试/移植，Windows 之外按进程组终止。
        try:
            import signal

            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (OSError, AttributeError):
            pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass


class ProcessRunner:
    """在调用线程中执行任务，并将 stdout/stderr 分块写入存储。"""

    def __init__(
        self,
        store: RunStore,
        *,
        encoding: str = "utf-8",
        max_buffer_chars: int = 64 * 1024,
        flush_interval: float = 0.25,
    ) -> None:
        self.store = store
        self.encoding = encoding
        self.max_buffer_chars = max_buffer_chars
        self.flush_interval = flush_interval
        self._active_lock = threading.RLock()
        self._active: dict[str, threading.Event] = {}
        self._processes: dict[str, subprocess.Popen[bytes]] = {}

    def stop(self, run_id: str) -> bool:
        with self._active_lock:
            event = self._active.get(run_id)
        if event is None:
            return False
        event.set()
        return True

    def cancel_all(self) -> int:
        with self._active_lock:
            events = list(self._active.values())
        for event in events:
            event.set()
        return len(events)

    @property
    def active_run_ids(self) -> list[str]:
        with self._active_lock:
            return list(self._active)

    def run(
        self,
        config: RunConfig,
        *,
        cancel_event: threading.Event | None = None,
        on_output: OutputCallback | None = None,
        on_started: StartedCallback | None = None,
    ) -> RunOutcome:
        cfg = validate_config(config)
        build = build_command(cfg)
        merged_env = os.environ.copy()
        merged_env.update(cfg.env_overrides)
        project_path = cfg.project_path or cfg.cwd
        git_context = get_git_context(project_path)
        metadata = {
            "build_warning": build.warning,
            "shell": build.shell,
            "encoding": f"auto (utf-8 / {locale.getpreferredencoding(False) or 'utf-8'})",
            "command_display": build.display,
            "recover_note": "启动时若仍为 running，会被标记为 interrupted",
        }
        run_id = self.store.create_run(
            name=cfg.name,
            command=cfg.command,
            args=cfg.args,
            cwd=cfg.cwd,
            env=merged_env,
            env_overrides=cfg.env_overrides,
            shell=cfg.shell,
            timeout=cfg.timeout,
            status="running",
            project_path=project_path,
            git_context=git_context,
            metadata=metadata,
        )
        run_record = self.store.get_run(run_id)
        started_at = run_record["started_at"] if run_record else utc_now_iso()
        if on_started is not None:
            try:
                on_started(run_id)
            except Exception:
                # UI/日志回调故障不能导致任务无法运行。
                pass

        cancel = cancel_event or threading.Event()
        with self._active_lock:
            self._active[run_id] = cancel

        sink = StreamCapture(
            lambda stream, text: self.store.append_logs(run_id, stream, text),
            max_buffer_chars=self.max_buffer_chars,
            flush_interval=self.flush_interval,
        )
        sink.start_auto_flush()

        process: subprocess.Popen[bytes] | None = None
        reader_threads: list[threading.Thread] = []
        stop_reason: str | None = None
        error: str | None = None
        callback_errors: list[str] = []
        monotonic_start = time.monotonic()
        ended_at = started_at
        exit_code: int | None = None
        status = "failed"
        duration_ms = 0

        def emit(stream: str, text: str) -> None:
            if not text:
                return
            sink.feed(stream, text)
            if on_output is not None:
                try:
                    on_output(stream, text)
                except Exception as exc:  # 回调异常不影响真实任务
                    callback_errors.append(repr(exc))

        def reader(pipe: Any, stream_name: str) -> None:
            buffer = b""
            fallback = locale.getpreferredencoding(False) or "utf-8"
            try:
                while True:
                    # read1 返回当前可用的数据，避免按 8 KiB 阻塞导致日志延迟到进程结束。
                    chunk = pipe.read1(8192) if hasattr(pipe, "read1") else pipe.read(8192)
                    if not chunk:
                        break
                    buffer += chunk
                    text, buffer = _decode_buffer(buffer, fallback)
                    if text:
                        emit(stream_name, text)
                if buffer:
                    emit(stream_name, _decode_output(buffer, fallback))
            except (OSError, ValueError):
                pass

        try:
            kwargs: dict[str, Any] = {
                "cwd": cfg.cwd,
                "env": merged_env,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "shell": build.shell,
            }
            if os.name == "nt":
                flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
                kwargs["creationflags"] = flags
            else:
                kwargs["start_new_session"] = True
            process = subprocess.Popen(build.argv, **kwargs)
            with self._active_lock:
                self._processes[run_id] = process

            for pipe, stream_name in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                thread = threading.Thread(
                    target=reader,
                    args=(pipe, stream_name),
                    name=f"runtrace-reader-{run_id}-{stream_name}",
                    daemon=True,
                )
                thread.start()
                reader_threads.append(thread)

            deadline = (
                monotonic_start + cfg.timeout if cfg.timeout is not None else None
            )
            try:
                while process.poll() is None:
                    if cancel.is_set():
                        stop_reason = "cancelled"
                        break
                    if deadline is not None and time.monotonic() >= deadline:
                        stop_reason = "timeout"
                        break
                    time.sleep(0.05)
            except KeyboardInterrupt:
                stop_reason = "cancelled"

            if stop_reason is not None:
                _terminate_process_tree(process)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                _terminate_process_tree(process)
            exit_code = process.returncode

            for thread in reader_threads:
                thread.join(timeout=2.0)
            if stop_reason == "cancelled":
                status = "cancelled"
            elif stop_reason == "timeout":
                status = "timeout"
            elif exit_code == 0:
                status = "success"
            else:
                status = "failed"
        except (OSError, ValueError) as exc:
            error = f"启动或执行失败：{exc}"
            emit("stderr", f"\n[RunTrace] {error}\n")
            status = "cancelled" if cancel.is_set() else "failed"
            exit_code = None
        finally:
            try:
                sink.close()
            except Exception as exc:
                error = (error + "; " if error else "") + f"日志写入失败：{exc}"
                if status == "success":
                    status = "failed"
            ended_at = utc_now_iso()
            duration_ms = max(0, int((time.monotonic() - monotonic_start) * 1000))
            try:
                self.store.finish_run(
                    run_id,
                    exit_code=exit_code,
                    status=status,
                    ended_at=ended_at,
                    duration_ms=duration_ms,
                )
            except Exception as exc:
                error = (error + "; " if error else "") + f"结束记录写入失败：{exc}"
            with self._active_lock:
                self._active.pop(run_id, None)
                self._processes.pop(run_id, None)

        if callback_errors:
            note = f"输出回调错误 {len(callback_errors)} 次"
            error = (error + "; " if error else "") + note
        return RunOutcome(
            run_id=run_id,
            name=cfg.name,
            status=status,
            exit_code=exit_code,
            duration_ms=duration_ms,
            started_at=started_at,
            ended_at=ended_at,
            command_display=build.display,
            cwd=cfg.cwd,
            timed_out=stop_reason == "timeout",
            cancelled=stop_reason == "cancelled",
            error=error,
        )


def run_once(
    store: RunStore,
    config: RunConfig,
    *,
    on_output: OutputCallback | None = None,
    timeout_override: float | None = None,
) -> RunOutcome:
    if timeout_override is not None:
        config = config.copy(timeout=timeout_override)
    return ProcessRunner(store).run(config, on_output=on_output)


def preview(config: RunConfig) -> BuildResult:
    """启动前展示命令/工作目录所需信息。"""
    return build_command(config)
