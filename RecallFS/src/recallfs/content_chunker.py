"""内容分块与代码符号抽取。

分块是“证据定位”的基础：每个 chunk 记录起止行号（或 PDF 页码），
查询命中后可以回到原始文件的确切位置。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

MAX_LINES = 40
MAX_CHARS = 1200
OVERLAP_LINES = 0


@dataclass
class Chunk:
    text: str
    kind: str = "raw_text"  # metadata / raw_text / summary / model
    start_line: int | None = None
    end_line: int | None = None
    page: int | None = None
    symbol: str | None = None


@dataclass
class Symbol:
    name: str
    kind: str
    line: int


_SYMBOL_PATTERNS: dict[str, tuple[tuple[str, str], ...]] = {
    ".py": (
        (r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)", "function"),
        (r"^\s*class\s+([A-Za-z_]\w*)", "class"),
    ),
    ".js": (
        (r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", "function"),
        (r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)", "class"),
        (r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function|\()", "function"),
    ),
    ".ts": (
        (r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", "function"),
        (r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)", "class"),
        (r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)", "interface"),
        (r"^\s*(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\s*=", "type"),
        (r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function|\()", "function"),
    ),
    ".java": (
        (r"^\s*(?:public|private|protected)?\s*(?:static\s+)?(?:final\s+)?(?:abstract\s+)?class\s+(\w+)", "class"),
        (r"^\s*(?:public|private|protected)\s+(?:static\s+)?(?:final\s+)?[\w<>\[\], ?]+\s+(\w+)\s*\(", "method"),
    ),
    ".c": (
        (r"^\s*(?:struct|enum|union)\s+(\w+)", "struct"),
        (r"^\s*(?:static\s+)?(?:inline\s+)?[\w\*\s]+?\s+(\w+)\s*\([^;]*\)\s*\{?\s*$", "function"),
    ),
    ".cpp": (
        (r"^\s*(?:class|struct|enum|union)\s+(\w+)", "class"),
        (r"^\s*(?:static\s+)?(?:inline\s+)?[\w:\*\s<>]+?\s+(\w+)\s*\([^;]*\)\s*\{?\s*$", "function"),
    ),
    ".h": (
        (r"^\s*(?:class|struct|enum|union)\s+(\w+)", "class"),
        (r"^\s*[\w\*\s]+?\s+(\w+)\s*\([^;]*\)\s*;", "declaration"),
    ),
    ".sql": (
        (r"(?i)\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW|INDEX)\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"\[]?(\w+)", "object"),
        (r"(?i)\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:FUNCTION|PROCEDURE)\s+(\w+)", "routine"),
    ),
    ".sh": (
        (r"^\s*function\s+(\w+)", "function"),
        (r"^\s*(\w+)\s*\(\)\s*\{", "function"),
    ),
    ".md": (
        (r"^(#{1,6})\s+(.+)$", "heading"),
    ),
}
_SYMBOL_PATTERNS[".jsx"] = _SYMBOL_PATTERNS[".js"]
_SYMBOL_PATTERNS[".tsx"] = _SYMBOL_PATTERNS[".ts"]
_SYMBOL_PATTERNS[".cc"] = _SYMBOL_PATTERNS[".cpp"]
_SYMBOL_PATTERNS[".cxx"] = _SYMBOL_PATTERNS[".cpp"]
_SYMBOL_PATTERNS[".hpp"] = _SYMBOL_PATTERNS[".cpp"]
_SYMBOL_PATTERNS[".bash"] = _SYMBOL_PATTERNS[".sh"]


def extract_symbols(text: str, ext: str) -> list[Symbol]:
    """按扩展名做简化的正则符号抽取（def/class/function 等）。"""
    patterns = _SYMBOL_PATTERNS.get(ext.casefold())
    if not patterns:
        return []
    found: list[Symbol] = []
    seen: set[tuple[str, int]] = set()
    for line_no, line in enumerate(text.splitlines(), start=1):
        for pattern, symbol_kind in patterns:
            match = re.search(pattern, line)
            if not match:
                continue
            if ext.casefold() == ".md":
                name = match.group(2).strip()
                symbol_kind = "heading"
            else:
                name = match.group(1).strip()
            if not name:
                continue
            key = (name.casefold(), line_no)
            if key in seen:
                continue
            seen.add(key)
            found.append(Symbol(name=name, kind=symbol_kind, line=line_no))
    return found


def _symbols_in_range(symbols: Sequence[Symbol] | None, start: int | None, end: int | None) -> str | None:
    if not symbols or start is None or end is None:
        return None
    names = [s.name for s in symbols if start <= s.line <= end]
    return ", ".join(dict.fromkeys(names)) if names else None


def chunk_text(
    text: str,
    *,
    kind: str = "raw_text",
    page: int | None = None,
    start_line: int = 1,
    max_lines: int = MAX_LINES,
    max_chars: int = MAX_CHARS,
    symbols: Sequence[Symbol] | None = None,
) -> list[Chunk]:
    """把一段文本按行聚合为 chunk；超长行按 max_chars 切分但仍记录同一行号。"""
    if not text:
        return []
    lines = text.split("\n")
    chunks: list[Chunk] = []
    buffer: list[str] = []
    buffer_start = start_line
    buffer_chars = 0

    def flush(end_line: int) -> None:
        nonlocal buffer, buffer_chars, buffer_start
        if not buffer:
            return
        body = "\n".join(buffer)
        if body.strip():
            chunks.append(
                Chunk(
                    text=body,
                    kind=kind,
                    start_line=buffer_start,
                    end_line=end_line,
                    page=page,
                    symbol=_symbols_in_range(symbols, buffer_start, end_line),
                )
            )
        buffer = []
        buffer_chars = 0

    for offset, line in enumerate(lines):
        line_no = start_line + offset
        if len(line) > max_chars:
            flush(line_no - 1)
            for piece_start in range(0, len(line), max_chars):
                piece = line[piece_start : piece_start + max_chars]
                chunks.append(
                    Chunk(
                        text=piece,
                        kind=kind,
                        start_line=line_no,
                        end_line=line_no,
                        page=page,
                        symbol=_symbols_in_range(symbols, line_no, line_no),
                    )
                )
            buffer_start = line_no + 1
            continue
        if not buffer:
            if not line.strip():
                buffer_start = line_no + 1
                continue
            buffer_start = line_no
        if len(buffer) >= max_lines or buffer_chars + len(line) + 1 > max_chars:
            flush(line_no - 1)
            if not line.strip():
                buffer_start = line_no + 1
                continue
            buffer_start = line_no
        buffer.append(line)
        buffer_chars += len(line) + 1
    flush(start_line + len(lines) - 1)
    return chunks


def chunk_pages(
    pages: Iterable[tuple[int | None, str]],
    *,
    max_lines: int = MAX_LINES,
    max_chars: int = MAX_CHARS,
) -> list[Chunk]:
    """按页分块（PDF 等），行号在该页内计数。"""
    chunks: list[Chunk] = []
    for page_no, text in pages:
        chunks.extend(
            chunk_text(text, page=page_no, max_lines=max_lines, max_chars=max_chars, start_line=1)
        )
    return chunks


def metadata_chunk(
    *,
    name: str,
    rel_path: str,
    ext: str,
    size: int,
    mtime: float,
    parse_status: str,
    parser: str | None = None,
    parse_error: str | None = None,
    truncated: bool = False,
) -> Chunk:
    """始终可检索的元数据片段（文件名/路径/扩展名/大小/时间/解析状态）。"""
    import datetime

    modified = datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"文件名: {name}",
        f"路径: {rel_path}",
        f"扩展名: {ext or '(无)'}",
        f"大小: {size} 字节",
        f"修改时间: {modified}",
        f"解析状态: {parse_status}",
    ]
    if parser:
        lines.append(f"解析器: {parser}")
    if truncated:
        lines.append("内容截断: 是（仅索引文件前部）")
    if parse_error:
        lines.append(f"解析说明: {parse_error}")
    return Chunk(text="\n".join(lines), kind="metadata", start_line=1, end_line=len(lines), page=None)
