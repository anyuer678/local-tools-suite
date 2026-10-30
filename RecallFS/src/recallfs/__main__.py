"""入口：有 PySide6 且未指定子命令时启动 GUI，否则走命令行。

GUI 采用惰性导入：``recallfs.ui.qt_app`` 内部才 import PySide6。
PySide6 缺失时这里捕获 ImportError 并回退到 CLI，绝不崩溃。
"""

from __future__ import annotations

import argparse
import importlib
import sys
from typing import Sequence

GUI_MODULE = "recallfs.ui.qt_app"


def _load_gui():
    return importlib.import_module(GUI_MODULE)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] not in ("gui",):
        from .cli import main as cli_main

        return cli_main(args)

    db_path: str | None = None
    if args:
        parser = argparse.ArgumentParser(prog="recallfs gui", description="启动 RecallFS 图形界面")
        parser.add_argument("--db", default=None, help="索引数据库路径")
        try:
            namespace = parser.parse_args(args[1:])
        except SystemExit:
            from .cli import main as cli_main

            return cli_main(["--help"])
        db_path = namespace.db

    try:
        module = _load_gui()
    except Exception as exc:
        print(
            f"GUI 不可用（{type(exc).__name__}: {exc}），已回退到命令行。",
            file=sys.stderr,
        )
        from .cli import main as cli_main

        return cli_main(["--help"])

    return int(module.run_gui(db_path) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
