"""RecallFS —— 本地离线文件检索与证据定位。

核心约束：
* 仅使用 Python 标准库即可完成索引、全文检索、证据定位；
* 可选依赖（pypdf / python-docx / python-pptx / PySide6）缺失时如实降级；
* 任何模块都不进行网络访问，也不修改被索引的原始文件。
"""

from __future__ import annotations

__version__ = "0.1.0"
__app_name__ = "RecallFS"

__all__ = ["__version__", "__app_name__"]
