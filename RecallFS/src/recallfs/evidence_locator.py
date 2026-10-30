"""证据定位：把搜索结果多带回到原始文件的真实行号/页码，并校验索引是否仍然有效。

校验策略
--------
1. 文件是否存在；
2. 当前 size/mtime 是否与索引时一致；
3. 对可解析文本，重新读取当前文件并检查 chunk 文本是否仍存在；
4. PDF 按页码重新提取文本比对。

任一环节失败都会给出 ``stale_reason``，旧 chunk 在查询时会被过滤或标注为 stale。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .document_parser import live_text_for_evidence
from .fulltext_index import IndexDatabase

_WS_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


@dataclass
class Evidence:
    file_id: int | None
    path: str
    name: str
    chunk_id: int | None
    kind: str
    start_line: int | None
    end_line: int | None
    page: int | None
    snippet: str
    exists: bool
    stale: bool
    stale_reason: str | None
    indexed_at: float | None
    parse_status: str | None
    verified_at: float
    current_size: int | None = None
    current_mtime: float | None = None
    content_verified: bool | None = None
    live_lines: list[tuple[int, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "path": self.path,
            "name": self.name,
            "chunk_id": self.chunk_id,
            "kind": self.kind,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "page": self.page,
            "snippet": self.snippet,
            "exists": self.exists,
            "stale": self.stale,
            "stale_reason": self.stale_reason,
            "indexed_at": self.indexed_at,
            "parse_status": self.parse_status,
            "verified_at": self.verified_at,
            "current_size": self.current_size,
            "current_mtime": self.current_mtime,
            "content_verified": self.content_verified,
            "live_lines": self.live_lines,
        }


class EvidenceLocator:
    def __init__(self, db: IndexDatabase) -> None:
        self.db = db

    # ------------------------------------------------------------------
    def check_file(self, path: str, size: int | None, mtime: float | None) -> tuple[bool, bool, str | None, os.stat_result | None]:
        """快速校验：返回 (exists, stale, reason, stat)。"""
        try:
            stat = os.stat(path)
        except OSError as exc:
            return False, True, f"文件不存在或不可访问: {exc.strerror or exc}", None
        if size is not None and stat.st_size != size:
            return True, True, "文件大小已变化，旧索引片段失效", stat
        if mtime is not None and abs(stat.st_mtime - mtime) > 1e-6:
            return True, True, "文件修改时间已变化，旧索引片段失效", stat
        return True, False, None, stat

    def locate(
        self,
        *,
        path: str,
        name: str = "",
        kind: str = "raw_text",
        chunk_text: str = "",
        start_line: int | None = None,
        end_line: int | None = None,
        page: int | None = None,
        file_id: int | None = None,
        chunk_id: int | None = None,
        indexed_at: float | None = None,
        parse_status: str | None = None,
        size: int | None = None,
        mtime: float | None = None,
        snippet: str = "",
    ) -> Evidence:
        import time

        exists, stale, reason, stat = self.check_file(path, size, mtime)
        verified: bool | None = None
        live_lines: list[tuple[int, str]] = []
        if exists and not stale and kind == "raw_text" and chunk_text:
            live_text, error = live_text_for_evidence(path, page=page)
            if error:
                verified = None
                reason = f"无法重新读取文件进行证据校验: {error}"
            else:
                target = _normalize(chunk_text)
                probe = target[:200]
                if probe and probe not in _normalize(live_text):
                    stale = True
                    reason = "当前文件内容与索引片段不一致（文件已被修改）"
                verified = not stale
                if start_line is not None and not stale:
                    live_lines = self.read_around(path, start_line, end_line, page=page)
        elif exists and not stale and kind == "metadata":
            verified = True
        if stat is not None:
            current_size, current_mtime = stat.st_size, stat.st_mtime
        else:
            current_size, current_mtime = None, None
        return Evidence(
            file_id=file_id,
            path=path,
            name=name,
            chunk_id=chunk_id,
            kind=kind,
            start_line=start_line,
            end_line=end_line,
            page=page,
            snippet=snippet or chunk_text[:200],
            exists=exists,
            stale=stale,
            stale_reason=reason,
            indexed_at=indexed_at,
            parse_status=parse_status,
            verified_at=time.time(),
            current_size=current_size,
            current_mtime=current_mtime,
            content_verified=verified,
            live_lines=live_lines,
        )

    def locate_hit(self, hit: Any, *, size: int | None = None, mtime: float | None = None) -> Evidence:
        getter = hit.get if isinstance(hit, dict) else lambda key, default=None: getattr(hit, key, default)
        return self.locate(
            path=getter("path", "") or "",
            name=getter("name", "") or "",
            kind=getter("kind", "raw_text") or "raw_text",
            chunk_text=getter("text", "") or "",
            start_line=getter("start_line"),
            end_line=getter("end_line"),
            page=getter("page"),
            file_id=getter("file_id"),
            chunk_id=getter("chunk_id"),
            indexed_at=getter("indexed_at"),
            parse_status=getter("parse_status"),
            size=size,
            mtime=mtime,
            snippet=getter("snippet", "") or "",
        )

    # ------------------------------------------------------------------
    def read_around(
        self,
        path: str,
        start_line: int,
        end_line: int | None = None,
        *,
        page: int | None = None,
        radius: int = 2,
    ) -> list[tuple[int, str]]:
        text, error = live_text_for_evidence(path, page=page)
        if error:
            return []
        lines = text.split("\n")
        if page is not None:
            return [(index + 1, line) for index, line in enumerate(lines)]
        begin = max(1, start_line - radius)
        finish = min(len(lines), (end_line or start_line) + radius)
        return [(number, lines[number - 1]) for number in range(begin, finish + 1)]

    def verify_many(self, rows: Iterable[Any]) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for row in rows:
            exists, stale, reason, stat = self.check_file(row["path"], row["size"], row["mtime"])
            results.append(
                {
                    "file_id": int(row["id"]),
                    "path": row["path"],
                    "exists": exists,
                    "stale": stale,
                    "reason": reason,
                    "current_size": stat.st_size if stat else None,
                    "current_mtime": stat.st_mtime if stat else None,
                }
            )
        return results
