from __future__ import annotations

from pathlib import Path

import pytest

from envforge.audit import AuditLog
from envforge.doctor import Doctor, summarize
from envforge.envstore import EnvService
from envforge.envvars import (
    RegistryProvider,
    SessionProvider,
    analyze_path,
    dedupe_path,
    provider_for,
    split_path,
)
from envforge.errors import (
    ConfirmationRequired,
    EnvAccessError,
    EnvWriteFailure,
    NotFoundError,
    ValidationError,
)
from envforge.lock import file_lock
from envforge.profiles import ProfileService, ProjectService
from envforge.runtimes import RuntimeService

from conftest import FakeCipher, MemoryProvider


def test_split_path_handles_quotes_and_spaces():
    assert split_path(r'"C:\Program Files\A";C:\b ;;') == [r"C:\Program Files\A", r"C:\b"]
    assert split_path("") == []


def test_dedupe_path_removes_empty_and_duplicates():
    cleaned, removed = dedupe_path(r"C:\a;;C:\A;C:\a;")
    assert cleaned == r"C:\a"
    assert removed == 4
    assert dedupe_path(r"C:\a;C:\b")[1] == 0


def test_analyze_path_marks_duplicate_and_missing(tmp_path):
    present = tmp_path / "present"
    present.mkdir()
    value = f"{present};{tmp_path / 'missing'};{present};"
    entries = analyze_path(value)
    assert entries[0].exists is True and entries[0].duplicate_of is None
    assert entries[1].exists is False
    assert entries[2].duplicate_of == 0
    assert entries[3].raw == "" and entries[3].exists is False


def test_provider_for_validation():
    with pytest.raises(ValidationError):
        provider_for("galaxy")
    assert provider_for("session").scope == "session"


def test_session_provider_roundtrip():
    provider = SessionProvider()
    provider.write({"ENVFORGE_TEST_TMP": "1"})
    try:
        assert provider.get("ENVFORGE_TEST_TMP") == "1"
        provider.write({"ENVFORGE_TEST_TMP": None})
        assert provider.get("ENVFORGE_TEST_TMP") is None
    finally:
        import os

        os.environ.pop("ENVFORGE_TEST_TMP", None)


def test_system_registry_provider_is_read_only():
    provider = RegistryProvider("system")
    assert provider.writable is False
    with pytest.raises(EnvAccessError):
        provider.write({"X": "1"})


def test_snapshot_create_list_get_delete(env_service):
    snapshot = env_service.create("user", "base")
    assert snapshot.id == 1 and len(snapshot.values) == 2
    assert env_service.list("user")[0].label == "base"
    assert env_service.get(1).values["USERNAME"] == "tester"
    env_service.delete(1)
    with pytest.raises(NotFoundError):
        env_service.get(1)


def test_snapshot_unknown_scope(env_service):
    with pytest.raises(ValidationError):
        env_service.create("galaxy")


def test_diff_masks_sensitive_values(env_service, provider):
    env_service.create("user", "before")
    provider.write({"PATH": r"C:\Windows", "API_TOKEN": "abc"})
    env_service.create("user", "after")
    data = env_service.diff(1, 2)
    assert data["added"]["API_TOKEN"] == "***"
    assert data["changed"][0]["key"] == "PATH"
    revealed = env_service.diff(1, 2, reveal=True)
    assert revealed["added"]["API_TOKEN"] == "abc"
    assert data["path_removed"]


def test_export_marks_masking(env_service, provider):
    provider.write({"API_TOKEN": "abc"})
    snapshot = env_service.create("user")
    masked = env_service.export(snapshot.id)
    assert masked["format"] == "envforge-snapshot-v1"
    assert masked["values"]["API_TOKEN"] == "***"
    assert masked["sensitive_values_masked"] is True
    assert env_service.export(snapshot.id, reveal=True)["values"]["API_TOKEN"] == "abc"


def test_apply_requires_confirmation(env_service, provider):
    with pytest.raises(ConfirmationRequired):
        env_service.apply("user", {"NEW_KEY": "1"})
    assert "NEW_KEY" not in provider.values


