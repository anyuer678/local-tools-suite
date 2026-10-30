"""集合（项目/课程/自定义）、收藏、最近搜索，以及可解释的相关文件关联。

所有配置持久化在同一个 SQLite 库中；删除集合/收藏不会删除源文件。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Iterable

from .document_parser import extract_referenced_paths
from .filesystem_watcher import normalize_path, path_key
from .fulltext_index import IndexDatabase, expand_for_fts

COLLECTION_KINDS = ("project", "course", "custom")

_WORD_RE = re.compile(r"[A-Za-z0-9_]{2,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]{2,}")

_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "have", "will", "not",
    "def", "class", "import", "return", "self", "true", "false", "none",
    "的", "了", "和", "是", "在", "有", "与", "对", "为", "等", "一个", "我们",
}


class CollectionStore:
    def __init__(self, db: IndexDatabase) -> None:
        self.db = db

    def create(
        self,
        name: str,
        kind: str = "custom",
        description: str = "",
        roots: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        if not name.strip():
            raise ValueError("集合名称不能为空")
        if kind not in COLLECTION_KINDS:
            raise ValueError(f"集合类型必须是 {COLLECTION_KINDS} 之一")
        now = time.time()
        existing = self.get(name)
        if existing:
            return self.describe(name)
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO collections(name, kind, description, created_at) VALUES (?,?,?,?)",
                (name.strip(), kind, description, now),
            )
        for root in roots or ():
            self.add_root(name, root)
        return self.describe(name)

    def get(self, name: str) -> Any:
        return self.db.query_one("SELECT * FROM collections WHERE name=?", (name.strip(),))

    def list(self) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM collections ORDER BY created_at")
        return [self.describe(row["name"]) for row in rows]

    def describe(self, name: str) -> dict[str, Any]:
        row = self.get(name)
        if row is None:
            raise KeyError(f"集合不存在: {name}")
        roots = [
            r["path"]
            for r in self.db.query(
                "SELECT roots.path FROM collection_roots JOIN roots ON roots.id=collection_roots.root_id "
                "WHERE collection_roots.collection_id=? ORDER BY roots.path_key",
                (int(row["id"]),),
            )
        ]
        return {
            "id": int(row["id"]),
            "name": row["name"],
            "kind": row["kind"],
            "description": row["description"] or "",
            "roots": roots,
            "file_count": len(self.files(name)),
            "created_at": row["created_at"],
        }

    def delete(self, name: str) -> bool:
        row = self.get(name)
        if row is None:
            return False
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM collections WHERE id=?", (int(row["id"]),))
        return True

    def add_root(self, name: str, root_path: str) -> None:
        row = self.get(name)
        if row is None:
            raise KeyError(f"集合不存在: {name}")
        root = self.db.get_root(root_path) or self.db.get_root(normalize_path(root_path))
        if root is None:
            root_id = self.db.add_root(normalize_path(root_path))
        else:
            root_id = int(root["id"])
        self.db.execute(
            "INSERT OR IGNORE INTO collection_roots(collection_id, root_id) VALUES (?,?)",
            (int(row["id"]), root_id),
        )

    def remove_root(self, name: str, root_path: str) -> None:
        row = self.get(name)
        root = self.db.get_root(root_path)
        if row is None or root is None:
            return
        self.db.execute(
            "DELETE FROM collection_roots WHERE collection_id=? AND root_id=?",
            (int(row["id"]), int(root["id"])),
        )

    def add_file(self, name: str, file_path: str) -> bool:
        row = self.get(name)
        file_row = self.db.get_file_by_path(file_path)
        if row is None or file_row is None:
            return False
        self.db.execute(
            "INSERT OR IGNORE INTO collection_files(collection_id, file_id) VALUES (?,?)",
            (int(row["id"]), int(file_row["id"])),
        )
        return True

    def remove_file(self, name: str, file_path: str) -> None:
        row = self.get(name)
        file_row = self.db.get_file_by_path(file_path)
        if row is None or file_row is None:
            return
        self.db.execute(
            "DELETE FROM collection_files WHERE collection_id=? AND file_id=?",
            (int(row["id"]), int(file_row["id"])),
        )

    def files(self, name: str) -> set[int]:
        row = self.get(name)
        if row is None:
            return set()
        collection_id = int(row["id"])
        ids: set[int] = set()
        for record in self.db.query(
            "SELECT f.id FROM files f JOIN collection_roots cr ON cr.root_id=f.root_id WHERE cr.collection_id=?",
            (collection_id,),
        ):
            ids.add(int(record["id"]))
        for record in self.db.query(
            "SELECT file_id FROM collection_files WHERE collection_id=?", (collection_id,)
        ):
            ids.add(int(record["file_id"]))
        return ids

    def resolve_names(self, names: Iterable[str]) -> set[int]:
        result: set[int] = set()
        for name in names:
            if not self.get(name):
                raise KeyError(f"集合不存在: {name}")
            result |= self.files(name)
        return result


class FavoritesStore:
    def __init__(self, db: IndexDatabase) -> None:
        self.db = db

    def add_file(self, path: str, note: str = "", label: str | None = None) -> int | None:
        row = self.db.get_file_by_path(path)
        if row is None:
            return None
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO favorites(fav_type, file_id, query_text, label, note, created_at)"
                " VALUES ('file', ?, NULL, ?, ?, ?)",
                (int(row["id"]), label or row["name"], note, time.time()),
            )
            return int(cursor.lastrowid)

    def add_query(self, query: str, note: str = "", label: str | None = None) -> int:
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO favorites(fav_type, file_id, query_text, label, note, created_at)"
                " VALUES ('query', NULL, ?, ?, ?, ?)",
                (query, label or query, note, time.time()),
            )
            return int(cursor.lastrowid)

    def list(self) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT favorites.*, files.path AS file_path, files.name AS file_name, files.parse_status "
            "FROM favorites LEFT JOIN files ON files.id=favorites.file_id "
            "ORDER BY favorites.created_at DESC, favorites.id DESC"
        )
        return [
            {
                "id": int(row["id"]),
                "type": row["fav_type"],
                "file_id": row["file_id"],
                "path": row["file_path"],
                "name": row["file_name"],
                "query": row["query_text"],
                "label": row["label"],
                "note": row["note"] or "",
                "created_at": row["created_at"],
                "parse_status": row["parse_status"],
            }
            for row in rows
        ]

    def remove(self, favorite_id: int) -> bool:
        existing = self.db.query_one("SELECT id FROM favorites WHERE id=?", (favorite_id,))
        if not existing:
            return False
        self.db.execute("DELETE FROM favorites WHERE id=?", (favorite_id,))
        return True

    def clear(self) -> None:
        self.db.execute("DELETE FROM favorites")


class RecentSearchStore:
    def __init__(self, db: IndexDatabase, keep: int = 50) -> None:
        self.db = db
        self.keep = keep

    def record(self, query: str, filters: dict[str, Any] | None = None, result_count: int = 0) -> int:
        payload = json.dumps(filters or {}, ensure_ascii=False, sort_keys=True)
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM recent_searches WHERE query=? AND filters_json=?", (query, payload)
            )
            cursor = conn.execute(
                "INSERT INTO recent_searches(query, filters_json, result_count, created_at) VALUES (?,?,?,?)",
                (query, payload, int(result_count), time.time()),
            )
            conn.execute(
                "DELETE FROM recent_searches WHERE id NOT IN "
                "(SELECT id FROM recent_searches ORDER BY created_at DESC, id DESC LIMIT ?)",
                (self.keep,),
            )
            return int(cursor.lastrowid)

    def list(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.query(
            "SELECT * FROM recent_searches ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
        )
        return [
            {
                "id": int(row["id"]),
                "query": row["query"],
                "filters": json.loads(row["filters_json"] or "{}"),
                "result_count": int(row["result_count"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def clear(self) -> None:
        self.db.execute("DELETE FROM recent_searches")


class RelatedFiles:
    """相关文件关联，每个结果都附带可解释依据（同哈希/引用/共同关键词）。"""

    def __init__(self, db: IndexDatabase) -> None:
        self.db = db

    def _target_keywords(self, file_id: int, limit: int = 8) -> list[str]:
        rows = self.db.query(
            "SELECT text FROM chunks WHERE file_id=? AND kind='raw_text' LIMIT 20", (file_id,)
        )
        counts: dict[str, int] = {}
        for row in rows:
            text = row["text"] or ""
            for match in _WORD_RE.finditer(text):
                word = match.group(0).casefold()
                if len(word) >= 3 and word not in _STOPWORDS:
                    counts[word] = counts.get(word, 0) + 1
            for match in _CJK_RE.finditer(text):
                run = match.group(0)
                for index in range(len(run) - 1):
                    bigram = run[index : index + 2]
                    if bigram not in _STOPWORDS:
                        counts[bigram] = counts.get(bigram, 0) + 1
        ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        return [word for word, count in ordered if count >= 1][:limit]

    def related(self, file_id_or_path: int | str, limit: int = 10) -> list[dict[str, Any]]:
        if isinstance(file_id_or_path, int):
            target = self.db.get_file(file_id_or_path)
        else:
            target = self.db.get_file_by_path(file_id_or_path)
        if target is None:
            return []
        target_id = int(target["id"])
        reasons: dict[int, list[str]] = {}
        scores: dict[int, float] = {}

        def add(candidate_id: int, reason: str, weight: float) -> None:
            if candidate_id == target_id:
                return
            bucket = reasons.setdefault(candidate_id, [])
            if reason not in bucket:
                bucket.append(reason)
                scores[candidate_id] = scores.get(candidate_id, 0.0) + weight

        if target["content_hash"]:
            for row in self.db.query(
                "SELECT id FROM files WHERE content_hash=? AND id<>? AND hash_status=?",
                (target["content_hash"], target_id, target["hash_status"] or "full"),
            ):
                add(int(row["id"]), "内容哈希相同（可能是副本或移动）", 5.0)

        if target["name"]:
            matches = self.db.query(
                "SELECT DISTINCT c.file_id FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid "
                "WHERE chunks_fts MATCH ? LIMIT 100",
                (self._fts_phrase(target["name"]),),
            )
            for row in matches:
                add(int(row["file_id"]), f"其他文件正文引用了 {target['name']}", 3.0)

        target_chunks = self.db.query(
            "SELECT text FROM chunks WHERE file_id=? AND kind='raw_text' LIMIT 50", (target_id,)
        )
        references: list[str] = []
        for row in target_chunks:
            references.extend(extract_referenced_paths(row["text"] or ""))
        for reference in list(dict.fromkeys(references))[:20]:
            base = Path(reference.replace("\\", "/")).name
            if base.casefold() == str(target["name"]).casefold():
                continue
            for row in self.db.query(
                "SELECT id, name FROM files WHERE name_key=? LIMIT 5", (base.casefold(),)
            ):
                add(int(row["id"]), f"目标文件引用了 {base}", 2.0)

        keywords = self._target_keywords(target_id)
        if keywords:
            overlap: dict[int, list[str]] = {}
            for keyword in keywords:
                try:
                    rows = self.db.query(
                        "SELECT DISTINCT c.file_id FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid "
                        "WHERE chunks_fts MATCH ? LIMIT 200",
                        (self._fts_phrase(keyword),),
                    )
                except Exception:
                    continue
                for row in rows:
                    candidate_id = int(row["file_id"])
                    if candidate_id == target_id:
                        continue
                    overlap.setdefault(candidate_id, []).append(keyword)
            for candidate_id, words in overlap.items():
                if len(words) >= 2:
                    add(candidate_id, "共同关键词: " + ", ".join(dict.fromkeys(words)), 1.0 * len(words))

        results: list[dict[str, Any]] = []
        for candidate_id, why in reasons.items():
            row = self.db.get_file(candidate_id)
            if row is None:
                continue
            results.append(
                {
                    "file_id": candidate_id,
                    "path": row["path"],
                    "name": row["name"],
                    "parse_status": row["parse_status"],
                    "reasons": why,
                    "score": round(scores.get(candidate_id, 0.0), 2),
                }
            )
        results.sort(key=lambda item: (-item["score"], item["path"].casefold()))
        return results[:limit]

    @staticmethod
    def _fts_phrase(text: str) -> str:
        expanded = expand_for_fts(text).replace('"', " ")
        return f'"{expanded}"' if expanded else '""'
