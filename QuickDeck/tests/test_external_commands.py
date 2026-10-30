from __future__ import annotations

import sys
from pathlib import Path

import pytest

from quickdeck.builtin_actions import (
    BuiltinToolkit,
    ConfirmationRequired,
    ExternalCommandError,
    ExternalCommandRunner,
    ExternalCommandSpec,
)
from quickdeck.clipboard_service import MemoryClipboardBackend
from quickdeck.command_registry import CommandRegistry


def test_preview_and_confirmation_required(tmp_path: Path) -> None:
    runner = ExternalCommandRunner()
    spec = ExternalCommandSpec(argv=[sys.executable, "-c", "print('hi')"], name="测试")
    preview = runner.preview(spec)
    assert preview["argv"] == [sys.executable, "-c", "print('hi')"]
    assert preview["cwd"]
    with pytest.raises(ConfirmationRequired) as caught:
        runner.execute(spec)
    assert caught.value.preview["argv"][0] == sys.executable


def test_confirmed_execution_success(tmp_path: Path) -> None:
    runner = ExternalCommandRunner()
    result = runner.execute(
        [sys.executable, "-c", "print('hello')"],
        confirm=True,
    )
    assert result.ok
    assert result.returncode == 0
    assert "hello" in result.stdout


def test_execution_cwd_and_nonzero_exit(tmp_path: Path) -> None:
    runner = ExternalCommandRunner()
    result = runner.execute(
        {
            "argv": [sys.executable, "-c", "import os;print(os.getcwd())"],
            "cwd": str(tmp_path),
        },
        confirm=True,
    )
    assert result.ok
    assert str(tmp_path) in result.stdout

    failed = runner.execute(
        [sys.executable, "-c", "import sys;sys.exit(3)"],
        confirm=True,
    )
    assert failed.ok is False
    assert failed.returncode == 3
    assert "退出码 3" in failed.as_text()


def test_timeout_and_missing_executable() -> None:
    runner = ExternalCommandRunner()
    result = runner.execute(
        [sys.executable, "-c", "import time;time.sleep(2)"],
        confirm=True,
        timeout=0.2,
    )
    assert result.ok is False
    assert result.timed_out is True
    assert "超时" in result.as_text()

    missing = runner.execute(["definitely-not-a-real-command-xyz"], confirm=True)
    assert missing.ok is False
    assert "找不到" in missing.error


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("echo hello", "参数数组"),
        ([], "不能为空"),
        (["python", 1], "字符串"),
        ({"command": "echo"}, "参数数组"),
        ({"argv": []}, "不能为空"),
        ({"argv": ["echo"], "cwd": "/no/such/dir"}, "工作目录"),
        ({"argv": ["echo"], "timeout": 0}, "超时"),
    ],
)
def test_invalid_command_specs(spec, message: str) -> None:
    with pytest.raises(ExternalCommandError, match=message):
        ExternalCommandRunner._normalize(spec)


def test_command_shell_metacharacters_are_literal() -> None:
    runner = ExternalCommandRunner()
    result = runner.execute(
        [sys.executable, "-c", "import sys;print(sys.argv[1])", "a;b&c"],
        confirm=True,
    )
    assert result.ok
    assert "a;b&c" in result.stdout


def test_builtin_command_requires_confirmation(tmp_path: Path) -> None:
    toolkit = BuiltinToolkit(
        tmp_path / "q.db",
        clipboard_backend=MemoryClipboardBackend(),
    )
    registry = CommandRegistry(tmp_path / "q.db")
    toolkit.register_commands(registry)
    params = {"argv": [sys.executable, "-c", "print('confirmed')"]}
    with pytest.raises(ConfirmationRequired, match="确认"):
        registry.execute("command.run", params=params)
    params["confirm"] = True
    assert "confirmed" in registry.execute("command.run", params=params)
    registry.close()
    toolkit.close()
