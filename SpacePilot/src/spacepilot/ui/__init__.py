"""GUI 包。

PySide6 的导入严格限制在 qt_app.py 内，供 __main__ 惰性加载；
未安装 PySide6 时不会影响 CLI 与核心库。
"""

from __future__ import annotations

__all__: list[str] = []
