from __future__ import annotations

import sqlite3

import pytest

from envforge.db import SCHEMA_VERSION, Database, DatabaseError, default_data_dir
from envforge.models import looks_sensitive_key, parse_version
from envforge.secrets import CryptoError, SecretCipher, mask_value


def test_schema_version_and_reopen(tmp_path):
    path = tmp_path / "a.db"
    with Database(path) as db:
        assert db.version == SCHEMA_VERSION
    with Database(path) as db:
        assert db.version == SCHEMA_VERSION


def test_schema_too_new_rejected(tmp_path):
    path = tmp_path / "b.db"
    with Database(path) as db:
        db.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    with pytest.raises(DatabaseError):
        Database(path)


def test_transaction_rolls_back_on_error(db):
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO projects(name, path, created_at, updated_at) VALUES('x','p','t','t')"
            )
            conn.execute(
                "INSERT INTO projects(name, path, created_at, updated_at) VALUES('x','p','t','t')"
            )
    assert db.query("SELECT COUNT(*) AS c FROM projects")[0]["c"] == 0


def test_integrity_check_on_healthy_database(db):
    assert db.integrity["ok"] is True
    assert db.integrity_check()["ok"] is True


def test_backup_to_creates_consistent_copy(db, tmp_path):
    db.execute("INSERT INTO projects(name, path, created_at, updated_at) VALUES('p','x','t','t')")
    target = db.backup_to(tmp_path / "copies" / "copy.db")
    assert target.exists()
    with Database(target) as copy:
        assert copy.query("SELECT COUNT(*) AS c FROM projects")[0]["c"] == 1
        assert copy.integrity["ok"] is True


def test_backup_daily_creates_once_per_day(db):
    first = db.backup_daily()
    assert first is not None and first.exists()
    assert db.backup_daily() is None


def test_close_is_idempotent(db):
    assert db.query_one("SELECT value FROM meta WHERE key='schema_version'")["value"] == "1"
    db.close()
    db.close()


def test_default_data_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("ENVFORGE_DATA_DIR", str(tmp_path / "data"))
    assert str(default_data_dir()).endswith("data")


def test_parse_version_cases():
    assert parse_version("Python 3.12.3") == "3.12.3"
    assert parse_version("v20.11.1") == "20.11.1"
    assert parse_version('openjdk version "21.0.2"') == "21.0.2"
    assert parse_version("no version here") == ""
    assert parse_version(None) == ""


def test_sensitive_key_detection():
    assert looks_sensitive_key("DB_PASSWORD")
    assert looks_sensitive_key("api_key")
    assert looks_sensitive_key("GITHUB_TOKEN")
    assert not looks_sensitive_key("HOME")


def test_mask_value_never_leaks():
    assert mask_value("secret") == "***"
    assert mask_value("a") == "***"
    assert mask_value("abcdef", visible_prefix=2, visible_suffix=2) == "ab***ef"
    assert mask_value("") == "***"


@pytest.mark.skipif(not SecretCipher().available, reason="当前环境没有 DPAPI")
def test_dpapi_roundtrip_including_unicode():
    cipher = SecretCipher()
    blob = cipher.protect("密码-测试-🔐")
    assert "密码".encode("utf-8") not in blob
    assert cipher.unprotect(blob) == "密码-测试-🔐"


@pytest.mark.skipif(not SecretCipher().available, reason="当前环境没有 DPAPI")
def test_dpapi_rejects_garbage():
    cipher = SecretCipher()
    with pytest.raises(CryptoError):
        cipher.unprotect(b"not-a-valid-blob")
