import subprocess
import sys
import threading
import time

import pytest

from runtrace.command_builder import RunConfig
from runtrace.process_runner import ProcessRunner
from runtrace.run_store import RunStore


def _config(tmp_path, *, args, name="run", timeout=None, env_overrides=None, cwd=None):
    return RunConfig(
        name=name,
        cwd=str(cwd or tmp_path),
        command=sys.executable,
        args=args,
        timeout=timeout,
        env_overrides=env_overrides or {},
    )


def test_success_run_persists_and_redacts_environment(store, runner, tmp_path, monkeypatch):
    monkeypatch.setenv("RUNTRACE_TEST_SECRET", "my-secret-value")
    config = _config(
        tmp_path,
        args=["-c", "print('hello'); print('中文输出'); print('to-stderr', file=__import__('sys').stderr)"],
        name="成功场景",
        env_overrides={"RUNTRACE_TEST_TOKEN": "override-secret"},
    )
    outcome = runner.run(config)
    assert outcome.status == "success"
    assert outcome.exit_code == 0
    assert outcome.duration_ms >= 0
    assert outcome.error is None

    run = store.get_run(outcome.run_id)
    assert run is not None
    assert run["status"] == "success"
    assert run["exit_code"] == 0
    assert run["shell"] is False
    assert run["args"] == config.args
    assert "hello" in store.get_logs(outcome.run_id, "stdout")
    assert "中文输出" in store.get_logs(outcome.run_id, "stdout")
    assert "to-stderr" in store.get_logs(outcome.run_id, "stderr")

    # 环境在入库前脱敏，运行本身仍拿到真实覆盖值。
    assert run["env"]["RUNTRACE_TEST_SECRET"] == "***"
    assert run["env_overrides"]["RUNTRACE_TEST_TOKEN"] == "***"
    assert run["env"]["RUNTRACE_TEST_TOKEN"] == "***"
    assert "my-secret-value" not in repr(run)
    assert "override-secret" not in repr(run)


def test_gbk_output_is_decoded_with_locale_fallback(store, runner, tmp_path, monkeypatch):
    script = (
        "import sys; "
        "sys.stdout.buffer.write('第一行中文\\n'.encode('gbk')); "
        "sys.stdout.buffer.write('utf-8 中文\\n'.encode('utf-8'))"
    )
    monkeypatch.setattr("runtrace.process_runner.locale.getpreferredencoding", lambda do_setlocale=True: "gbk")
    outcome = runner.run(_config(tmp_path, args=["-c", script], name="混合编码"))
    assert outcome.status == "success"
    logs = store.get_logs(outcome.run_id, "stdout")
    assert "第一行中文" in logs
    assert "utf-8 中文" in logs


def test_failed_exit_code_three_and_stderr(store, runner, tmp_path):
    outcome = runner.run(
        _config(
            tmp_path,
            args=[
                "-c",
                "import sys; print('boom', file=sys.stderr); print('out'); sys.exit(3)",
            ],
            name="失败场景",
        )
    )
    assert outcome.status == "failed"
    assert outcome.exit_code == 3
    run = store.get_run(outcome.run_id)
    assert run["status"] == "failed"
    assert "boom" in store.get_logs(outcome.run_id, "stderr")
    assert "out" in store.get_logs(outcome.run_id, "stdout")


def test_chinese_space_cwd_and_arguments(store, runner, tmp_path):
    cwd = tmp_path / "中文 空格 目录"
    cwd.mkdir()
    config = _config(
        tmp_path,
        cwd=cwd,
        args=[
            "-c",
            "import sys; print(sys.argv[1]); print(sys.argv[2])",
            "中文 参数",
            "值 with spaces",
        ],
        name="中文路径",
    )
    outcome = runner.run(config)
    assert outcome.status == "success", outcome.error
    assert outcome.cwd == str(cwd.resolve())
    stdout = store.get_logs(outcome.run_id, "stdout")
    assert "中文 参数" in stdout
    assert "值 with spaces" in stdout
    assert store.get_run(outcome.run_id)["cwd"] == str(cwd.resolve())


def test_timeout_kills_process_tree(store, runner, tmp_path):
    started = time.monotonic()
    outcome = runner.run(
        _config(
            tmp_path,
            args=["-c", "import time; time.sleep(2); print('never')"],
            name="超时",
            timeout=0.5,
        )
    )
    elapsed = time.monotonic() - started
    assert outcome.status == "timeout"
    assert outcome.timed_out is True
    assert outcome.exit_code is not None
    assert elapsed < 2.0
    assert "never" not in store.get_logs(outcome.run_id, "stdout")
    assert store.get_run(outcome.run_id)["status"] == "timeout"


