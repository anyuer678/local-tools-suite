"""项目与环境档案：多环境（dev/test/prod）变量、敏感值加密、脱敏导出与差异。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .db import Database
from .errors import ConfirmationRequired, NotFoundError, ValidationError
from .models import (
    ProfileEntry,
    ProfileVar,
    ProjectEntry,
    VarChange,
    looks_sensitive_key,
    now_iso,
)
from .secrets import REDACTED, SecretCipher

_KEY_RE = re.compile(r"^[^=\s]+$")
_DOTENV_SPECIAL = re.compile(r"[\s\"'$`\\]")


def _normalize_path(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        raise ValidationError("项目路径不能为空。")
    path = Path(text).expanduser()
    try:
        return str(path.resolve(strict=False))
    except OSError:
        return str(path.absolute())


def _validate_key(key: str) -> str:
    text = (key or "").strip()
    if not text:
        raise ValidationError("变量名不能为空。")
    if not _KEY_RE.match(text):
        raise ValidationError(f"变量名 {text!r} 含有空白或 '='，不是合法的环境变量名。")
    if len(text) > 256:
        raise ValidationError("变量名过长（上限 256 字符）。")
    return text


class ProjectService:
    """项目登记：名称唯一（不区分大小写），路径标准化存储。"""

    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, name: str, path: str) -> ProjectEntry:
        label = (name or "").strip() or Path(path).name
        if not label:
            raise ValidationError("项目名称不能为空。")
        normalized = _normalize_path(path)
        existing = self.get(label)
        now = now_iso()
        if existing:
            if existing.path == normalized:
                return existing
            raise ValidationError(
                f"项目 {label!r} 已存在且指向 {existing.path}；"
                "请换一个名称，或先删除旧项目记录。"
            )
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO projects(name, path, created_at, updated_at) VALUES(?,?,?,?)",
                (label, normalized, now, now),
            )
            new_id = int(cursor.lastrowid)
        return ProjectEntry(name=label, path=normalized, id=new_id, created_at=now, updated_at=now)

    def get(self, name: str) -> ProjectEntry | None:
        row = self.db.query_one(
            "SELECT * FROM projects WHERE name = ? COLLATE NOCASE", (name.strip(),)
        )
        if not row:
            return None
        return ProjectEntry(
            name=row["name"],
            path=row["path"],
            id=row["id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def require(self, name: str) -> ProjectEntry:
        entry = self.get(name)
        if entry is None:
            raise NotFoundError(f"项目 {name!r} 不存在；请先用 `envforge project add` 登记。")
        return entry

    def list(self) -> list[ProjectEntry]:
        rows = self.db.query("SELECT * FROM projects ORDER BY name COLLATE NOCASE")
        return [
            ProjectEntry(
                name=row["name"],
                path=row["path"],
                id=row["id"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def remove(self, name: str) -> None:
        entry = self.require(name)
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM projects WHERE id = ?", (entry.id,))

    def touch(self, name: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE projects SET updated_at = ? WHERE name = ? COLLATE NOCASE",
                (now_iso(), name),
            )


class ProfileService:
    """环境档案读写。敏感值只以 DPAPI 密文落库，展示与导出默认脱敏。"""

    def __init__(
        self,
        db: Database,
        projects: ProjectService | None = None,
        cipher: SecretCipher | None = None,
        audit: AuditLog | None = None,
    ) -> None:
        self.db = db
        self.projects = projects or ProjectService(db)
        self.cipher = cipher or SecretCipher()
        self.audit = audit

    # ------------------------------------------------------------------ 档案
    def upsert_profile(
        self,
        project_name: str,
        profile_name: str,
        description: str | None = None,
        active: bool | None = None,
    ) -> ProfileEntry:
        project = self.projects.require(project_name)
        name = (profile_name or "").strip()
        if not name:
            raise ValidationError("档案名称不能为空。")
        if len(name) > 64:
            raise ValidationError("档案名称过长（上限 64 字符）。")
        row = self.db.query_one(
            "SELECT * FROM profiles WHERE project_id = ? AND name = ? COLLATE NOCASE",
            (project.id, name),
        )
        now = now_iso()
        with self.db.transaction() as conn:
            if row:
                if description is not None:
                    conn.execute(
                        "UPDATE profiles SET description = ?, updated_at = ? WHERE id = ?",
                        (description, now, row["id"]),
                    )
                if active is not None:
                    conn.execute("UPDATE profiles SET active = ? WHERE id = ?", (int(active), row["id"]))
                if active:
                    conn.execute(
                        "UPDATE profiles SET active = 0 WHERE project_id = ? AND id != ?",
                        (project.id, row["id"]),
                    )
                profile_id = int(row["id"])
            else:
                cursor = conn.execute(
                    "INSERT INTO profiles(project_id, name, description, active, created_at, updated_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (project.id, name, description or "", int(bool(active)), now, now),
                )
                profile_id = int(cursor.lastrowid)
                if active:
                    conn.execute(
                        "UPDATE profiles SET active = 0 WHERE project_id = ? AND id != ?",
                        (project.id, profile_id),
                    )
        self.projects.touch(project.name)
        return self.get_profile(project_name, name)

    def get_profile(self, project_name: str, profile_name: str) -> ProfileEntry:
        project = self.projects.require(project_name)
        row = self.db.query_one(
            "SELECT * FROM profiles WHERE project_id = ? AND name = ? COLLATE NOCASE",
            (project.id, profile_name.strip()),
        )
        if not row:
            raise NotFoundError(
                f"项目 {project.name!r} 下没有档案 {profile_name!r}；"
                "请先创建，例如 `envforge profile set <项目> <档案> KEY=VALUE`。"
            )
        return ProfileEntry(
            project_id=row["project_id"],
            name=row["name"],
            description=row["description"],
            active=bool(row["active"]),
            id=row["id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_profiles(self, project_name: str) -> list[ProfileEntry]:
        project = self.projects.require(project_name)
        rows = self.db.query(
            "SELECT * FROM profiles WHERE project_id = ? ORDER BY active DESC, name COLLATE NOCASE",
            (project.id,),
        )
        return [
            ProfileEntry(
                project_id=row["project_id"],
                name=row["name"],
                description=row["description"],
                active=bool(row["active"]),
                id=row["id"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    def remove_profile(self, project_name: str, profile_name: str) -> None:
        entry = self.get_profile(project_name, profile_name)
        keys = [row["key"] for row in self._rows(project_name, profile_name)]
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM profiles WHERE id = ?", (entry.id,))
        self._audit("profile.remove", project_name, profile_name, keys, "ok", "档案及其变量已删除")

    # ------------------------------------------------------------------ 变量
    def set_var(
        self,
        project_name: str,
        profile_name: str,
        key: str,
        value: str,
        sensitive: bool | None = None,
    ) -> tuple[ProfileVar, str | None]:
        """写入变量。返回（变量视图，警告信息）。

        警告信息非空表示敏感值未能安全落库（当前平台不支持加密）。
        """
        self.upsert_profile(project_name, profile_name)
        profile = self.get_profile(project_name, profile_name)
        clean_key = _validate_key(key)
        is_sensitive = looks_sensitive_key(clean_key) if sensitive is None else bool(sensitive)
        now = now_iso()
        warning: str | None = None
        blob: bytes | None = None
        stored_value = value
        encrypted = False
        if is_sensitive:
            if self.cipher.available:
                blob = self.cipher.protect(value)
                stored_value = ""
                encrypted = True
            else:
                stored_value = ""
                warning = (
                    "当前系统不支持 DPAPI，敏感值未保存（数据库只记录键名）。"
                    "请在支持的 Windows 环境重新设置。"
                )
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO profile_vars(profile_id, key, value, sensitive, encrypted, secret_blob,"
                " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)"
                " ON CONFLICT(profile_id, key) DO UPDATE SET value = excluded.value,"
                " sensitive = excluded.sensitive, encrypted = excluded.encrypted,"
                " secret_blob = excluded.secret_blob, updated_at = excluded.updated_at",
                (profile.id, clean_key, stored_value, int(is_sensitive), int(encrypted), blob, now, now),
            )
        self.projects.touch(project_name)
        view = ProfileVar(
            key=clean_key,
            value=REDACTED if is_sensitive else value,
            sensitive=is_sensitive,
            encrypted=encrypted,
            stored=encrypted or not is_sensitive,
            updated_at=now,
        )
        self._audit(
            "profile.set",
            project_name,
            profile.name,
            [clean_key],
            "ok" if not warning else "degraded",
            warning or ("敏感值已加密存储" if encrypted else ""),
        )
        return view, warning

    def unset_var(self, project_name: str, profile_name: str, key: str) -> None:
        profile = self.get_profile(project_name, profile_name)
        clean_key = _validate_key(key)
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM profile_vars WHERE profile_id = ? AND key = ? COLLATE NOCASE",
                (profile.id, clean_key),
            )
        if cursor.rowcount == 0:
            raise NotFoundError(f"档案 {profile.name!r} 中没有变量 {clean_key!r}。")
        self.projects.touch(project_name)
        self._audit("profile.unset", project_name, profile.name, [clean_key], "ok", "")

    def _audit(
        self,
        action: str,
        project_name: str,
        profile_name: str,
        keys: list[str],
        result: str = "ok",
        detail: str = "",
    ) -> None:
        if self.audit is None:
            return
        self.audit.append(
            action,
            scope=f"{project_name}/{profile_name}",
            keys=keys,
            result=result,
            detail=detail,
        )

    def _rows(self, project_name: str, profile_name: str) -> list[dict[str, Any]]:
        profile = self.get_profile(project_name, profile_name)
        return self.db.query(
            "SELECT * FROM profile_vars WHERE profile_id = ? ORDER BY key COLLATE NOCASE",
            (profile.id,),
        )

    def _decrypt(self, row: dict[str, Any]) -> tuple[str | None, str]:
        """返回（明文或 None，状态）。状态：ok / unavailable / error。"""
        if not row["sensitive"]:
            return row["value"], "ok"
        if not row["encrypted"] or not row["secret_blob"]:
            return None, "unavailable"
        try:
            return self.cipher.unprotect(bytes(row["secret_blob"])), "ok"
        except Exception:
            return None, "error"

    def list_vars(
        self, project_name: str, profile_name: str, reveal: bool = False
    ) -> list[ProfileVar]:
        result: list[ProfileVar] = []
        for row in self._rows(project_name, profile_name):
            sensitive = bool(row["sensitive"])
            encrypted = bool(row["encrypted"])
            if sensitive:
                if reveal:
                    value, status = self._decrypt(row)
                    display = value if status == "ok" and value is not None else ""
                else:
                    display = REDACTED
                result.append(
                    ProfileVar(
                        key=row["key"],
                        value=display,
                        sensitive=True,
                        encrypted=encrypted,
                        stored=encrypted,
                        updated_at=row["updated_at"],
                    )
                )
            else:
                result.append(
                    ProfileVar(
                        key=row["key"],
                        value=row["value"],
                        sensitive=False,
                        encrypted=False,
                        stored=True,
                        updated_at=row["updated_at"],
                    )
                )
        return result

    def reveal_var(self, project_name: str, profile_name: str, key: str) -> tuple[str | None, str]:
        profile = self.get_profile(project_name, profile_name)
        row = self.db.query_one(
            "SELECT * FROM profile_vars WHERE profile_id = ? AND key = ? COLLATE NOCASE",
            (profile.id, _validate_key(key)),
        )
        if not row:
            raise NotFoundError(f"档案 {profile.name!r} 中没有变量 {key!r}。")
        return self._decrypt(row)

    def activation(
        self, project_name: str, profile_name: str, confirm: bool = False
    ) -> tuple[dict[str, str], list[str]]:
        """生成可真实使用的环境变量集合（会解密敏感值），必须显式确认。"""
        if not confirm:
            raise ConfirmationRequired(
                "激活档案会解密敏感值并写入当前会话环境。请加 --yes 显式确认。"
            )
        profile = self.get_profile(project_name, profile_name)
        env: dict[str, str] = {}
        warnings: list[str] = []
        for row in self._rows(project_name, profile_name):
            value, status = self._decrypt(row)
            if status != "ok" or value is None:
                warnings.append(f"{row['key']}：敏感值不可解密（状态 {status}），已跳过。")
                continue
            env[row["key"]] = value
        env["ENVFORGE_PROJECT"] = self.projects.require(project_name).path
        env["ENVFORGE_PROFILE"] = profile.name
        self._audit(
            "profile.activate",
            project_name,
            profile_name,
            [row["key"] for row in self._rows(project_name, profile_name)],
            "ok" if not warnings else "degraded",
            "已解密并生成会话激活脚本",
        )
        return env, warnings

    def export_dotenv(
        self, project_name: str, profile_name: str, reveal: bool = False
    ) -> str:
        profile = self.get_profile(project_name, profile_name)
        header = [
            f"# EnvForge 导出：项目 {project_name} / 档案 {profile.name}",
            "# 敏感值默认以 *** 占位；使用 --reveal 才会导出明文（请谨慎保存）。",
        ]
        lines = list(header)
        for row in self._rows(project_name, profile_name):
            if row["sensitive"]:
                if reveal:
                    value, status = self._decrypt(row)
                    if status != "ok" or value is None:
                        lines.append(f"# {row['key']}=<敏感值不可解密，未导出>")
                        continue
                else:
                    value = REDACTED
            else:
                value = row["value"]
            lines.append(f"{row['key']}={_dotenv_quote(value)}")
        return "\n".join(lines) + "\n"

    def diff(
        self, project_name: str, profile_a: str, profile_b: str, reveal: bool = False
    ) -> list[VarChange]:
        a = {item.key.upper(): item for item in self.list_vars(project_name, profile_a, reveal=reveal)}
        b = {item.key.upper(): item for item in self.list_vars(project_name, profile_b, reveal=reveal)}
        changes: list[VarChange] = []
        for key in sorted(set(a) | set(b)):
            left = a.get(key)
            right = b.get(key)
            if left is None:
                changes.append(VarChange(key=right.key, before=None, after=right.value, sensitive=right.sensitive))
            elif right is None:
                changes.append(VarChange(key=left.key, before=left.value, after=None, sensitive=left.sensitive))
            elif left.value != right.value:
                changes.append(
                    VarChange(
                        key=left.key,
                        before=left.value,
                        after=right.value,
                        sensitive=left.sensitive or right.sensitive,
                    )
                )
        return changes


def _dotenv_quote(value: str) -> str:
    if value == "":
        return '""'
    if _DOTENV_SPECIAL.search(value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value
