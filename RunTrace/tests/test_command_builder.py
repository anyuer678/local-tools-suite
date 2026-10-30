import sys
from pathlib import Path

import pytest

from runtrace.command_builder import (
    ConfigError,
    RunConfig,
    build_command,
    env_overrides_from_pairs,
    validate_config,
)


def test_argv_mode_keeps_spaces_as_single_argument(tmp_path):
    cwd = tmp_path / "中文 目录"
    cwd.mkdir()
    config = RunConfig(
        name="参数测试",
        cwd=str(cwd),
        command=sys.executable,
        args=["-c", "print('带 空格')", "中文 参数"],
    )
    build = build_command(config)
    assert build.shell is False
    assert build.argv == [sys.executable, "-c", "print('带 空格')", "中文 参数"]
    assert build.cwd == str(cwd.resolve())
    assert "中文" in build.display


def test_shell_mode_is_explicit_and_warns(tmp_path):
    config = RunConfig(
        name="shell",
        cwd=str(tmp_path),
        command="echo",
        args=["hello world"],
        shell=True,
    )
    build = build_command(config)
    assert build.shell is True
    assert build.warning
    assert "注入" in build.warning
    assert isinstance(build.argv, str)


def test_config_validation_errors(tmp_path):
    with pytest.raises(ConfigError):
        build_command(RunConfig(name="x", cwd=str(tmp_path), command=""))
    with pytest.raises(ConfigError):
        build_command(RunConfig(name="x", cwd=str(tmp_path / "missing"), command="x"))
    with pytest.raises(ConfigError):
        build_command(RunConfig(name="x", cwd=str(tmp_path), command="x", timeout=0))
    with pytest.raises(ConfigError):
        build_command(
            RunConfig(name="x", cwd=str(tmp_path), command="x", env_overrides={"BAD=KEY": "v"})
        )


def test_validate_resolves_cwd_and_env_pairs(tmp_path):
    config = RunConfig(
        name="  ",
        cwd=str(tmp_path),
        command=sys.executable,
        env_overrides={"A": "1"},
    )
    normalized = validate_config(config)
    assert normalized.name == Path(sys.executable).name
    with pytest.raises(ConfigError):
        env_overrides_from_pairs(["NOVALUE"])
    assert env_overrides_from_pairs(["A=1=2"]) == {"A": "1=2"}
