"""程序入口：优先 GUI（PySide6 惰性导入）；不可用时回退 CLI。"""

from __future__ import annotations

import sys


def _cli_help(reason: str | None = None) -> int:
    if reason:
        print(reason, file=sys.stderr)
    from .cli import build_parser

    build_parser().print_help()
    return 0


def _run_gui() -> int:
    try:
        from .ui.qt_app import run_gui
    except Exception as exc:  # ImportError / DLL 缺失 / 无 Qt 平台插件
        return _cli_help(f"GUI 不可用（{type(exc).__name__}: {exc}），已回退到 CLI。")
    try:
        return int(run_gui())
    except Exception as exc:  # pragma: no cover - 依赖图形环境
        return _cli_help(f"GUI 启动失败（{type(exc).__name__}: {exc}），已回退到 CLI。")


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("--gui", "--ui"):
        return _run_gui()
    if not args:
        return _run_gui()

    from .cli import main as cli_main

    return cli_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
