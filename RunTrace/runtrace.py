"""源码检出目录的启动兼容层。

正式安装后应使用 ``src/runtrace``（setuptools 的 src 布局不会打包本文件）。
在项目根目录直接执行 ``python -m runtrace`` 时，本文件用内部别名加载
``src/runtrace``，再把启动逻辑交给真正的包入口，便于未安装项目时也能运行
GUI/CLI。安装后 wheel 不包含本文件。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PACKAGE_DIR = Path(__file__).resolve().parent / "src" / "runtrace"

if __name__ == "__main__":
    # 用内部别名加载源码包，避免 cwd 中的启动器与 `runtrace` 模块名冲突。
    spec = importlib.util.spec_from_file_location(
        "_runtrace_src",
        _PACKAGE_DIR / "__init__.py",
        submodule_search_locations=[str(_PACKAGE_DIR)],
    )
    if spec is None or spec.loader is None:
        raise SystemExit("无法加载 src/runtrace 源码包")
    package = importlib.util.module_from_spec(spec)
    sys.modules["_runtrace_src"] = package
    spec.loader.exec_module(package)
    from _runtrace_src.__main__ import main

    raise SystemExit(main())
else:
    # 从项目根目录 `import runtrace` 时，使本模块成为指向 src/runtrace 的
    # 包代理，子模块（runtrace.ui、runtrace.run_store 等）仍可正常导入。
    __path__ = [str(_PACKAGE_DIR)]
    exec(
        compile(
            (_PACKAGE_DIR / "__init__.py").read_text(encoding="utf-8"),
            str(_PACKAGE_DIR / "__init__.py"),
            "exec",
        ),
        globals(),
    )
