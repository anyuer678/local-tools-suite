"""设置、数据目录、JSON 导入导出与 SQLite 备份/迁移/恢复。"""

from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping


class SettingsError(RuntimeError):
    pass


DEFAULT_SETTINGS: dict[str, Any] = {
    "hotkey": "Ctrl+Alt+Space",
    "theme": "light",
    "launch_behavior": "tray",
    "data_dir": "",
    "search": {
        "max_results": 30,
        "fuzzy": True,
        "case_sensitive": False,
    },
    "clipboard": {
        "enabled": False,
        "max_items": 50,
        "exclude_patterns": [],
    },
    "plugins": {
        "timeout_seconds": 5.0,
        "auto_enable": False,
    },
    "startup": {
        "open_at_login": False,
    },
    "external_commands": [],
}


def default_data_dir() -> Path:
    env = os.environ.get("QUICKDECK_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        if base:
            return (Path(base) / "QuickDeck").resolve()
    return (Path.home() / ".quickdeck").resolve()


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if (
            isinstance(value, Mapping)
            and isinstance(result.get(key), dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class SettingsStore:
    """以 JSON 文件保存设置，SQLite 位于同一数据目录。"""

    def __init__(
        self,
        data_dir: str | Path | None = None,
        *,
        settings_file: str | Path | None = None,
        db_path: str | Path | None = None,
    ) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve() if data_dir else default_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings_file = (
            Path(settings_file).expanduser()
            if settings_file
            else self.data_dir / "settings.json"
        )
        self.db_path = Path(db_path).expanduser() if db_path else self.data_dir / "quickdeck.db"
        self._lock = threading.RLock()
        self._settings = self._load()

    @property
    def settings(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._settings)

    # ---------------------------------------------------------------- load/save
    def _load(self) -> dict[str, Any]:
        defaults = copy.deepcopy(DEFAULT_SETTINGS)
        defaults["data_dir"] = str(self.data_dir)
        if not self.settings_file.exists():
            return defaults
        try:
            raw = json.loads(self.settings_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SettingsError(f"设置文件损坏：{exc}") from exc
        if not isinstance(raw, dict):
            raise SettingsError("设置文件顶层必须是对象")
        merged = _deep_merge(defaults, raw)
        return self.validate(merged)

    def save(self, settings: Mapping[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            candidate = (
                _deep_merge(DEFAULT_SETTINGS, settings)
                if settings is not None
                else copy.deepcopy(self._settings)
            )
            candidate["data_dir"] = str(self.data_dir)
            validated = self.validate(candidate)
            payload = json.dumps(validated, ensure_ascii=False, indent=2)
            self.settings_file.parent.mkdir(parents=True, exist_ok=True)
            temp = self.settings_file.with_suffix(self.settings_file.suffix + ".tmp")
            temp.write_text(payload, encoding="utf-8")
            temp.replace(self.settings_file)
            self._settings = validated
            return copy.deepcopy(validated)

    def validate(self, settings: Mapping[str, Any]) -> dict[str, Any]:
        merged = _deep_merge(DEFAULT_SETTINGS, settings)
        if merged.get("theme") != "light":
            raise SettingsError("当前版本只支持浅色主题（theme=light）")
        hotkey = merged.get("hotkey")
        if not isinstance(hotkey, str) or not hotkey.strip():
            raise SettingsError("快捷键不能为空")
        from .hotkey_manager import HotkeyParseError, parse_hotkey

        try:
            parse_hotkey(hotkey)
        except HotkeyParseError as exc:
            raise SettingsError(f"快捷键无效：{exc}") from exc
        behavior = merged.get("launch_behavior")
        if behavior not in {"tray", "window", "background"}:
            raise SettingsError("launch_behavior 必须是 tray/window/background")
        search = merged.get("search")
        if not isinstance(search, dict):
            raise SettingsError("search 必须是对象")
        try:
            max_results = int(search.get("max_results", 30))
        except (TypeError, ValueError) as exc:
            raise SettingsError("search.max_results 必须是整数") from exc
        if max_results < 1 or max_results > 500:
            raise SettingsError("search.max_results 必须在 1..500")
        search["max_results"] = max_results
        clipboard = merged.get("clipboard")
        if not isinstance(clipboard, dict):
            raise SettingsError("clipboard 必须是对象")
        if not isinstance(clipboard.get("enabled"), bool):
            raise SettingsError("clipboard.enabled 必须是布尔值")
        try:
            max_items = int(clipboard.get("max_items", 50))
        except (TypeError, ValueError) as exc:
            raise SettingsError("clipboard.max_items 必须是整数") from exc
        if max_items < 1:
            raise SettingsError("clipboard.max_items 必须大于 0")
        clipboard["max_items"] = max_items
        patterns = clipboard.get("exclude_patterns", [])
        if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
            raise SettingsError("clipboard.exclude_patterns 必须是字符串数组")
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise SettingsError(f"剪贴板排除规则无效：{pattern}（{exc}）") from exc
        plugins = merged.get("plugins")
        if not isinstance(plugins, dict):
            raise SettingsError("plugins 必须是对象")
        if plugins.get("auto_enable") is not False:
            raise SettingsError("出于安全考虑，插件不得自动启用")
        try:
            timeout = float(plugins.get("timeout_seconds", 5.0))
        except (TypeError, ValueError) as exc:
            raise SettingsError("plugins.timeout_seconds 必须是数字") from exc
        if timeout <= 0:
            raise SettingsError("plugins.timeout_seconds 必须大于 0")
        plugins["timeout_seconds"] = timeout
        startup = merged.get("startup")
        if not isinstance(startup, dict) or not isinstance(startup.get("open_at_login"), bool):
            raise SettingsError("startup.open_at_login 必须是布尔值")
        external = merged.get("external_commands")
        if not isinstance(external, list):
            raise SettingsError("external_commands 必须是数组")
        merged["data_dir"] = str(merged.get("data_dir") or self.data_dir)
        return merged

    # -------------------------------------------------------------- get/set
    def get(self, key: str, default: Any = None) -> Any:
        parts = key.split(".") if key else []
        with self._lock:
            value: Any = self._settings
            for part in parts:
                if not isinstance(value, dict) or part not in value:
                    return default
                value = value[part]
            return copy.deepcopy(value)

    def set(self, key: str, value: Any, *, save: bool = True) -> dict[str, Any]:
        if not key:
            raise SettingsError("设置键不能为空")
        candidate = self.settings
        parts = key.split(".")
        target = candidate
        for part in parts[:-1]:
            if not isinstance(target.get(part), dict):
                target[part] = {}
            target = target[part]
        target[parts[-1]] = copy.deepcopy(value)
        if save:
            return self.save(candidate)
        validated = self.validate(candidate)
        with self._lock:
            self._settings = validated
        return copy.deepcopy(validated)

    def update(self, values: Mapping[str, Any], *, save: bool = True) -> dict[str, Any]:
        candidate = _deep_merge(self.settings, values)
        if save:
            return self.save(candidate)
        validated = self.validate(candidate)
        with self._lock:
            self._settings = validated
        return copy.deepcopy(validated)

    # ---------------------------------------------------------- import/export
    def export_json(self, path: str | Path) -> Path:
        destination = Path(path).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            payload = json.dumps(self._settings, ensure_ascii=False, indent=2)
        destination.write_text(payload, encoding="utf-8")
        return destination

    def import_json(self, path: str | Path, *, merge: bool = True) -> dict[str, Any]:
        source = Path(path).expanduser()
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SettingsError(f"无法导入设置：{exc}") from exc
        if not isinstance(raw, dict):
            raise SettingsError("导入设置顶层必须是对象")
        if "settings" in raw and isinstance(raw["settings"], dict):
            raw = raw["settings"]
        candidate = (
            _deep_merge(self.settings, raw)
            if merge
            else _deep_merge(DEFAULT_SETTINGS, raw)
        )
        candidate["data_dir"] = str(self.data_dir)
        return self.save(candidate)

    # --------------------------------------------------------------- database
    def migrate_database(self, db_path: str | Path | None = None) -> int:
        path = Path(db_path).expanduser() if db_path else self.db_path
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS quickdeck_meta "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_history "
                "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
            )
            conn.commit()
            row = conn.execute(
                "SELECT value FROM quickdeck_meta WHERE key = 'schema_version'"
            ).fetchone()
            current = int(row[0]) if row else 0
            if current < 1:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_history "
                    "(version INTEGER PRIMARY KEY, applied_at REAL NOT NULL)"
                )
                conn.execute(
                    "INSERT OR IGNORE INTO schema_history(version, applied_at) VALUES (1, ?)",
                    (time.time(),),
                )
                current = 1
            if current < 2:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS diagnostic_events "
                    "(id INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL, "
                    "payload TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL)"
                )
                conn.execute(
                    "INSERT OR IGNORE INTO schema_history(version, applied_at) VALUES (2, ?)",
                    (time.time(),),
                )
                current = 2
            conn.execute(
                "INSERT INTO quickdeck_meta(key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(current),),
            )
            conn.commit()
            return current
        finally:
            conn.close()

    def database_version(self, db_path: str | Path | None = None) -> int:
        path = Path(db_path).expanduser() if db_path else self.db_path
        if not path.exists():
            return 0
        conn = sqlite3.connect(str(path))
        try:
            try:
                row = conn.execute(
                    "SELECT value FROM quickdeck_meta WHERE key = 'schema_version'"
                ).fetchone()
            except sqlite3.DatabaseError:
                return 0
            return int(row[0]) if row else 0
        finally:
            conn.close()

    def backup_database(
        self,
        db_path: str | Path | None = None,
        backup_path: str | Path | None = None,
    ) -> Path:
        source_path = Path(db_path).expanduser() if db_path else self.db_path
        if not source_path.exists():
            raise SettingsError(f"数据库不存在：{source_path}")
        if backup_path:
            destination = Path(backup_path).expanduser()
        else:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            destination = self.data_dir / "backups" / f"quickdeck-{stamp}.db"
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(str(source_path))
        target = sqlite3.connect(str(destination))
        try:
            with target:
                source.backup(target)
        finally:
            target.close()
            source.close()
        return destination

    def restore_database(
        self,
        backup_path: str | Path,
        db_path: str | Path | None = None,
    ) -> Path:
        source_path = Path(backup_path).expanduser()
        if not source_path.exists():
            raise SettingsError(f"备份文件不存在：{source_path}")
        target_path = Path(db_path).expanduser() if db_path else self.db_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(str(source_path))
        target = sqlite3.connect(str(target_path))
        try:
            with target:
                source.backup(target)
        except sqlite3.DatabaseError as exc:
            raise SettingsError(f"恢复数据库失败：{exc}") from exc
        finally:
            target.close()
            source.close()
        return target_path