def test_cancel_long_running_task(store, tmp_path):
    runner = ProcessRunner(store)
    cancel = threading.Event()
    started = threading.Event()
    run_id_holder: dict[str, str] = {}
    result_holder: dict[str, object] = {}

    def target():
        result_holder["outcome"] = runner.run(
            _config(
                tmp_path,
                args=[
                    "-c",
                    "import sys, time; print('started', flush=True); time.sleep(30)",
                ],
                name="取消",
            ),
            cancel_event=cancel,
            on_started=lambda rid: (run_id_holder.__setitem__("id", rid), started.set()),
        )

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    assert started.wait(timeout=5)
    run_id = run_id_holder["id"]
    # 给子进程一点时间真正启动并产出首行日志。
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if store.get_logs(run_id, "stdout").strip():
            break
        time.sleep(0.05)
    # 真流式：进程仍在运行时日志应已可读，而不是等进程结束才落库。
    assert store.get_logs(run_id, "stdout").strip(), "取消前未收到实时日志"
    assert runner.stop(run_id) is True
    thread.join(timeout=10)
    assert not thread.is_alive()
    outcome = result_holder["outcome"]
    assert outcome.status == "cancelled"
    assert outcome.cancelled is True
    assert outcome.duration_ms < 10000
    assert store.get_run(run_id)["status"] == "cancelled"


def _pid_exists(pid: int) -> bool:
    if sys.platform != "win32":
        return False
    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return str(pid) in (result.stdout or "")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 进程树检查")
def test_stop_terminates_grandchild_process_tree(store, tmp_path):
    runner = ProcessRunner(store)
    cancel = threading.Event()
    run_id_holder: dict[str, str] = {}
    result_holder: dict[str, object] = {}
    child_pid_holder: dict[str, int] = {}

    parent_script = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(30)\n"
    )

    def target():
        result_holder["outcome"] = runner.run(
            _config(tmp_path, args=["-c", parent_script], name="进程树"),
            cancel_event=cancel,
            on_started=lambda rid: (run_id_holder.__setitem__("id", rid)),
        )

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    run_id = run_id_holder.get("id")
    if not run_id:
        # 等待 run 记录出现。
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not run_id:
            run_id = run_id_holder.get("id")
            time.sleep(0.05)
    assert run_id
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        text = store.get_logs(run_id, "stdout").strip()
        if text.isdigit():
            child_pid_holder["pid"] = int(text.splitlines()[0])
            break
        time.sleep(0.05)
    assert "pid" in child_pid_holder, "未能读取孙进程 PID"
    child_pid = child_pid_holder["pid"]
    assert _pid_exists(child_pid)

    assert runner.stop(run_id) is True
    thread.join(timeout=10)
    assert not thread.is_alive()
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if not _pid_exists(child_pid):
            break
        time.sleep(0.1)
    assert not _pid_exists(child_pid), "taskkill /T 未终止孙进程"
    outcome = result_holder["outcome"]
    assert outcome.status == "cancelled"


def test_large_output_is_streamed_in_chunks(store, runner, tmp_path):
    script = (
        "import sys\n"
        "chunk = 'X' * 60000\n"
        "for _ in range(50):\n"
        "    sys.stdout.write(chunk)\n"
        "sys.stdout.flush()\n"
    )
    outcome = runner.run(_config(tmp_path, args=["-c", script], name="大输出"))
    assert outcome.status == "success"
    text = store.get_logs(outcome.run_id, "stdout")
    assert len(text) == 3_000_000
    stats = store.get_log_stats(outcome.run_id)
    assert stats["stdout"]["chunks"] > 1
    assert stats["stdout"]["chars"] == 3_000_000


def test_output_callback_receives_both_streams(store, runner, tmp_path):
    chunks: list[tuple[str, str]] = []
    outcome = runner.run(
        _config(
            tmp_path,
            args=[
                "-c",
                "import sys; print('o1'); print('e1', file=sys.stderr); print('o2')",
            ],
        ),
        on_output=lambda stream, text: chunks.append((stream, text)),
    )
    assert outcome.status == "success"
    assert {stream for stream, _ in chunks} == {"stdout", "stderr"}
    assert "o1" in "".join(text for stream, text in chunks if stream == "stdout")
    assert "e1" in "".join(text for stream, text in chunks if stream == "stderr")


def test_invalid_executable_is_recorded_as_failure(store, tmp_path):
    runner = ProcessRunner(store)
    config = RunConfig(
        name="bad",
        cwd=str(tmp_path),
        command="definitely-not-a-real-executable-xyz",
        args=[],
    )
    outcome = runner.run(config)
    assert outcome.status == "failed"
    assert outcome.exit_code is None
    assert outcome.error
    assert store.get_run(outcome.run_id)["status"] == "failed"
    assert "[RunTrace]" in store.get_logs(outcome.run_id, "stderr")
