from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from quickdeck.command_registry import CommandRegistry
from quickdeck.settings_store import SettingsError, SettingsStore


def test_defaults_and_persistence(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path)
    assert store.get("theme") == "light"
    assert store.get("hotkey") == "Ctrl+Alt+Space"
    assert store.get("clipboard.enabled") is False
    assert store.get("plugins.auto_enable") is False
    assert store.settings["data_dir"] == str(tmp_path.resolve())

    store.set("hotkey", "Ctrl+Shift+Space")
    store.update({"clipboard": {"enabled": True, "max_items": 7}})
    saved = json.loads(store.settings_file.read_text(encoding="utf-8"))
    assert saved["hotkey"] == "Ctrl+Shift+Space"
    assert saved["clipboard"]["enabled"] is True
    assert saved["clipboard"]["max_items"] == 7

    reloaded = SettingsStore(tmp_path)
    assert reloaded.get("clipboard.enabled") is True
    assert reloaded.get("clipboard.max_items") == 7
    assert reloaded.get("hotkey") == "Ctrl+Shift+Space"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("hotkey", "Ctrl+BadKey+Space"),
        ("hotkey", ""),
        ("theme", "dark"),
        ("launch_behavior", "bad"),
        ("search.max_results", 0),
        ("clipboard.max_items", 0),
        ("plugins.auto_enable", True),
        ("plugins.timeout_seconds", 0),
    ],
)
def test_invalid_settings_rejected(tmp_path: Path, key: str, value) -> None:
    store = SettingsStore(tmp_path)
    with pytest.raises(SettingsError):
        store.set(key, value)


def test_invalid_clipboard_regex_rejected(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path)
    with pytest.raises(SettingsError, match="排除规则"):
        store.set("clipboard.exclude_patterns", ["["])


def test_export_and_import_merge_and_replace(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path)
    store.set("clipboard.enabled", True)
    export = store.export_json(tmp_path / "export.json")
    assert export.exists()
    data = json.loads(export.read_text(encoding="utf-8"))
    assert data["clipboard"]["enabled"] is True

    other = tmp_path / "partial.json"
    other.write_text(
        json.dumps({"hotkey": "Ctrl+F8", "clipboard": {"max_items": 3}}),
        encoding="utf-8",
    )
    store.import_json(other, merge=True)
    assert store.get("hotkey") == "Ctrl+F8"
    assert store.get("clipboard.enabled") is True
    assert store.get("clipboard.max_items") == 3
    assert store.get("settings.data_dir", store.get("data_dir")) == str(tmp_path.resolve())

    store.import_json(other, merge=False)
    assert store.get("hotkey") == "Ctrl+F8"
    assert store.get("clipboard.enabled") is False
    assert store.get("clipboard.max_items") == 3
    assert store.get("data_dir") == str(tmp_path.resolve())


def test_export_roundtrip(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path)
    store.set("clipboard.enabled", True)
    path = store.export_json(tmp_path / "settings-export.json")
    imported = SettingsStore(tmp_path / "other").import_json(path)
    assert imported["clipboard"]["enabled"] is True
    assert imported["theme"] == "light"


def test_migration_is_idempotent(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path)
    db = tmp_path / "migrate.db"
    assert store.database_version(db) == 0
    assert store.migrate_database(db) == 2
    assert store.migrate_database(db) == 2
    assert store.database_version(db) == 2
    conn = sqlite3.connect(db)
    try:
        versions = [row[0] for row in conn.execute("SELECT version FROM schema_history")]
        assert versions == [1, 2]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"quickdeck_meta", "schema_history", "diagnostic_events"} <= tables
    finally:
        conn.close()


def test_database_backup_and_restore(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path)
    registry = CommandRegistry(store.db_path)
    registry.register_command("keep.me", "保留命令", handler=lambda ctx: ctx.text)
    registry.close()

    backup = store.backup_database()
    assert backup.exists()
    assert backup.parent == tmp_path / "backups"

    registry = CommandRegistry(store.db_path)
    registry.register_command("remove.me", "临时命令", handler=lambda ctx: ctx.text)
    registry.close()

    store.restore_database(backup)
    reopened = CommandRegistry(store.db_path)
    assert reopened.get("keep.me") is not None
    assert reopened.get("remove.me") is None
    reopened.close()

    explicit = tmp_path / "explicit-backup.db"
    store.backup_database(backup_path=explicit)
    assert explicit.exists()


def test_missing_backup_errors(tmp_path: Path) -> None:
    store = SettingsStore(tmp_path)
    with pytest.raises(SettingsError):
        store.backup_database()
    with pytest.raises(SettingsError):
        store.restore_database(tmp_path / "missing.db")
