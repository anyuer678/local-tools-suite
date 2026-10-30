"""命令注册表：SQLite 持久化 + 内存缓存 + 模糊搜索。

设计要点：
* 命令元数据与处理器分离，处理器只存在于当前进程内；
* 启动时一次性把命令载入内存，搜索不访问数据库，避免阻塞 UI；
* 使用次数与最近使用时间写入 SQLite，用于频率/最近排序。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

CommandHandler = Callable[["CommandContext"], Any]


class CommandRegistryError(RuntimeError):
    """命令注册表相关错误。"""


class CommandNotFound(CommandRegistryError):
    pass


class CommandDisabled(CommandRegistryError):
    pass


class CommandExecutionError(CommandRegistryError):
    pass


@dataclass
class Command:
    """一条可持久化的命令元数据。"""

    id: str
    name: str
    aliases: list[str] = field(default_factory=list)
    category: str = "通用"
    keywords: list[str] = field(default_factory=list)
    description: str = ""
    source: str = "builtin"
    enabled: bool = True
    use_count: int = 0
    last_used: float = 0.0

    def __post_init__(self) -> None:
        if not str(self.id).strip():
            raise ValueError("命令 id 不能为空")
        if not str(self.name).strip():
            raise ValueError("命令名称不能为空")
        self.id = str(self.id).strip()
        self.name = str(self.name).strip()
        self.aliases = [str(a).strip() for a in self.aliases if str(a).strip()]
        self.keywords = [str(k).strip() for k in self.keywords if str(k).strip()]
        self.category = str(self.category or "通用").strip()
        self.source = str(self.source or "builtin").strip()

    def clone(self) -> "Command":
        return replace(
            self,
            aliases=list(self.aliases),
            keywords=list(self.keywords),
        )


@dataclass
class CommandContext:
    """命令执行上下文。插件使用 plugin_api.CommandContext，但字段保持一致。"""

    command_id: str
    text: str = ""
    params: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)


@dataclass
class SearchHit:
    command: Command
    score: float


def _normalize(value: str) -> str:
    return str(value).casefold().strip()


def _as_sequence(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, Iterable):
        return [str(item) for item in value]
    return [str(value)]


def fuzzy_score(text: str, query: str) -> int:
    """子序列模糊评分；完全不匹配返回 -1。

    连续命中、从开头命中、跨度越小得分越高。
    """

    text_l = _normalize(text)
    query_l = _normalize(query)
    if not query_l:
        return 0
    if not text_l:
        return -1
    pos = 0
    matched: list[int] = []
    for ch in query_l:
        found = text_l.find(ch, pos)
        if found < 0:
            return -1
        matched.append(found)
        pos = found + 1
    span = matched[-1] - matched[0] + 1
    consecutive = sum(
        1 for left, right in zip(matched, matched[1:]) if right == left + 1
    )
    score = 220 - (span - len(query_l)) * 8 - matched[0] * 3 + consecutive * 6
    return max(1, score)


def _recency_boost(last_used: float, now: float | None = None) -> float:
    if not last_used:
        return 0.0
    age = max(0.0, (now if now is not None else time.time()) - last_used)
    if age <= 3600:
        return 35.0
    if age <= 86400:
        return 25.0
    if age <= 7 * 86400:
        return 12.0
    return 3.0


class CommandRegistry:
    """命令注册与搜索。

    ``db_path`` 可以是文件路径，也可以传 ``:memory:``。
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        if self.db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._closed = False
        self._cache: dict[str, Command] = {}
        self._handlers: dict[str, CommandHandler] = {}
        self._create_schema()
        self._load_cache()

    # ------------------------------------------------------------------ schema
    def _create_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS commands (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    aliases TEXT NOT NULL DEFAULT '[]',
                    category TEXT NOT NULL DEFAULT '通用',
                    keywords TEXT NOT NULL DEFAULT '[]',
                    description TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'builtin',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    use_count INTEGER NOT NULL DEFAULT 0,
                    last_used REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_commands_category
                    ON commands(category);
                CREATE INDEX IF NOT EXISTS idx_commands_source
                    ON commands(source);
                """
            )
            self._conn.commit()

    def _load_cache(self) -> None:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM commands").fetchall()
            self._cache = {row["id"]: self._row_to_command(row) for row in rows}

    @staticmethod
    def _row_to_command(row: sqlite3.Row) -> Command:
        try:
            aliases = json.loads(row["aliases"] or "[]")
        except (TypeError, json.JSONDecodeError):
            aliases = []
        try:
            keywords = json.loads(row["keywords"] or "[]")
        except (TypeError, json.JSONDecodeError):
            keywords = []
        return Command(
            id=row["id"],
            name=row["name"],
            aliases=list(aliases) if isinstance(aliases, list) else [],
            category=row["category"],
            keywords=list(keywords) if isinstance(keywords, list) else [],
            description=row["description"],
            source=row["source"],
            enabled=bool(row["enabled"]),
            use_count=int(row["use_count"]),
            last_used=float(row["last_used"]),
        )

    def _ensure_open(self) -> None:
        if self._closed:
            raise CommandRegistryError("命令注册表已关闭")

    # ---------------------------------------------------------------- registering
    def register(self, command: Command, handler: CommandHandler | None = None) -> Command:
        """注册或更新命令；更新时保留历史使用统计。"""

        with self._lock:
            self._ensure_open()
            old = self._cache.get(command.id)
            if old is not None:
                command.use_count = old.use_count
                command.last_used = old.last_used
                handler = handler if handler is not None else self._handlers.get(command.id)
            command = command.clone()
            self._conn.execute(
                """
                INSERT INTO commands
                    (id, name, aliases, category, keywords, description, source,
                     enabled, use_count, last_used)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    aliases=excluded.aliases,
                    category=excluded.category,
                    keywords=excluded.keywords,
                    description=excluded.description,
                    source=excluded.source,
                    enabled=excluded.enabled,
                    use_count=excluded.use_count,
                    last_used=excluded.last_used
                """,
                (
                    command.id,
                    command.name,
                    json.dumps(command.aliases, ensure_ascii=False),
                    command.category,
                    json.dumps(command.keywords, ensure_ascii=False),
                    command.description,
                    command.source,
                    1 if command.enabled else 0,
                    command.use_count,
                    command.last_used,
                ),
            )
            self._conn.commit()
            self._cache[command.id] = command
            if handler is not None:
                self._handlers[command.id] = handler
            return command.clone()

    def register_command(
        self,
        command_id: str,
        name: str,
        *,
        handler: CommandHandler | None = None,
        aliases: Sequence[str] | None = None,
        category: str = "通用",
        keywords: Sequence[str] | None = None,
        description: str = "",
        source: str = "builtin",
        enabled: bool = True,
    ) -> Command:
        """``register`` 的便捷写法。"""

        cmd = Command(
            id=command_id,
            name=name,
            aliases=list(aliases or []),
            category=category,
            keywords=list(keywords or []),
            description=description,
            source=source,
            enabled=enabled,
        )
        return self.register(cmd, handler)

    def unregister(self, command_id: str) -> bool:
        with self._lock:
            self._ensure_open()
            existed = command_id in self._cache
            if not existed:
                return False
            self._conn.execute("DELETE FROM commands WHERE id = ?", (command_id,))
            self._conn.commit()
            self._cache.pop(command_id, None)
            self._handlers.pop(command_id, None)
            return True

    def unregister_source(self, source: str) -> int:
        with self._lock:
            self._ensure_open()
            ids = [cid for cid, cmd in self._cache.items() if cmd.source == source]
            for cid in ids:
                self._conn.execute("DELETE FROM commands WHERE id = ?", (cid,))
                self._cache.pop(cid, None)
                self._handlers.pop(cid, None)
            if ids:
                self._conn.commit()
            return len(ids)

    def set_enabled(self, command_id: str, enabled: bool) -> Command:
        with self._lock:
            self._ensure_open()
            cmd = self._cache.get(command_id)
            if cmd is None:
                raise CommandNotFound(f"命令不存在：{command_id}")
            cmd.enabled = bool(enabled)
            self._conn.execute(
                "UPDATE commands SET enabled = ? WHERE id = ?",
                (1 if enabled else 0, command_id),
            )
            self._conn.commit()
            return cmd.clone()

    def has_handler(self, command_id: str) -> bool:
        with self._lock:
            return command_id in self._handlers

    # -------------------------------------------------------------------- query
    def get(self, command_id: str) -> Command | None:
        with self._lock:
            cmd = self._cache.get(command_id)
            return cmd.clone() if cmd else None

    def get_handler(self, command_id: str) -> CommandHandler | None:
        with self._lock:
            return self._handlers.get(command_id)

    def all(self, category: str | None = None, include_disabled: bool = True) -> list[Command]:
        with self._lock:
            commands = list(self._cache.values())
        if category:
            category_l = _normalize(category)
            commands = [c for c in commands if _normalize(c.category) == category_l]
        if not include_disabled:
            commands = [c for c in commands if c.enabled]
        commands.sort(key=lambda c: (-c.use_count, -c.last_used, c.name.casefold()))
        return [c.clone() for c in commands]

    def categories(self) -> list[str]:
        return sorted({c.category for c in self.all()}, key=str.casefold)

    def count(self) -> int:
        with self._lock:
            return len(self._cache)

    def execute(
        self,
        command_id: str,
        text: str = "",
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        with self._lock:
            cmd = self._cache.get(command_id)
            if cmd is None:
                raise CommandNotFound(f"命令不存在：{command_id}")
            if not cmd.enabled:
                raise CommandDisabled(f"命令已禁用：{command_id}")
            handler = self._handlers.get(command_id)
            if handler is None:
                raise CommandExecutionError(f"命令没有可执行处理器：{command_id}")
        ctx = CommandContext(command_id=command_id, text=text or "", params=dict(params or {}))
        self.record_use(command_id)
        return handler(ctx)

    def record_use(self, command_id: str) -> None:
        now = time.time()
        with self._lock:
            self._ensure_open()
            cmd = self._cache.get(command_id)
            if cmd is None:
                raise CommandNotFound(f"命令不存在：{command_id}")
            cmd.use_count += 1
            cmd.last_used = now
            self._conn.execute(
                "UPDATE commands SET use_count = ?, last_used = ? WHERE id = ?",
                (cmd.use_count, now, command_id),
            )
            self._conn.commit()

    # ------------------------------------------------------------------- search
    def search(
        self,
        query: str = "",
        *,
        category: str | None = None,
        limit: int | None = 50,
        include_disabled: bool = False,
    ) -> list[Command]:
        """模糊搜索命令。

        匹配顺序：精确 id/名称/别名 → 前缀 → 子串 → 子序列模糊 → 关键词。
        最终使用频率和最近使用参与排序。
        """

        if limit is not None and limit < 0:
            raise ValueError("limit 不能为负数")
        with self._lock:
            commands = [c.clone() for c in self._cache.values()]
        if not include_disabled:
            commands = [c for c in commands if c.enabled]
        if category:
            category_l = _normalize(category)
            commands = [c for c in commands if _normalize(c.category) == category_l]

        query = (query or "").strip()
        now = time.time()
        hits: list[SearchHit] = []
        if not query:
            for cmd in commands:
                hits.append(
                    SearchHit(
                        command=cmd,
                        score=cmd.use_count * 10 + _recency_boost(cmd.last_used, now),
                    )
                )
        else:
            for cmd in commands:
                score = self._score(cmd, query, now)
                if score > 0:
                    hits.append(SearchHit(command=cmd, score=score))
        hits.sort(
            key=lambda hit: (
                -hit.score,
                -hit.command.use_count,
                -hit.command.last_used,
                hit.command.name.casefold(),
            )
        )
        results = [hit.command for hit in hits]
        return results[:limit] if limit is not None else results

    def search_scored(
        self,
        query: str = "",
        *,
        category: str | None = None,
        limit: int | None = 50,
        include_disabled: bool = False,
    ) -> list[SearchHit]:
        """与 ``search`` 相同，但返回带评分的命中。"""

        results = self.search(
            query,
            category=category,
            limit=None,
            include_disabled=include_disabled,
        )
        now = time.time()
        if not query.strip():
            return [
                SearchHit(c, c.use_count * 10 + _recency_boost(c.last_used, now))
                for c in (results[:limit] if limit is not None else results)
            ]
        scored = [
            SearchHit(c, max(1, self._score(c, query, now)))
            for c in results
        ]
        return scored[:limit] if limit is not None else scored

    @staticmethod
    def _score(cmd: Command, query: str, now: float) -> float:
        q = _normalize(query)
        name = _normalize(cmd.name)
        cid = _normalize(cmd.id)
        aliases = [_normalize(a) for a in cmd.aliases]
        keywords = [_normalize(k) for k in cmd.keywords]
        description = _normalize(cmd.description)

        base = 0.0
        if q in {cid, name, *aliases}:
            base = 1000.0
        elif name.startswith(q) or cid.startswith(q) or any(a.startswith(q) for a in aliases):
            base = 600.0
        elif q in name or q in cid or any(q in a for a in aliases):
            base = 450.0
        else:
            fuzzy = max(
                [fuzzy_score(name, q), fuzzy_score(cid, q)]
                + [fuzzy_score(a, q) for a in aliases]
            )
            if fuzzy > 0:
                base = float(fuzzy)

        if base <= 0:
            if any(q == k for k in keywords):
                base = 360.0
            elif any(k.startswith(q) for k in keywords):
                base = 300.0
            elif any(q in k for k in keywords):
                base = 260.0
            elif q in description:
                base = 80.0
        if base <= 0:
            return 0.0
        return base + cmd.use_count * 10 + _recency_boost(cmd.last_used, now)

    # ------------------------------------------------------------------- cleanup
    def backup_to(self, destination: str | Path) -> Path:
        """使用 SQLite 在线备份 API 备份命令库。"""

        destination = Path(destination).expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._ensure_open()
            dest_conn = sqlite3.connect(str(destination))
            try:
                with dest_conn:
                    self._conn.backup(dest_conn)
            finally:
                dest_conn.close()
        return destination

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def __enter__(self) -> "CommandRegistry":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __len__(self) -> int:
        return self.count()
