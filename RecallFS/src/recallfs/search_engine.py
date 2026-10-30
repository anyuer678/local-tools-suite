"""多层检索：精确文件名、文件属性筛选、FTS5 全文（bm25）、代码符号、可选语义。

结果始终携带真实路径、chunk 起止行/页码与索引时间，并区分 chunk 的 kind
（metadata / raw_text / summary / model）。查询时校验文件当前 mtime/size，
旧片段会被过滤或明确标注为 stale。
"""

from __future__ import annotations

import dataclasses
import html
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .content_chunker import MAX_CHARS
from .evidence_locator import EvidenceLocator
from .filesystem_watcher import path_key
from .fulltext_index import IndexDatabase, expand_for_fts
from .project_collections import CollectionStore, RecentSearchStore
from .semantic_index import SemanticIndex

PER_FILE_HITS = 3
SNIPPET_WIDTH = 200


@dataclass
class SearchFilters:
    roots: list[str] | None = None
    collections: list[str] | None = None
    exts: list[str] | None = None
    kinds: list[str] | None = None  # metadata / raw_text / summary / model
    symbols: list[str] | None = None
    name_contains: str | None = None
    path_contains: str | None = None
    mtime_from: float | None = None
    mtime_to: float | None = None
    size_min: int | None = None
    size_max: int | None = None
    parse_status: list[str] | None = None
    include_stale: bool = False
    exact_name: bool = True

    def to_dict(self) -> dict[str, Any]:
        payload = dataclasses.asdict(self)
        return {key: value for key, value in payload.items() if value not in (None, [], False)}


@dataclass
class SearchHit:
    file_id: int
    chunk_id: int | None
    path: str
    name: str
    kind: str
    layer: str
    text: str
    snippet: str
    highlighted_name: str
    start_line: int | None
    end_line: int | None
    page: int | None
    score: float
    matched_terms: list[str]
    matched_field: str
    stale: bool
    stale_reason: str | None
    parse_status: str
    parse_error: str | None
    indexed_at: float | None
    size: int
    mtime: float
    symbol: str | None = None
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class SearchResult:
    query: str
    mode: str
    hits: list[SearchHit] = field(default_factory=list)
    total: int = 0
    empty_reason: str | None = None
    elapsed_ms: float = 0.0
    semantic: dict[str, Any] | None = None
    warning: str | None = None
    filters: dict[str, Any] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.hits

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "mode": self.mode,
            "hits": [hit.to_dict() for hit in self.hits],
            "total": self.total,
            "empty": self.empty,
            "empty_reason": self.empty_reason,
            "elapsed_ms": self.elapsed_ms,
            "semantic": self.semantic,
            "warning": self.warning,
            "filters": self.filters,
        }


@dataclass
class QuerySpec:
    raw: str
    terms: list[str] = field(default_factory=list)
    excludes: list[str] = field(default_factory=list)
    expression: str = ""

    @property
    def all_terms(self) -> list[str]:
        return list(self.terms)


_TOKEN_RE = re.compile(r'"([^"]*)"|(\S+)')
_FTS_SPECIAL_RE = re.compile(r'["*^:()\[\]{}~]')


def parse_query(raw: str) -> QuerySpec:
    spec = QuerySpec(raw=raw.strip())
    parts: list[str] = []
    if not spec.raw:
        return spec
    for match in _TOKEN_RE.finditer(spec.raw):
        phrase = match.group(1)
        word = match.group(2)
        if phrase is not None:
            cleaned = phrase.strip()
            if cleaned:
                spec.terms.append(cleaned)
                parts.append(_fts_phrase(cleaned))
            continue
        if word is None:
            continue
        if word.upper() == "OR":
            # 保持简单：OR 不改变 AND 语义，但记录在原始查询中
            continue
        if word.startswith("-") and len(word) > 1:
            cleaned = _sanitize(word[1:])
            if cleaned:
                spec.excludes.append(cleaned)
                parts.append(f'NOT "{_expand_quote(cleaned)}"')
            continue
        cleaned = _sanitize(word)
        if cleaned:
            spec.terms.append(cleaned)
            parts.append(_fts_phrase(cleaned))
    spec.expression = " ".join(parts)
    return spec


