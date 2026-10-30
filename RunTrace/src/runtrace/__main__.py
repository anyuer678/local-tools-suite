"""入口：优先 GUI，PySide6 缺失时自动回退 CLI。"""

from __future__ import annotations

import sys


def _launch_gui(argv: list[str]) -> int:
    from .ui.qt_app import launch

    return int(launch(argv) or 0)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if args and args[0] != "--gui":
        # 任何显式 CLI 参数（含 --help）都不导入 PySide6。
        from .cli import main as cli_main

        return cli_main(args)

    if args and args[0] == "--gui":
        args = args[1:]
        try:
            return _launch_gui(args)
        except ImportError:
            print(
                "未安装 PySide6，无法启动图形界面。可安装 PySide6 或使用 CLI："
                "python -m runtrace --help",
                file=sys.stderr,
            )
            return 2
        except Exception as exc:
            print(f"图形界面启动失败: {exc}", file=sys.stderr)
            return 2

    # 无参数：先尝试 GUI；PySide6 不可用/启动失败时回退到可用的 CLI。
    try:
        return _launch_gui(args)
    except ImportError:
        print(
            "未检测到可用的 PySide6，已自动回退到 CLI。"
            "安装 PySide6 可启用图形界面（pip install PySide6）。",
            file=sys.stderr,
        )
        from .cli import main as cli_main

        return cli_main(["--help"])
    except Exception as exc:
        print(f"图形界面不可用（{exc}），已自动回退到 CLI。", file=sys.stderr)
        from .cli import main as cli_main

        return cli_main(["--help"])


if __name__ == "__main__":
    raise SystemExit(main())