def test_apply_creates_backup_and_writes(env_service, provider):
    result = env_service.apply("user", {"NEW_KEY": "1", "USERNAME": None}, confirm=True)
    assert provider.values["NEW_KEY"] == "1"
    assert "USERNAME" not in provider.values
    assert result["pre_snapshot_id"] == 1
    assert result["changed"] == ["NEW_KEY"] and result["deleted"] == ["USERNAME"]


def test_apply_empty_changes(env_service):
    with pytest.raises(ValidationError):
        env_service.apply("user", {}, confirm=True)


def test_apply_read_only_scope_raises(env_service):
    with pytest.raises(EnvAccessError):
        env_service.apply("system", {"X": "1"}, confirm=True)


def test_restore_noop_when_unchanged(env_service):
    snapshot = env_service.create("user")
    result = env_service.restore(snapshot.id, confirm=True)
    assert result["changed"] == [] and result["pre_snapshot_id"] is None


def test_restore_reverts_and_optionally_deletes(env_service, provider):
    snapshot = env_service.create("user", "base")
    provider.write({"PATH": r"C:\Windows", "NEW_KEY": "x"})
    result = env_service.restore(snapshot.id, confirm=True, delete_extras=True)
    assert "NEW_KEY" not in provider.values
    assert provider.values["PATH"] == r"C:\Windows;C:\Windows\System32"
    assert result["restored_snapshot_id"] == snapshot.id


def test_restore_read_only_raises(env_service):
    system_snapshot = env_service.create("system")
    with pytest.raises(EnvAccessError):
        env_service.restore(system_snapshot.id, confirm=True)


def test_path_report_statuses(env_service, provider, tmp_path):
    existing = tmp_path / "exists"
    existing.mkdir()
    provider.write({"PATH": f"{existing};{tmp_path / 'missing'};{existing};"})
    report = env_service.path_report("user")
    statuses = [item["status"] for item in report]
    assert statuses == ["ok", "missing", "duplicate", "empty"]


def _doctor_env(db, tmp_path):
    existing = tmp_path / "bin"
    existing.mkdir()
    path_value = f"{existing};{tmp_path / 'gone'};{existing};"
    user = MemoryProvider("user", {"PATH": path_value})
    system = MemoryProvider("system", {"PATH": path_value}, writable=False)
    service = EnvService(db, {"user": user, "system": system})
    return service


def test_doctor_reports_path_and_runtime_issues(db, tmp_path):
    service = _doctor_env(db, tmp_path)
    runtimes = RuntimeService(db, lambda name: None, lambda path, kind: ("", ""))
    exe = tmp_path / "python.exe"
    exe.write_text("stub", encoding="utf-8")
    runtimes.register("python", str(exe), "3.12.0")
    runtimes.register("python", str(exe), "3.12.0")
    exe.unlink()
    projects = ProjectService(db)
    projects.add("ghost", str(tmp_path / "no-such-project"))

    findings = Doctor(db, service, runtimes, projects).run(["user"])
    codes = {item.code for item in findings}
    assert "path-duplicate-user" in codes
    assert "path-missing-dir-user" in codes
    assert "runtime-missing" in codes
    assert "project-path-missing" in codes
    counts = summarize(findings)
    assert counts["warn"] >= 3


def test_doctor_reports_unencrypted_secret(db, tmp_path):
    service = _doctor_env(db, tmp_path)
    projects = ProjectService(db)
    target = tmp_path / "proj"
    target.mkdir(exist_ok=True)
    projects.add("demo", str(target))
    profiles = ProfileService(db, projects, FakeCipher(available=False))
    profiles.set_var("demo", "dev", "DB_PASSWORD", "x")
    findings = Doctor(db, service, projects=projects).run(["user"])
    assert any(item.code == "profile-secret-unencrypted" for item in findings)


def test_doctor_clean_environment_has_no_errors(db, tmp_path):
    existing = tmp_path / "bin"
    existing.mkdir()
    user = MemoryProvider("user", {"PATH": str(existing)})
    service = EnvService(db, {"user": user, "system": MemoryProvider("system", {}, False)})
    findings = Doctor(db, service).run(["user"])
    assert summarize(findings)["error"] == 0
    assert not any(item.code.startswith("path-") for item in findings)