def _sanitize(text: str) -> str:
    return _FTS_SPECIAL_RE.sub(" ", text).strip()


def _expand_quote(text: str) -> str:
    return expand_for_fts(text).replace('"', " ")


def _fts_phrase(text: str) -> str:
    expanded = _expand_quote(text)
    return f'"{expanded}"' if expanded else '""'


def _highlight_terms(text: str, terms: Sequence[str]) -> tuple[str, list[str]]:
    """在转义后的文本中高亮所有词/短语，返回 ``(html, 实际命中的词)``。"""
    if not text:
        return "", []
    escaped = html.escape(text, quote=False)
    matched: list[str] = []
    for term in sorted({t for t in terms if t}, key=len, reverse=True):
        needle = html.escape(term, quote=False)
        pattern = re.compile(re.escape(needle), re.IGNORECASE)
        if pattern.search(escaped):
            escaped = pattern.sub(lambda m: f"<mark>{m.group(0)}</mark>", escaped)
            matched.append(term)
    return escaped, matched


def make_snippet(text: str, terms: Sequence[str], width: int = SNIPPET_WIDTH) -> tuple[str, str | None, list[str]]:
    """截取命中附近的上下文并高亮。返回 ``(snippet_html, matched_term, matched_terms)``。"""
    if not text:
        return "", None, []
    lowered = text.casefold()
    anchor = -1
    anchor_term: str | None = None
    for term in sorted({t for t in terms if t}, key=len, reverse=True):
        position = lowered.find(term.casefold())
        if position >= 0 and (anchor < 0 or position < anchor):
            anchor = position
            anchor_term = term
    if anchor < 0:
        start = 0
    else:
        start = max(0, anchor - width // 3)
    end = min(len(text), start + width)
    snippet = text[start:end]
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    highlighted, matched = _highlight_terms(snippet, terms)
    return f"{prefix}{highlighted}{suffix}", anchor_term, matched


class SearchEngine:
    def __init__(
        self,
        db: IndexDatabase,
        *,
        semantic: SemanticIndex | None = None,
        collections: CollectionStore | None = None,
        recent: RecentSearchStore | None = None,
    ) -> None:
        self.db = db
        self.semantic = semantic if semantic is not None else SemanticIndex(db)
        self.collections = collections
        self.recent = recent
        self.locator = EvidenceLocator(db)
        self._had_stale = False

    # ------------------------------------------------------------------
    # 公共入口
    # ------------------------------------------------------------------
    def search(
        self,
        query: str,
        filters: SearchFilters | None = None,
        *,
        mode: str = "fulltext",
        limit: int | None = None,
        offset: int = 0,
        record_recent: bool = True,
    ) -> SearchResult:
        started = time.perf_counter()
        filters = filters or SearchFilters()
        limit = max(1, limit if limit is not None else 20)
        query = (query or "").strip()
        if mode == "semantic":
            result = self._semantic_search(query, filters, limit, offset)
        else:
            result = self._fulltext_search(query, filters, limit, offset)
        result.elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        result.filters = filters.to_dict()
        if self.recent is not None and query and record_recent:
            try:
                self.recent.record(query, result.filters, len(result.hits))
            except Exception:
                pass
        return result

    def count(self, filters: SearchFilters | None = None) -> int:
        filters = filters or SearchFilters()
        clauses, params = self._where(filters)
        sql = "SELECT COUNT(*) FROM files f"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        return int(self.db.scalar(sql, params) or 0)

    def list_files(self, filters: SearchFilters | None = None, limit: int = 100) -> list[dict[str, Any]]:
        filters = filters or SearchFilters()
        clauses, params = self._where(filters)
        sql = "SELECT f.* FROM files f"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY f.path_key LIMIT ?"
        rows = self.db.query(sql, [*params, limit])
        return [self._file_row_to_dict(row) for row in rows]

    # ------------------------------------------------------------------
    # 全文 + 精确 + 筛选
    # ------------------------------------------------------------------
    def _fulltext_search(self, query: str, filters: SearchFilters, limit: int, offset: int) -> SearchResult:
        result = SearchResult(query=query, mode="fulltext")
        if not query:
            result.empty_reason = "empty_query"
            result.semantic = self._semantic_meta()
            return result
        if self.db.file_count() == 0:
            result.empty_reason = "empty_index"
            result.semantic = self._semantic_meta()
            return result

        spec = parse_query(query)
        clauses, params = self._where(filters)

        exact_hits: list[SearchHit] = []
        if filters.exact_name and (not filters.kinds or "metadata" in filters.kinds):
            exact_hits = self._exact_name_hits(query, clauses, params)

        hits: list[SearchHit] = list(exact_hits)
        seen_chunks = {(hit.file_id, hit.chunk_id) for hit in hits}
        per_file: dict[int, int] = {}
        for hit in hits:
            per_file[hit.file_id] = per_file.get(hit.file_id, 0) + 1

        if spec.terms:
            sql = (
                "SELECT c.id AS chunk_id, c.file_id, c.kind AS chunk_kind, c.start_line, c.end_line, c.page,"
                " c.text, c.symbol, c.ordinal,"
                " bm25(chunks_fts, 1.0, 6.0, 3.0, 3.0) AS rank,"
                " f.path, f.name, f.ext, f.size, f.mtime, f.parse_status, f.parse_error, f.indexed_at,"
                " f.truncated, f.kind AS file_kind"
                " FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid JOIN files f ON f.id=c.file_id"
                " WHERE chunks_fts MATCH ?"
            )
            chunk_clauses: list[str] = []
            chunk_params: list[Any] = []
            if filters.kinds:
                placeholders = ",".join("?" for _ in filters.kinds)
                chunk_clauses.append(f"c.kind IN ({placeholders})")
                chunk_params.extend(filters.kinds)
            all_clauses = clauses + chunk_clauses
            if all_clauses:
                sql += " AND " + " AND ".join(all_clauses)
            sql += " ORDER BY rank ASC, c.id ASC LIMIT ?"
            fetch_limit = max(limit * 5, 50) + offset
            try:
                rows = self.db.query(sql, [spec.expression, *params, *chunk_params, fetch_limit])
            except sqlite3.OperationalError as exc:
                result.warning = f"全文查询语法错误，已返回空结果: {exc}"
                rows = []
            for row in rows:
                file_id = int(row["file_id"])
                if per_file.get(file_id, 0) >= PER_FILE_HITS:
                    continue
                key = (file_id, int(row["chunk_id"]))
                if key in seen_chunks:
                    continue
                hit = self._row_to_hit(row, spec, layer="fulltext")
                if hit is None:
                    continue
                hits.append(hit)
                seen_chunks.add(key)
                per_file[file_id] = per_file.get(file_id, 0) + 1

        hits = self._apply_staleness(hits, filters.include_stale)
        result.total = len(hits)
        if offset:
            hits = hits[offset:]
        result.hits = hits[:limit]
        result.semantic = self._semantic_meta()
        if not result.hits:
            result.empty_reason = "all_results_stale" if result.total == 0 and self._had_stale else "no_match"
        return result

    _had_stale = False

    def _row_to_hit(self, row: Any, spec: QuerySpec, *, layer: str) -> SearchHit | None:
        text = row["text"] or ""
        names = spec.terms
        snippet, _term, matched_terms = make_snippet(text, names)
        name_html, name_matched = _highlight_terms(row["name"] or "", names)
        matched_field = "text" if matched_terms else "other"
        if name_matched:
            matched_field = "name"
        elif any(term.casefold() in (row["path"] or "").casefold() for term in names):
            matched_field = "path"
        elif any(term.casefold() in (row["symbol"] or "").casefold() for term in names):
            matched_field = "symbol"
        rank = float(row["rank"]) if "rank" in row.keys() else 0.0
        return SearchHit(
            file_id=int(row["file_id"]),
            chunk_id=int(row["chunk_id"]) if row["chunk_id"] is not None else None,
            path=row["path"],
            name=row["name"],
            kind=row["chunk_kind"],
            layer=layer,
            text=text,
            snippet=snippet,
            highlighted_name=name_html or html.escape(row["name"] or ""),
            start_line=row["start_line"],
            end_line=row["end_line"],
            page=row["page"],
            score=round(-rank, 4),
            matched_terms=list(dict.fromkeys(matched_terms)),
            matched_field=matched_field,
            stale=False,
            stale_reason=None,
            parse_status=row["parse_status"],
            parse_error=row["parse_error"],
            indexed_at=row["indexed_at"],
            size=int(row["size"]),
            mtime=float(row["mtime"]),
            symbol=row["symbol"],
            truncated=bool(row["truncated"]),
        )

    def _exact_name_hits(self, query: str, clauses: list[str], params: list[Any]) -> list[SearchHit]:
        query_key = query.casefold()
        sql = (
            "SELECT f.*, c.id AS chunk_id, c.kind AS chunk_kind, c.start_line, c.end_line, c.page, c.text,"
            " c.symbol"
            " FROM files f LEFT JOIN chunks c ON c.id = ("
            "   SELECT id FROM chunks WHERE file_id=f.id AND kind='metadata' ORDER BY ordinal LIMIT 1)"
        )
        conditions = [
            "(f.name_key = ? OR lower(substr(f.name, 1, length(f.name) - length(f.ext))) = ?)"
        ]
        sql_params: list[Any] = [query_key, query_key]
        if clauses:
            conditions.extend(clauses)
            sql_params.extend(params)
        sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY f.path_key LIMIT 20"
        rows = self.db.query(sql, sql_params)
        hits: list[SearchHit] = []
        for row in rows:
            text = row["text"] or f"文件名: {row['name']}\n路径: {row['rel_path']}"
            terms = [query]
            snippet, _term, matched = make_snippet(text, terms)
            name_html, _nm = _highlight_terms(row["name"], terms)
            hits.append(
                SearchHit(
                    file_id=int(row["id"]),
                    chunk_id=int(row["chunk_id"]) if row["chunk_id"] is not None else None,
                    path=row["path"],
                    name=row["name"],
                    kind=row["chunk_kind"] or "metadata",
                    layer="exact_name",
                    text=text,
                    snippet=snippet,
                    highlighted_name=name_html or html.escape(row["name"]),
                    start_line=row["start_line"],
                    end_line=row["end_line"],
                    page=None,
                    score=1000.0,
                    matched_terms=matched or [query],
                    matched_field="name",
                    stale=False,
                    stale_reason=None,
                    parse_status=row["parse_status"],
                    parse_error=row["parse_error"],
                    indexed_at=row["indexed_at"],
                    size=int(row["size"]),
                    mtime=float(row["mtime"]),
                    symbol=row["symbol"],
                    truncated=bool(row["truncated"]),
                )
            )
        return hits

    def _apply_staleness(self, hits: list[SearchHit], include_stale: bool) -> list[SearchHit]:
        stat_cache: dict[str, os.stat_result | None] = {}

        def get_stat(path: str) -> os.stat_result | None:
            if path not in stat_cache:
                try:
                    stat_cache[path] = os.stat(path)
                except OSError:
                    stat_cache[path] = None
            return stat_cache[path]

        kept: list[SearchHit] = []
        any_stale = False
        for hit in hits:
            stat = get_stat(hit.path)
            reason = None
            if stat is None:
                reason = "文件已删除或不可访问，索引片段已失效"
            elif stat.st_size != hit.size:
                reason = "文件大小已变化，索引片段已失效"
            elif abs(stat.st_mtime - hit.mtime) > 1e-6:
                reason = "文件修改时间已变化，索引片段已失效"
            if reason:
                any_stale = True
                hit.stale = True
                hit.stale_reason = reason
                if not include_stale:
                    continue
            kept.append(hit)
        self._had_stale = any_stale
        return kept

    # ------------------------------------------------------------------
    # 语义
    # ------------------------------------------------------------------
    def _semantic_meta(self) -> dict[str, Any]:
        try:
            return self.semantic.stats()
        except Exception as exc:  # pragma: no cover
            return {"available": False, "error": str(exc), "label": self.semantic.label}

    def _semantic_search(self, query: str, filters: SearchFilters, limit: int, offset: int) -> SearchResult:
        result = SearchResult(query=query, mode="semantic-local-hash")
        meta = self._semantic_meta()
        result.semantic = meta
        if not query:
            result.empty_reason = "empty_query"
            return result
        if not meta.get("available"):
            fallback = self._fulltext_search(query, filters, limit, offset)
            fallback.mode = "semantic-fallback-fulltext"
            fallback.warning = (
                "本地哈希向量索引尚未构建或不可用，已自动回退到 FTS5 全文检索；"
                "语义结果只代表向量相似度，不代表事实。"
            )
            fallback.semantic = {**meta, "fallback": "fulltext", "fallback_reason": "vector_index_not_built"}
            return fallback
        try:
            file_ids = self._filtered_file_ids(filters)
            candidates = self.semantic.search(
                query, file_ids=file_ids, limit=max(limit * 3, 30), min_score=0.005
            )
        except Exception as exc:
            fallback = self._fulltext_search(query, filters, limit, offset)
            fallback.mode = "semantic-fallback-fulltext"
            fallback.warning = f"语义检索失败（{exc}），已回退到全文检索"
            fallback.semantic = {**meta, "fallback": "fulltext", "error": str(exc)}
            return fallback

        hits: list[SearchHit] = []
        terms = parse_query(query).terms
        for candidate in candidates:
            row = self.db.query_one(
                "SELECT c.id AS chunk_id, c.file_id, c.kind AS chunk_kind, c.start_line, c.end_line, c.page,"
                " c.text, c.symbol, f.path, f.name, f.ext, f.size, f.mtime, f.parse_status, f.parse_error,"
                " f.indexed_at, f.truncated"
                " FROM chunks c JOIN files f ON f.id=c.file_id WHERE c.id=?",
                (candidate["chunk_id"],),
            )
            if row is None:
                continue
            if filters.kinds and row["chunk_kind"] not in filters.kinds:
                continue
            snippet, _term, matched = make_snippet(row["text"] or "", terms)
            name_html, _nm = _highlight_terms(row["name"], terms)
            hits.append(
                SearchHit(
                    file_id=int(row["file_id"]),
                    chunk_id=int(row["chunk_id"]),
                    path=row["path"],
                    name=row["name"],
                    kind=row["chunk_kind"],
                    layer="semantic",
                    text=row["text"] or "",
                    snippet=snippet or html.escape((row["text"] or "")[:SNIPPET_WIDTH]),
                    highlighted_name=name_html or html.escape(row["name"]),
                    start_line=row["start_line"],
                    end_line=row["end_line"],
                    page=row["page"],
                    score=float(candidate["score"]),
                    matched_terms=matched,
                    matched_field="text" if matched else "similarity",
                    stale=False,
                    stale_reason=None,
                    parse_status=row["parse_status"],
                    parse_error=row["parse_error"],
                    indexed_at=row["indexed_at"],
                    size=int(row["size"]),
                    mtime=float(row["mtime"]),
                    symbol=row["symbol"],
                    truncated=bool(row["truncated"]),
                )
            )
        hits = self._apply_staleness(hits, filters.include_stale)
        result.total = len(hits)
        result.hits = hits[offset : offset + limit]
        if not result.hits:
            result.empty_reason = "all_results_stale" if self._had_stale else "no_match"
        result.semantic = {**meta, "notes": "相似度排序结果不代表事实；请通过证据定位核对原文"}
        return result

    def _filtered_file_ids(self, filters: SearchFilters) -> set[int] | None:
        clauses, params = self._where(filters)
        if not clauses:
            return None
        sql = "SELECT f.id FROM files f WHERE " + " AND ".join(clauses)
        return {int(row["id"]) for row in self.db.query(sql, params)}

    # ------------------------------------------------------------------
    # WHERE 构造
    # ------------------------------------------------------------------
    def _where(self, filters: SearchFilters) -> tuple[list[str], list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if filters.roots:
            placeholders = ",".join("?" for _ in filters.roots)
            clauses.append(
                f"f.root_id IN (SELECT id FROM roots WHERE path_key IN ({placeholders}))"
            )
            params.extend(path_key(root) for root in filters.roots)
        if filters.collections:
            if self.collections is None:
                clauses.append("0=1")
            else:
                names = list(filters.collections)
                placeholders = ",".join("?" for _ in names)
                clauses.append(
                    "(f.id IN (SELECT f2.id FROM files f2 JOIN collection_roots cr ON cr.root_id=f2.root_id"
                    " JOIN collections col ON col.id=cr.collection_id"
                    f" WHERE col.name IN ({placeholders}))"
                    " OR f.id IN (SELECT cf.file_id FROM collection_files cf"
                    " JOIN collections col2 ON col2.id=cf.collection_id"
                    f" WHERE col2.name IN ({placeholders})))"
                )
                params.extend(names)
                params.extend(names)
        if filters.exts:
            normalized = [ext.casefold() if ext.startswith(".") else "." + ext.casefold() for ext in filters.exts]
            placeholders = ",".join("?" for _ in normalized)
            clauses.append(f"f.ext IN ({placeholders})")
            params.extend(normalized)
        if filters.kinds:
            placeholders = ",".join("?" for _ in filters.kinds)
            clauses.append(f"f.id IN (SELECT file_id FROM chunks WHERE kind IN ({placeholders}))")
            params.extend(filters.kinds)
        if filters.symbols:
            conditions = []
            for symbol in filters.symbols:
                conditions.append(
                    "EXISTS (SELECT 1 FROM symbols s WHERE s.file_id=f.id AND s.name_key=?)"
                )
                params.append(symbol.casefold())
            clauses.append("(" + " OR ".join(conditions) + ")")
        if filters.name_contains:
            clauses.append("f.name_key LIKE ?")
            params.append(f"%{filters.name_contains.casefold()}%")
        if filters.path_contains:
            clauses.append("f.path_key LIKE ?")
            params.append(f"%{os.path.normcase(filters.path_contains)}%")
        if filters.mtime_from is not None:
            clauses.append("f.mtime >= ?")
            params.append(float(filters.mtime_from))
        if filters.mtime_to is not None:
            clauses.append("f.mtime <= ?")
            params.append(float(filters.mtime_to))
        if filters.size_min is not None:
            clauses.append("f.size >= ?")
            params.append(int(filters.size_min))
        if filters.size_max is not None:
            clauses.append("f.size <= ?")
            params.append(int(filters.size_max))
        if filters.parse_status:
            placeholders = ",".join("?" for _ in filters.parse_status)
            clauses.append(f"f.parse_status IN ({placeholders})")
            params.extend(filters.parse_status)
        return clauses, params

    @staticmethod
    def _file_row_to_dict(row: Any) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "path": row["path"],
            "rel_path": row["rel_path"],
            "name": row["name"],
            "ext": row["ext"],
            "size": int(row["size"]),
            "mtime": float(row["mtime"]),
            "content_hash": row["content_hash"],
            "hash_status": row["hash_status"],
            "parse_status": row["parse_status"],
            "parse_error": row["parse_error"],
            "parser": row["parser"],
            "truncated": bool(row["truncated"]),
            "kind": row["kind"],
            "indexed_at": row["indexed_at"],
        }

    # ------------------------------------------------------------------
    def evidence(self, hit: SearchHit) -> dict[str, Any]:
        return self.locator.locate_hit(hit, size=hit.size, mtime=hit.mtime).to_dict()
