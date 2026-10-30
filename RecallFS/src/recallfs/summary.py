"""文件摘要：只做可定位的原文摘录，不生成或模拟模型结论。"""

from __future__ import annotations

from typing import Any

NOTE = "原文摘录，不是模型生成的摘要；完整上下文请用 show 查看。"


def build_extract_summary(db: Any, file_row: Any, *, max_chars: int = 600) -> dict[str, Any]:
    """按 chunk 顺序截取开头文本，并返回每段的页码/行号定位。"""
    if max_chars <= 0:
        raise ValueError("max_chars 必须为正数")
    path = file_row["path"]
    name = file_row["name"]
    chunks = [
        chunk
        for chunk in db.chunks_for_file(int(file_row["id"]))
        if (chunk["kind"] or "") == "raw_text" and (chunk["text"] or "").strip()
    ]
    if not chunks:
        return {
            "path": path,
            "name": name,
            "excerpt": None,
            "locators": [],
            "truncated": False,
            "is_extract": True,
            "note": NOTE,
            "reason": "没有可提取的文本（可能为不支持格式、扫描件或解析失败）",
        }

    parts: list[str] = []
    locators: list[dict[str, Any]] = []
    used = 0
    truncated = False
    for chunk in chunks:
        text = chunk["text"].strip()
        if not text:
            continue
        remaining = max_chars - used
        if len(text) <= remaining:
            piece = text
        else:
            piece = text[:remaining]
            cut = piece.rfind("\n")
            if cut >= max_chars // 2:
                piece = piece[:cut]
            truncated = True
        if not piece.strip():
            break
        parts.append(piece.strip())
        used += len(piece.strip())
        locators.append(
            {
                "ordinal": chunk["ordinal"],
                "page": chunk["page"],
                "start_line": chunk["start_line"],
                "end_line": chunk["end_line"],
            }
        )
        if truncated:
            break

    return {
        "path": path,
        "name": name,
        "excerpt": "\n".join(parts),
        "locators": locators,
        "truncated": truncated,
        "is_extract": True,
        "note": NOTE,
        "reason": None,
    }


def format_locator(locator: dict[str, Any]) -> str:
    label = f"chunk {locator['ordinal']}"
    if locator.get("page"):
        label = f"第 {locator['page']} 页"
    start = locator.get("start_line")
    end = locator.get("end_line")
    if start and end and start != end:
        label += f" · 第 {start}–{end} 行"
    elif start:
        label += f" · 第 {start} 行"
    return label
