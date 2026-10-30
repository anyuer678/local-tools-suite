"""诊断日志与运行环境信息。"""

from __future__ import annotations

import datetime as _dt
import importlib.util
import json
import logging
import os
import platform
import sys
import threading
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

_LOGGER_NAME = "quickdeck"
_DIAG_LOCK = threading.RLock()


def log_directory(data_dir: str | Path) -> Path:
    return Path(data_dir).expanduser() / "logs"


def setup_logging(
    data_dir: str | Path,
    *,
    level: int = logging.INFO,
) -> logging.Logger:
    directory = log_directory(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / "quickdeck.log"
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(level)
    with _DIAG_LOCK:
        for handler in list(logger.handlers):
            if getattr(handler, "_quickdeck_handler", False):
                logger.removeHandler(handler)
                handler.close()
        handler = RotatingFileHandler(
            log_path,
            maxBytes=1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s [%(name)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        handler._quickdeck_handler = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
        logger.propagate = False
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    suffix = f".{name}" if name else ""
    return logging.getLogger(_LOGGER_NAME + suffix)


def log_event(
    data_dir: str | Path,
    event: str,
    **details: Any,
) -> Path:
    """追加一条 JSONL 诊断事件，同时写日志。"""

    path = Path(data_dir).expanduser() / "diagnostics.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "event": str(event),
        "details": details,
    }
    with _DIAG_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    get_logger("diagnostics").info("%s %s", event, details)
    return path


def log_exception(
    data_dir: str | Path,
    event: str,
    exc: BaseException,
    **details: Any,
) -> Path:
    return log_event(
        data_dir,
        event,
        error=type(exc).__name__,
        message=str(exc),
        traceback=traceback.format_exception(type(exc), exc, exc.__traceback__),
        **details,
    )


def collect_diagnostics(data_dir: str | Path | None = None) -> dict[str, Any]:
    root = Path(data_dir).expanduser() if data_dir else None
    db_path = root / "quickdeck.db" if root else None
    settings_path = root / "settings.json" if root else None
    info: dict[str, Any] = {
        "timestamp": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "python": sys.version,
        "python_executable": sys.executable,
        "cwd": os.getcwd(),
        "windows": sys.platform == "win32",
        "pyside6_available": importlib.util.find_spec("PySide6") is not None,
        "data_dir": str(root) if root else "",
        "database_exists": bool(db_path and db_path.exists()),
        "database_size": db_path.stat().st_size if db_path and db_path.exists() else 0,
        "settings_exists": bool(settings_path and settings_path.exists()),
        "log_path": str(log_directory(root) / "quickdeck.log") if root else "",
    }
    if root:
        info["diagnostics_file"] = str(root / "diagnostics.jsonl")
    return info