def test_doctor_unreadable_scope_reports_warning(db, tmp_path):
    class BrokenProvider(MemoryProvider):
        def list_vars(self):
            raise EnvAccessError("拒绝访问")

    service = EnvService(db, {"user": BrokenProvider("user"), "system": MemoryProvider("system", {}, False)})
    findings = Doctor(db, service).run(["user"])
    assert any(item.code == "env-access-user" for item in findings)


def test_snapshot_values_are_independent_copies(env_service, provider):
    snapshot = env_service.create("user")
    provider.write({"PATH": "changed"})
    assert snapshot.values["PATH"] == r"C:\Windows;C:\Windows\System32"
    assert env_service.get(snapshot.id).values["PATH"] == r"C:\Windows;C:\Windows\System32"


# ---------------------------------------------------------------------- 写入事务化
class FailingWriteProvider(MemoryProvider):
    """首次写入在指定键处抛错，模拟注册表写入中途失败。"""

    def __init__(self, values, fail_key="BOOM"):
        super().__init__("user", values)
        self.fail_key = fail_key
        self.writes = 0

    def write(self, updates):
        self.writes += 1
        if self.writes > 1:
            super().write(updates)
            return
        for key, value in updates.items():
            if key == self.fail_key:
                raise OSError("模拟注册表写入失败")
            self.values[key] = value


class DriftProvider(MemoryProvider):
    """首次写入后值被系统改写，用于触发写后回读校验失败。"""

    def __init__(self, values):
        super().__init__("user", values)
        self.writes = 0

    def write(self, updates):
        self.writes += 1
        if self.writes > 1:
            super().write(updates)
            return
        for key, value in updates.items():
            if value is None:
                self.values.pop(key, None)
            else:
                self.values[key] = f"{value}!"


def test_apply_marks_verified(env_service):
    result = env_service.apply("user", {"NEW_KEY": "1"}, confirm=True)
    assert result["verified"] is True
    assert result["rollback_ok"] is None


def test_apply_rolls_back_on_write_exception(db):
    provider = FailingWriteProvider({"PATH": r"C:\Windows"})
    service = EnvService(db, {"user": provider})
    with pytest.raises(EnvWriteFailure) as info:
        service.apply("user", {"FIRST": "1", "BOOM": "2"}, confirm=True)
    assert info.value.rollback_ok is True and info.value.partial is False
    assert "FIRST" not in provider.values
    assert provider.values["PATH"] == r"C:\Windows"
    assert info.value.pre_snapshot_id is not None


def test_apply_rolls_back_on_readback_mismatch(db):
    provider = DriftProvider({"PATH": r"C:\Windows"})
    service = EnvService(db, {"user": provider})
    with pytest.raises(EnvWriteFailure) as info:
        service.apply("user", {"MISMATCHED": "v"}, confirm=True)
    assert info.value.failed_keys == ["MISMATCHED"]
    assert provider.values.get("MISMATCHED") is None


def test_apply_rejects_concurrent_writer(env_service):
    lock_dir = Path(env_service.db.path).parent
    with file_lock("env-user", lock_dir=lock_dir, timeout=1):
        with pytest.raises(EnvAccessError) as info:
            env_service.apply("user", {"A": "1"}, confirm=True, lock_timeout=0.05)
    assert "另一个" in str(info.value)


# ---------------------------------------------------------------------- 审计
def test_audit_records_keys_but_not_values(db, provider, tmp_path):
    log = AuditLog(tmp_path / "logs")
    service = EnvService(db, {"user": provider}, audit=log)
    service.apply("user", {"API_TOKEN": "super-secret"}, confirm=True)
    entries = log.recent(10)
    assert entries[0]["action"] == "env.apply"
    assert entries[0]["keys"] == ["API_TOKEN"] and entries[0]["result"] == "ok"
    raw = "".join(p.read_text(encoding="utf-8") for p in (tmp_path / "logs").glob("*.jsonl"))
    assert "super-secret" not in raw


def test_audit_records_failed_write_as_rolled_back(db, tmp_path):
    provider = DriftProvider({"PATH": r"C:\Windows"})
    log = AuditLog(tmp_path / "logs")
    service = EnvService(db, {"user": provider}, audit=log)
    with pytest.raises(EnvWriteFailure):
        service.apply("user", {"A": "1"}, confirm=True)
    assert log.recent(5)[0]["result"] == "rolled_back"
