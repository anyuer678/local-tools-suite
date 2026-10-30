from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

from quickdeck.command_registry import CommandRegistry
from quickdeck.plugin_host import (
    PluginConsentRequired,
    PluginDisabled,
    PluginExecutionError,
    PluginManager,
)

PROJECT_PLUGINS = Path(__file__).resolve().parents[1] / "plugins"


def _copy_example(root: Path) -> None:
    shutil.copytree(PROJECT_PLUGINS / "example_plugin", root / "example_plugin")


def _write_plugin(
    root: Path,
    directory: str,
    plugin_id: str,
    code: str,
    commands: list[dict],
) -> Path:
    target = root / directory
    target.mkdir(parents=True)
    (target / "plugin.json").write_text(
        json.dumps(
            {
                "id": plugin_id,
                "name": f"测试插件 {plugin_id}",
                "version": "1.0.0",
                "api_version": "1.0",
                "entry": "plugin.py",
                "commands": commands,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (target / "plugin.py").write_text(code, encoding="utf-8")
    return target


def _manager(root: Path, state: Path, timeout: float = 5.0) -> PluginManager:
    return PluginManager(root, state_path=state, timeout=timeout)


def test_example_plugin_is_disabled_by_default_and_requires_consent(tmp_path: Path) -> None:
    manager = _manager(PROJECT_PLUGINS, tmp_path / "state.json")
    info = manager.get("example")
    assert info.valid
    assert info.enabled is False
    with pytest.raises(PluginConsentRequired):
        manager.enable("example")
    assert manager.is_enabled("example") is False

    enabled = manager.enable("example", consent=True)
    assert enabled.enabled is True
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert state["plugins"]["example"]["enabled"] is True
    assert "consented_at" in state["plugins"]["example"]


def test_example_plugin_runs_in_real_subprocess_and_registers_commands(tmp_path: Path) -> None:
    manager = _manager(PROJECT_PLUGINS, tmp_path / "state.json")
    registry = CommandRegistry(tmp_path / "q.db")
    manager.register_commands(registry)
    assert registry.count() == 0

    manager.enable("example", consent=True)
    assert registry.count() == 2
    assert registry.get("plugin:example:char_count") is not None
    assert registry.search("统计")[0].id == "plugin:example:char_count"

    result = manager.run("example", "char_count", "你好abc")
    assert result.ok
    assert result.output == "字符数：5，行数：1"

    result = manager.run("example", "reverse", "QuickDeck")
    assert result.ok
    assert result.output == "kceDkciuQ"

    assert registry.execute("plugin:example:char_count", "abcd") == "字符数：4，行数：1"
    registry.close()


def test_disable_prevents_plugin_execution_and_unregisters(tmp_path: Path) -> None:
    manager = _manager(PROJECT_PLUGINS, tmp_path / "state.json")
    registry = CommandRegistry(tmp_path / "q.db")
    manager.register_commands(registry)
    manager.enable("example", consent=True)
    assert registry.get("plugin:example:reverse") is not None

    manager.disable("example")
    assert manager.is_enabled("example") is False
    assert registry.get("plugin:example:reverse") is None
    with pytest.raises(PluginDisabled):
        manager.run("example", "reverse", "abc")
    registry.close()


def test_timeout_and_plugin_error_are_isolated(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    root.mkdir()
    _copy_example(root)
    _write_plugin(
        root,
        "slow_plugin",
        "slow",
        "import time\n"
        "from quickdeck.plugin_api import PluginAPI\n"
        "def register(api):\n"
        "    @api.register_command('slow')\n"
        "    def slow(ctx):\n"
        "        time.sleep(2)\n"
        "        return ctx.text\n",
        [{"id": "slow", "name": "慢命令", "input_type": "text", "output_type": "text"}],
    )
    _write_plugin(
        root,
        "boom_plugin",
        "boom",
        "from quickdeck.plugin_api import PluginAPI\n"
        "def register(api):\n"
        "    @api.register_command('explode')\n"
        "    def explode(ctx):\n"
        "        raise RuntimeError('boom from plugin')\n",
        [{"id": "explode", "name": "异常命令", "input_type": "text", "output_type": "text"}],
    )
    manager = _manager(root, tmp_path / "state.json", timeout=0.2)
    manager.enable("slow", consent=True)
    manager.enable("boom", consent=True)
    manager.enable("example", consent=True)

    started = time.monotonic()
    timed = manager.run("slow", "slow", "abc", timeout=0.2)
    elapsed = time.monotonic() - started
    assert timed.ok is False
    assert timed.timed_out is True
    assert "超时" in timed.error
    assert elapsed < 1.8

    failed = manager.run("boom", "explode", "abc")
    assert failed.ok is False
    assert "boom from plugin" in failed.error
    assert failed.returncode != 0
    assert "Traceback" in failed.stderr

    # 主进程和插件宿主仍可正常工作
    ok = manager.run("example", "reverse", "abc")
    assert ok.ok and ok.output == "cba"


def test_scan_is_lazy_and_does_not_execute_plugin(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    root.mkdir()
    marker = root / "marker.txt"
    _write_plugin(
        root,
        "lazy_plugin",
        "lazy",
        "from pathlib import Path\n"
        "Path('imported.flag').write_text('executed', encoding='utf-8')\n"
        "from quickdeck.plugin_api import PluginAPI\n"
        "def register(api):\n"
        "    @api.register_command('ping')\n"
        "    def ping(ctx):\n"
        "        return 'pong:' + ctx.text\n",
        [{"id": "ping", "name": "Ping", "input_type": "text", "output_type": "text"}],
    )
    manager = _manager(root, tmp_path / "state.json")
    plugin_dir = root / "lazy_plugin"
    assert (plugin_dir / "imported.flag").exists() is False
    manager.enable("lazy", consent=True)
    assert (plugin_dir / "imported.flag").exists() is False

    result = manager.run("lazy", "ping", "x")
    assert result.ok
    assert result.output == "pong:x"
    assert (plugin_dir / "imported.flag").exists()


def test_invalid_manifest_is_reported_without_crashing(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    root.mkdir()
    bad = root / "bad_plugin"
    bad.mkdir()
    (bad / "plugin.json").write_text("{not json", encoding="utf-8")
    manager = _manager(root, tmp_path / "state.json")
    info = manager.get("bad_plugin")
    assert info.valid is False
    assert "无法读取" in info.error
    with pytest.raises(Exception):
        manager.enable("bad_plugin", consent=True)
    assert manager.list_plugins(refresh=False)


def test_uninstall_removes_config_and_optional_files(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    root.mkdir()
    _copy_example(root)
    manager = _manager(root, tmp_path / "state.json")
    registry = CommandRegistry(tmp_path / "q.db")
    manager.register_commands(registry)
    manager.enable("example", consent=True)
    assert registry.get("plugin:example:char_count") is not None
    with pytest.raises(PluginConsentRequired):
        manager.uninstall("example")

    manager.uninstall("example", consent=True, remove_files=True)
    assert not (root / "example_plugin").exists()
    assert registry.get("plugin:example:char_count") is None
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert "example" not in state["plugins"]
    registry.close()


def test_plugin_stdout_does_not_corrupt_protocol(tmp_path: Path) -> None:
    root = tmp_path / "plugins"
    root.mkdir()
    _write_plugin(
        root,
        "noisy_plugin",
        "noisy",
        "from quickdeck.plugin_api import PluginAPI\n"
        "def register(api):\n"
        "    @api.register_command('hello')\n"
        "    def hello(ctx):\n"
        "        print('noise from plugin')\n"
        "        return 'clean:' + ctx.text\n",
        [{"id": "hello", "name": "Hello", "input_type": "text", "output_type": "text"}],
    )
    manager = _manager(root, tmp_path / "state.json")
    manager.enable("noisy", consent=True)
    result = manager.run("noisy", "hello", "x")
    assert result.ok
    assert result.output == "clean:x"
    assert "noise from plugin" in result.stderr
