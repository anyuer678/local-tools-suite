"""SQLite 持久化：运行历史、日志、模板、搜索与导出。

所有写操作使用事务与进程内锁；日志以定长块流式写入，不把完整日志常驻内存。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
import zipfile
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .command_builder import RunConfig
from .redaction import REDACTED, redact_env, redacted_key_names, scan_sensitive_content

VALID_STATUSES = (
    "running",
    "success",
    "failed",
    "timeout",
    "cancelled",
    "interrupted",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    command TEXT NOT NULL,
    args_json TEXT NOT NULL DEFAULT '[]',
    cwd TEXT NOT NULL,
    env_json TEXT NOT NULL DEFAULT '{}',
    env_overrides_json TEXT NOT NULL DEFAULT '{}',
    shell INTEGER NOT NULL DEFAULT 0,
    timeout REAL,
    status TEXT NOT NULL,
    exit_code INTEGER,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_ms INTEGER,
    project_path TEXT,
    git_root TEXT,
    git_branch TEXT,
    git_commit TEXT,
    git_dirty INTEGER,
    tool_version TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_path);
CREATE INDEX IF NOT EXISTS idx_runs_cwd ON runs(cwd);

CREATE TABLE IF NOT EXISTS run_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    stream TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_run_id ON run_logs(run_id, id);
CREATE INDEX IF NOT EXISTS idx_logs_stream ON run_logs(run_id, stream, id);

CREATE TABLE IF NOT EXISTS task_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    cwd TEXT NOT NULL,
    command TEXT NOT NULL,
    args_json TEXT NOT NULL DEFAULT '[]',
    env_overrides_json TEXT NOT NULL DEFAULT '{}',
    timeout REAL,
    shell INTEGER NOT NULL DEFAULT 0,
    project_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_templates_name ON task_templates(name);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def default_data_dir() -> Path:
    override = os.environ.get("RUNTRACE_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "RunTrace"
    return Path.home() / ".runtrace"


def default_db_path() -> Path:
    return default_data_dir() / "runtrace.db"


def _json_load(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return default


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _time_bound(value: str | date | datetime | None, *, end: bool = False) -> str | None:
    """把筛选边界转为可直接比较的 UTC ISO 字符串。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
        if end:
            parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    else:
        text = str(value).strip()
        try:
            if len(text) == 10:
                day = date.fromisoformat(text)
                parsed = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
                if end:
                    parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
            elif text.endswith("Z"):
                parsed = datetime.fromisoformat(text[:-1] + "+00:00")
            else:
                parsed = datetime.fromisoformat(text)
        except ValueError:
            return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


