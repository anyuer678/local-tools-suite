"""源码检出目录的启动兼容层（不进入 wheel）。

安装后请使用 ``python -m envforge`` 或 ``envforge`` 命令。在项目根目录直接
``python envforge.py`` 时，本文件以内部别名加载 ``src/envforge`` 包。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent / "src" / "envforge"

if __name__ == "__main__":
    spec = importlib.util.spec_from_file_location(
        "_envforge_src",
        _PACKAGE_DIR / "__init__.py",
        submodule_search_locations=[str(_PACKAGE_DIR)],
    )
    if spec is None or spec.loader is None:
        raise SystemExit("无法加载 src/envforge 源码包")
    package = importlib.util.module_from_spec(spec)
    sys.modules["_envforge_src"] = package
    spec.loader.exec_module(package)
    from _envforge_src.__main__ import main

    raise SystemExit(main())
else:
    __path__ = [str(_PACKAGE_DIR)]
    exec(
        compile(
            (_PACKAGE_DIR / "__init__.py").read_text(encoding="utf-8"),
            str(_PACKAGE_DIR / "__init__.py"),
            "exec",
        ),
        globals(),
    )
