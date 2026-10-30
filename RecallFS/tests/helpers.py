"""测试辅助函数（非测试用例）。"""

from __future__ import annotations

import os
from pathlib import Path


def write_file(root: str | os.PathLike[str], name: str, content: str | bytes, encoding: str = "utf-8") -> Path:
    path = Path(root) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_bytes(content.encode(encoding))
    return path


def _escape_pdf(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(path: str | os.PathLike[str], pages_text: list[str]) -> Path:
    """手工生成最小 PDF（Helvetica 文本），用于验证页码与文本提取。"""
    count = len(pages_text)
    font_id = 3 + 2 * count
    objects: dict[int, str] = {
        1: "<< /Type /Catalog /Pages 2 0 R >>",
        2: "<< /Type /Pages /Kids [" + " ".join(f"{3 + i} 0 R" for i in range(count)) + f"] /Count {count} >>",
    }
    for index, text in enumerate(pages_text):
        page_id = 3 + index
        content_id = 3 + count + index
        stream = f"BT /F1 18 Tf 72 720 Td ({_escape_pdf(text)}) Tj ET"
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>"
        )
        objects[content_id] = f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"
    objects[font_id] = "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    content = "%PDF-1.4\n"
    offsets: dict[int, int] = {}
    for object_id in sorted(objects):
        offsets[object_id] = len(content.encode("latin-1"))
        content += f"{object_id} 0 obj\n{objects[object_id]}\nendobj\n"
    xref_at = len(content.encode("latin-1"))
    max_id = max(objects)
    content += f"xref\n0 {max_id + 1}\n0000000000 65535 f \n"
    for object_id in range(1, max_id + 1):
        content += f"{offsets.get(object_id, 0):010d} 00000 n \n"
    content += f"trailer\n<< /Size {max_id + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content.encode("latin-1"))
    return target
