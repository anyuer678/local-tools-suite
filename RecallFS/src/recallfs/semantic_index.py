"""可选的“语义”检索模块。

诚实声明
--------
本模块内置的是 **本地哈希向量（local hash vector）**：把词/CJK bigram 通过 blake2b
散列到固定维度并做符号累加，再 L2 归一化。它能做相似度排序，但**不是神经语义模型**，
不包含任何预训练权重，也不访问网络。

* 默认不上传任何内容、不自动下载模型；
* 模型/向量不可用时，搜索层自动回退到 FTS5 全文检索；
* 界面上必须展示模型名、版本、覆盖率以及“非语义模型”的标注。
"""

from __future__ import annotations

import array
import hashlib
import math
import re
import time
from typing import Any, Iterable, Sequence

from .fulltext_index import IndexDatabase

MODEL_NAME = "local-hash-bigram-v1"
MODEL_VERSION = "1"
MODEL_LABEL = "本地哈希向量（非语义模型）"

_WORD_RE = re.compile(r"[A-Za-z0-9_]{2,}")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]+")

_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "not", "you", "our",
    "的", "了", "和", "是", "在", "有", "与", "对", "中", "为", "等", "及", "或",
}


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _WORD_RE.finditer(text):
        word = match.group(0).casefold()
        if word not in _STOPWORDS:
            tokens.append(word)
    for match in _CJK_RUN_RE.finditer(text):
        run = match.group(0)
        if len(run) == 1:
            if run not in _STOPWORDS:
                tokens.append(run)
            continue
        for index in range(len(run) - 1):
            bigram = run[index : index + 2]
            if bigram not in _STOPWORDS:
                tokens.append(bigram)
    return tokens