class RunStore:
    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, timeout=30.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            if self.db_path != ":memory:":
                try:
                    self._conn.execute("PRAGMA journal_mode=WAL")
                except sqlite3.DatabaseError:
                    pass
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # ---------- 基础生命周期 ----------
    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def __enter__(self) -> "RunStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ---------- 运行记录 ----------
    def create_run(
        self,
        *,
        name: str,
        command: str,
        args: Iterable[str] = (),
        cwd: str,
        env: Mapping[str, object] | None = None,
        env_overrides: Mapping[str, object] | None = None,
        shell: bool = False,
        timeout: float | None = None,
        status: str = "running",
        exit_code: int | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
        duration_ms: int | None = None,
        project_path: str | None = None,
        git_context: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        run_id: str | None = None,
    ) -> str:
        """插入运行记录；env 在这里强制脱敏，避免调用者误存明文密钥。"""
        if status not in VALID_STATUSES:
            raise ValueError(f"非法状态：{status}")
        rid = run_id or f"rt_{datetime.now(timezone.utc):%Y%m%d%H%M%S}_{uuid.uuid4().hex[:10]}"
        started = started_at or utc_now_iso()
        args_list = [str(x) for x in args]
        env_redacted = redact_env(env or {})
        overrides_redacted = redact_env(env_overrides or {})
        secret_keys = redacted_key_names(env or {})
        extra_secret_keys = redacted_key_names(env_overrides or {})
        ctx = dict(git_context or {})
        if "git_context" not in (metadata or {}):
            meta = dict(metadata or {})
            meta["git_context"] = ctx
            meta["redacted_env_keys"] = secret_keys
            meta["redacted_env_override_keys"] = sorted(
                set(extra_secret_keys) | set(meta.get("redacted_env_override_keys") or [])
            )
        else:
            meta = dict(metadata or {})
        meta.setdefault("python_version", ".".join(str(v) for v in __import__("sys").version_info[:3]))
        meta.setdefault("platform", __import__("sys").platform)
        meta.setdefault("recorded_at", utc_now_iso())

        project = project_path or ctx.get("root") or cwd
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO runs (
                    id, name, command, args_json, cwd, env_json, env_overrides_json,
                    shell, timeout, status, exit_code, started_at, ended_at, duration_ms,
                    project_path, git_root, git_branch, git_commit, git_dirty,
                    tool_version, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rid,
                    name,
                    command,
                    json.dumps(args_list, ensure_ascii=False),
                    cwd,
                    json.dumps(env_redacted, ensure_ascii=False, sort_keys=True),
                    json.dumps(overrides_redacted, ensure_ascii=False, sort_keys=True),
                    int(bool(shell)),
                    timeout,
                    status,
                    exit_code,
                    started,
                    ended_at,
                    duration_ms,
                    str(project) if project else None,
                    ctx.get("root"),
                    ctx.get("branch"),
                    ctx.get("commit"),
                    None if ctx.get("dirty") is None else int(bool(ctx.get("dirty"))),
                    __version__,
                    json.dumps(meta, ensure_ascii=False, sort_keys=True),
                ),
            )
        return rid

    def finish_run(
        self,
        run_id: str,
        *,
        exit_code: int | None,
        status: str,
        ended_at: str | None = None,
        duration_ms: int | None = None,
    ) -> bool:
        if status not in VALID_STATUSES:
            raise ValueError(f"非法状态：{status}")
        ended = ended_at or utc_now_iso()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT started_at FROM runs WHERE id=?", (run_id,)
            ).fetchone()
            if row is None:
                return False
            if duration_ms is None:
                started = _parse_iso(row["started_at"])
                end_dt = _parse_iso(ended)
                if started and end_dt:
                    duration_ms = max(0, int((end_dt - started).total_seconds() * 1000))
            cur = self._conn.execute(
                """
                UPDATE runs SET exit_code=?, status=?, ended_at=?, duration_ms=?
                WHERE id=?
                """,
                (exit_code, status, ended, duration_ms, run_id),
            )
        return cur.rowcount > 0

    def update_status(self, run_id: str, status: str, *, note: str | None = None) -> bool:
        if status not in VALID_STATUSES:
            raise ValueError(f"非法状态：{status}")
        with self._lock, self._conn:
            if note:
                row = self._conn.execute(
                    "SELECT metadata_json FROM runs WHERE id=?", (run_id,)
                ).fetchone()
                if row is None:
                    return False
                meta = _json_load(row["metadata_json"], {})
                meta["status_note"] = note
                self._conn.execute(
                    "UPDATE runs SET status=?, metadata_json=? WHERE id=?",
                    (status, json.dumps(meta, ensure_ascii=False, sort_keys=True), run_id),
                )
                return True
            cur = self._conn.execute("UPDATE runs SET status=? WHERE id=?", (status, run_id))
        return cur.rowcount > 0

    def recover_interrupted_runs(self) -> int:
        """启动恢复：仍处于 running 的记录改为 interrupted 并在事务中补齐时间。"""
        now = utc_now_iso()
        recovered = 0
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT id, started_at FROM runs WHERE status='running'"
            ).fetchall()
            for row in rows:
                started = _parse_iso(row["started_at"])
                now_dt = _parse_iso(now)
                duration = (
                    max(0, int((now_dt - started).total_seconds() * 1000))
                    if started and now_dt
                    else None
                )
                cur = self._conn.execute(
                    """
                    UPDATE runs SET status='interrupted', ended_at=?, duration_ms=?,
                        metadata_json=json_set(metadata_json, '$.recovered_at', ?)
                    WHERE id=? AND status='running'
                    """,
                    (now, duration, now, row["id"]),
                )
                recovered += cur.rowcount
        return recovered

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return self._row_to_run(row) if row else None

    def _row_to_run(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["args"] = _json_load(data.pop("args_json", None), [])
        data["env"] = _json_load(data.pop("env_json", None), {})
        data["env_overrides"] = _json_load(data.pop("env_overrides_json", None), {})
        data["shell"] = bool(data.get("shell"))
        data["git_dirty"] = None if data.get("git_dirty") is None else bool(data.get("git_dirty"))
        data["metadata"] = _json_load(data.pop("metadata_json", None), {})
        data["git_context"] = data["metadata"].get("git_context") or {
            "is_git": bool(data.get("git_root")),
            "root": data.get("git_root"),
            "branch": data.get("git_branch"),
            "commit": data.get("git_commit"),
            "dirty": data.get("git_dirty"),
        }
        return data

    def list_runs(
        self,
        *,
        search: str | None = None,
        status: str | Iterable[str] | None = None,
        project: str | None = None,
        since: str | date | datetime | None = None,
        until: str | date | datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            statuses = [status] if isinstance(status, str) else [str(s) for s in status]
            clauses.append("r.status IN (" + ",".join("?" for _ in statuses) + ")")
            params.extend(statuses)
        if project:
            normalized = str(Path(project).expanduser().resolve()).replace("\\", "/")
            prefix = normalized.rstrip("/") + "/"
            clauses.append(
                "(REPLACE(r.project_path, '\\', '/') = ? OR "
                "REPLACE(r.project_path, '\\', '/') LIKE ? OR "
                "REPLACE(r.cwd, '\\', '/') = ? OR REPLACE(r.cwd, '\\', '/') LIKE ?)"
            )
            params.extend([normalized, prefix + "%", normalized, prefix + "%"])
        lower = _time_bound(since)
        upper = _time_bound(until, end=True)
        if lower:
            clauses.append("r.started_at >= ?")
            params.append(lower)
        if upper:
            clauses.append("r.started_at <= ?")
            params.append(upper)
        if search:
            pattern = f"%{search.strip()}%"
            clauses.append(
                "(r.name LIKE ? OR r.command LIKE ? OR r.args_json LIKE ? OR r.cwd LIKE ? "
                "OR r.project_path LIKE ? OR r.git_branch LIKE ? OR EXISTS ("
                "SELECT 1 FROM run_logs l WHERE l.run_id = r.id AND l.content LIKE ?))"
            )
            params.extend([pattern] * 7)
        limit = max(1, min(int(limit), 5000))
        offset = max(0, int(offset))
        query = "SELECT * FROM runs r"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY r.started_at DESC, r.id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_run(row) for row in rows]

    def search_runs(self, query: str, **kwargs: Any) -> list[dict[str, Any]]:
        return self.list_runs(search=query, **kwargs)

    # ---------- 日志 ----------
    def append_logs(self, run_id: str, stream: str, content: str) -> int:
        if stream not in ("stdout", "stderr"):
            raise ValueError(f"未知日志流：{stream}")
        if not content:
            return 0
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO run_logs (run_id, stream, content, created_at) VALUES (?, ?, ?, ?)",
                (run_id, stream, content, utc_now_iso()),
            )
        return int(cur.lastrowid or 0)

    def get_logs(self, run_id: str, stream: str | None = None) -> str:
        with self._lock:
            if stream:
                rows = self._conn.execute(
                    "SELECT content FROM run_logs WHERE run_id=? AND stream=? ORDER BY id",
                    (run_id, stream),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT content FROM run_logs WHERE run_id=? ORDER BY id", (run_id,)
                ).fetchall()
        return "".join(row["content"] for row in rows)

    def get_log_chunks(
        self, run_id: str, *, after_id: int = 0, limit: int = 500
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id, stream, content, created_at FROM run_logs
                WHERE run_id=? AND id>? ORDER BY id LIMIT ?
                """,
                (run_id, int(after_id), max(1, min(int(limit), 5000))),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_log_stats(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT stream, COUNT(*) AS chunks, COALESCE(SUM(LENGTH(content)), 0) AS chars
                FROM run_logs WHERE run_id=? GROUP BY stream
                """,
                (run_id,),
            ).fetchall()
        stats = {"stdout": {"chunks": 0, "chars": 0}, "stderr": {"chunks": 0, "chars": 0}}
        for row in rows:
            stats[row["stream"]] = {"chunks": row["chunks"], "chars": row["chars"]}
        return stats

    # ---------- 导出 ----------
    def build_export_payload(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run is None:
            raise KeyError(f"运行记录不存在：{run_id}")
        stdout = self.get_logs(run_id, "stdout")
        stderr = self.get_logs(run_id, "stderr")
        findings = []
        findings.extend(scan_sensitive_content(stdout, context="stdout"))
        findings.extend(scan_sensitive_content(stderr, context="stderr"))
        findings.extend(scan_sensitive_content(run.get("command", ""), context="command"))
        findings.extend(scan_sensitive_content(" ".join(run.get("args", [])), context="args"))
        for key in run.get("env", {}):
            findings.extend(scan_sensitive_content(str(key), context="env-key"))
        payload = {
            "schema": "runtrace.run.v1",
            "exported_at": utc_now_iso(),
            "run": run,
            "logs": {
                "stdout": stdout,
                "stderr": stderr,
            },
            "integrity": {
                "stdout_sha256": _sha256_text(stdout),
                "stderr_sha256": _sha256_text(stderr),
                "stdout_chars": len(stdout),
                "stderr_chars": len(stderr),
            },
            "security": {
                "environment_redacted": True,
                "sensitive_findings": findings,
                "warning": (
                    "发现疑似敏感内容，导出文件可能包含明文信息。" if findings else None
                ),
            },
        }
        return payload

    def export_run_json(self, run_id: str, output_path: str | os.PathLike[str]) -> dict[str, Any]:
        payload = self.build_export_payload(run_id)
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "path": str(target),
            "format": "json",
            "sensitive_findings": payload["security"]["sensitive_findings"],
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        }

    def export_run_text(self, run_id: str, output_path: str | os.PathLike[str]) -> dict[str, Any]:
        payload = self.build_export_payload(run_id)
        run = payload["run"]
        lines = [
            "RunTrace 运行导出",
            "=" * 60,
            f"Run ID: {run['id']}",
            f"名称: {run['name']}",
            f"状态: {run['status']} / 退出码: {run['exit_code']}",
            f"命令: {run['command']}",
            f"参数: {json.dumps(run['args'], ensure_ascii=False)}",
            f"工作目录: {run['cwd']}",
            f"Shell 模式: {'是' if run['shell'] else '否'}",
            f"超时: {run['timeout']}",
            f"开始: {run['started_at']}",
            f"结束: {run['ended_at']}",
            f"耗时(ms): {run['duration_ms']}",
            f"项目: {run['project_path']}",
            f"Git: {run['git_branch']} {run['git_commit']} dirty={run['git_dirty']}",
            f"工具版本: {run['tool_version']}",
            "",
            "环境（已脱敏）:",
            *[f"  {k}={v}" for k, v in sorted(run["env"].items())],
            "",
            "安全提示:",
            (
                json.dumps(payload["security"]["sensitive_findings"], ensure_ascii=False)
                if payload["security"]["sensitive_findings"]
                else "  未发现高置信度敏感内容"
            ),
            "",
            "STDOUT:",
            payload["logs"]["stdout"],
            "",
            "STDERR:",
            payload["logs"]["stderr"],
        ]
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines), encoding="utf-8")
        return {
            "path": str(target),
            "format": "text",
            "sensitive_findings": payload["security"]["sensitive_findings"],
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        }

    def export_run_archive(
        self, run_id: str, output_path: str | os.PathLike[str]
    ) -> dict[str, Any]:
        payload = self.build_export_payload(run_id)
        stdout = payload["logs"]["stdout"]
        stderr = payload["logs"]["stderr"]
        run_json = json.dumps(payload, ensure_ascii=False, indent=2)
        checksums = {
            "run.json": _sha256_text(run_json),
            "stdout.log": _sha256_text(stdout),
            "stderr.log": _sha256_text(stderr),
        }
        checksums_json = json.dumps(checksums, ensure_ascii=False, indent=2)
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.writestr("run.json", run_json)
            zf.writestr("stdout.log", stdout)
            zf.writestr("stderr.log", stderr)
            zf.writestr("checksums.json", checksums_json)
            zf.writestr(
                "README.txt",
                "RunTrace 运行归档\n"
                f"Run ID: {run_id}\n"
                "run.json 包含完整元数据（环境变量已脱敏）。\n"
                "checksums.json 记录各文件 SHA-256。导出前已执行敏感内容扫描，"
                "请查看 run.json 的 security 字段。\n",
            )
        return {
            "path": str(target),
            "format": "zip",
            "sensitive_findings": payload["security"]["sensitive_findings"],
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        }

    def export_run(
        self, run_id: str, output_path: str | os.PathLike[str], fmt: str = "json"
    ) -> dict[str, Any]:
        fmt = fmt.lower()
        if fmt == "json":
            return self.export_run_json(run_id, output_path)
        if fmt in ("txt", "text"):
            return self.export_run_text(run_id, output_path)
        if fmt in ("zip", "archive"):
            return self.export_run_archive(run_id, output_path)
        raise ValueError(f"不支持的导出格式：{fmt}")

    # ---------- 模板 / 再次运行 ----------
    def save_template(
        self,
        *,
        name: str,
        cwd: str,
        command: str,
        args: Iterable[str] = (),
        env_overrides: Mapping[str, object] | None = None,
        timeout: float | None = None,
        shell: bool = False,
        project_path: str | None = None,
    ) -> int:
        if not str(name).strip():
            raise ValueError("模板名称不能为空")
        now = utc_now_iso()
        # 模板同样不持久化敏感值；再次运行前用户需要重新提供密钥。
        safe_env = redact_env(env_overrides or {})
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO task_templates
                    (name, cwd, command, args_json, env_overrides_json, timeout,
                     shell, project_path, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    cwd=excluded.cwd,
                    command=excluded.command,
                    args_json=excluded.args_json,
                    env_overrides_json=excluded.env_overrides_json,
                    timeout=excluded.timeout,
                    shell=excluded.shell,
                    project_path=excluded.project_path,
                    updated_at=excluded.updated_at
                """,
                (
                    str(name).strip(),
                    cwd,
                    command,
                    json.dumps([str(x) for x in args], ensure_ascii=False),
                    json.dumps(safe_env, ensure_ascii=False, sort_keys=True),
                    timeout,
                    int(bool(shell)),
                    project_path,
                    now,
                    now,
                ),
            )
            row = self._conn.execute(
                "SELECT id FROM task_templates WHERE name=? COLLATE NOCASE", (str(name).strip(),)
            ).fetchone()
        return int(row["id"]) if row else 0

    def list_templates(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM task_templates ORDER BY updated_at DESC, name"
            ).fetchall()
        return [self._template_row(row) for row in rows]

    def get_template(self, name_or_id: str | int) -> dict[str, Any] | None:
        with self._lock:
            if isinstance(name_or_id, int) or str(name_or_id).isdigit():
                row = self._conn.execute(
                    "SELECT * FROM task_templates WHERE id=?", (int(name_or_id),)
                ).fetchone()
                if row:
                    return self._template_row(row)
            row = self._conn.execute(
                "SELECT * FROM task_templates WHERE name=? COLLATE NOCASE", (str(name_or_id),)
            ).fetchone()
        return self._template_row(row) if row else None

    def _template_row(self, row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["args"] = _json_load(data.pop("args_json", None), [])
        data["env_overrides"] = _json_load(data.pop("env_overrides_json", None), {})
        data["shell"] = bool(data.get("shell"))
        return data

    def delete_template(self, name_or_id: str | int) -> bool:
        with self._lock, self._conn:
            if isinstance(name_or_id, int) or str(name_or_id).isdigit():
                cur = self._conn.execute(
                    "DELETE FROM task_templates WHERE id=?", (int(name_or_id),)
                )
            else:
                cur = self._conn.execute(
                    "DELETE FROM task_templates WHERE name=? COLLATE NOCASE", (str(name_or_id),)
                )
        return cur.rowcount > 0

    def copy_template(self, source: str | int, new_name: str) -> int:
        tpl = self.get_template(source)
        if tpl is None:
            raise KeyError(f"模板不存在：{source}")
        return self.save_template(
            name=new_name,
            cwd=tpl["cwd"],
            command=tpl["command"],
            args=tpl["args"],
            env_overrides=tpl["env_overrides"],
            timeout=tpl["timeout"],
            shell=tpl["shell"],
            project_path=tpl["project_path"],
        )

    def config_from_template(self, name_or_id: str | int) -> RunConfig:
        tpl = self.get_template(name_or_id)
        if tpl is None:
            raise KeyError(f"模板不存在：{name_or_id}")
        env = {
            key: value
            for key, value in tpl["env_overrides"].items()
            if value != REDACTED
        }
        return RunConfig(
            name=tpl["name"],
            cwd=tpl["cwd"],
            command=tpl["command"],
            args=list(tpl["args"]),
            env_overrides=env,
            timeout=tpl["timeout"],
            shell=bool(tpl["shell"]),
            project_path=tpl["project_path"],
        )

    def config_from_run(self, run_id: str, *, name_suffix: str = "（再次运行）") -> RunConfig:
        run = self.get_run(run_id)
        if run is None:
            raise KeyError(f"运行记录不存在：{run_id}")
        env = {
            key: value
            for key, value in run["env_overrides"].items()
            if value != REDACTED
        }
        return RunConfig(
            name=f"{run['name']}{name_suffix}",
            cwd=run["cwd"],
            command=run["command"],
            args=list(run["args"]),
            env_overrides=env,
            timeout=run["timeout"],
            shell=bool(run["shell"]),
            project_path=run["project_path"],
        )


def recover_interrupted_runs(store: RunStore) -> int:
    """模块级恢复入口，便于启动代码显式调用。"""
    return store.recover_interrupted_runs()


def open_default_store() -> RunStore:
    return RunStore(default_db_path())


def wait_for_wal_checkpoint(db_path: str, timeout: float = 1.0) -> None:
    """测试辅助：确保 WAL 内容可被其他连接看到（通常无需调用）。"""
    deadline = time.monotonic() + timeout
    path = Path(str(db_path) + "-wal")
    while path.exists() and path.stat().st_size > 0 and time.monotonic() < deadline:
        time.sleep(0.05)