class SemanticIndex:
    is_neural_model = False
    label = MODEL_LABEL

    def __init__(
        self,
        db: IndexDatabase,
        *,
        dim: int = 256,
        max_chars: int = 4000,
        max_chunks: int = 20000,
        max_scan: int = 50000,
    ) -> None:
        self.db = db
        self.dim = dim
        self.max_chars = max_chars
        self.max_chunks = max_chunks
        self.max_scan = max_scan

    # ------------------------------------------------------------------
    def _token_index(self, token: str) -> tuple[int, float]:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "little") % self.dim
        sign = 1.0 if digest[4] & 1 else -1.0
        return index, sign

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dim
        tokens = tokenize(text[: self.max_chars])
        if not tokens:
            return vector
        for token in tokens:
            index, sign = self._token_index(token)
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm > 0:
            vector = [value / norm for value in vector]
        return vector

    @staticmethod
    def _pack(vector: Sequence[float]) -> bytes:
        return array.array("f", vector).tobytes()

    @staticmethod
    def _unpack(blob: bytes) -> array.array:
        values = array.array("f")
        values.frombytes(blob)
        return values

    # ------------------------------------------------------------------
    def build(
        self,
        file_ids: Iterable[int] | None = None,
        *,
        limit: int | None = None,
        batch_size: int = 500,
    ) -> dict[str, Any]:
        started = time.time()
        ids = list(file_ids) if file_ids is not None else None
        sql = "SELECT id, file_id, text FROM chunks WHERE kind='raw_text'"
        params: list[Any] = []
        if ids:
            placeholders = ",".join("?" for _ in ids)
            sql += f" AND file_id IN ({placeholders})"
            params.extend(ids)
        sql += " ORDER BY id"
        rows = self.db.query(sql, params)
        cap = limit if limit is not None else self.max_chunks
        awaiting = []
        for row in rows:
            text = row["text"] or ""
            if len(text.strip()) < 8:
                continue
            awaiting.append((int(row["id"]), text[: self.max_chars]))
            if len(awaiting) >= cap:
                break
        with self.db.transaction() as conn:
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"DELETE FROM semantic_vectors WHERE model=? AND chunk_id IN "
                    f"(SELECT id FROM chunks WHERE file_id IN ({placeholders}))",
                    (MODEL_NAME, *ids),
                )
            else:
                conn.execute("DELETE FROM semantic_vectors WHERE model=?", (MODEL_NAME,))
            now = time.time()
            for start in range(0, len(awaiting), batch_size):
                batch = awaiting[start : start + batch_size]
                payload = []
                for chunk_id, text in batch:
                    vector = self.embed(text)
                    payload.append(
                        (
                            chunk_id,
                            MODEL_NAME,
                            MODEL_VERSION,
                            self.dim,
                            self._pack(vector),
                            1.0,
                            now,
                        )
                    )
                conn.executemany(
                    "INSERT OR REPLACE INTO semantic_vectors"
                    "(chunk_id, model, version, dim, vector, norm, created_at) VALUES (?,?,?,?,?,?,?)",
                    payload,
                )
        self.db.set_meta("semantic_model", MODEL_NAME)
        self.db.set_meta("semantic_model_version", MODEL_VERSION)
        self.db.set_meta("semantic_label", MODEL_LABEL)
        self.db.set_meta("semantic_dim", str(self.dim))
        self.db.set_meta("semantic_built_at", str(time.time()))
        coverage = self.coverage()
        return {
            "model": MODEL_NAME,
            "version": MODEL_VERSION,
            "label": MODEL_LABEL,
            "is_neural_model": False,
            "dim": self.dim,
            "indexed_chunks": len(awaiting),
            "candidate_chunks": len(rows),
            "coverage": coverage["coverage"],
            "truncated_by_limit": len(rows) > len(awaiting),
            "duration_seconds": round(time.time() - started, 3),
        }

    def clear(self) -> int:
        removed = int(self.db.scalar("SELECT COUNT(*) FROM semantic_vectors WHERE model=?", (MODEL_NAME,)) or 0)
        self.db.execute("DELETE FROM semantic_vectors WHERE model=?", (MODEL_NAME,))
        self.db.set_meta("semantic_built_at", "")
        return removed

    def coverage(self) -> dict[str, Any]:
        total = int(self.db.scalar("SELECT COUNT(*) FROM chunks WHERE kind='raw_text'") or 0)
        indexed = int(self.db.scalar("SELECT COUNT(*) FROM semantic_vectors WHERE model=?", (MODEL_NAME,)) or 0)
        built_at = self.db.get_meta("semantic_built_at") or ""
        return {
            "model": MODEL_NAME,
            "version": MODEL_VERSION,
            "label": MODEL_LABEL,
            "is_neural_model": False,
            "dim": self.dim,
            "total_chunks": total,
            "indexed_chunks": indexed,
            "coverage": round(indexed / total, 4) if total else 0.0,
            "built_at": built_at or None,
            "available": indexed > 0,
        }

    def stats(self) -> dict[str, Any]:
        payload = self.coverage()
        payload.update(
            {
                "max_chars": self.max_chars,
                "max_chunks": self.max_chunks,
                "network_required": False,
                "auto_download": False,
            }
        )
        return payload

    def search(
        self,
        query_text: str,
        *,
        file_ids: set[int] | None = None,
        limit: int = 20,
        min_score: float = 0.01,
    ) -> list[dict[str, Any]]:
        """余弦相似度检索。返回 chunk 级候选（不代表事实，只代表相似度）。"""
        query = self.embed(query_text)
        bank = math.sqrt(sum(value * value for value in query))
        if bank == 0:
            return []
        sql = (
            "SELECT v.chunk_id, v.vector, c.file_id, c.text, c.start_line, c.end_line, c.page "
            "FROM semantic_vectors v JOIN chunks c ON c.id=v.chunk_id WHERE v.model=? LIMIT ?"
        )
        rows = self.db.query(sql, (MODEL_NAME, self.max_scan))
        results: list[dict[str, Any]] = []
        for row in rows:
            if file_ids is not None and int(row["file_id"]) not in file_ids:
                continue
            stored = self._unpack(row["vector"])
            score = sum(a * b for a, b in zip(query, stored))
            if score < min_score:
                continue
            results.append(
                {
                    "chunk_id": int(row["chunk_id"]),
                    "file_id": int(row["file_id"]),
                    "text": row["text"],
                    "start_line": row["start_line"],
                    "end_line": row["end_line"],
                    "page": row["page"],
                    "score": round(score, 4),
                }
            )
        results.sort(key=lambda item: item["score"], reverse=True)
        return results[:limit]
