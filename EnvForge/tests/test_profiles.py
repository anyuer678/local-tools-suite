from __future__ import annotations

from pathlib import Path

import pytest

from envforge.errors import ConfirmationRequired, NotFoundError, ValidationError
from envforge.profiles import ProfileService, ProjectService

from conftest import FakeCipher


@pytest.fixture
def projects(db):
    return ProjectService(db)


@pytest.fixture
def profiles(db, projects, cipher, tmp_path):
    target = tmp_path / "demo-dir"
    target.mkdir(exist_ok=True)
    if projects.get("demo") is None:
        projects.add("demo", str(target))
    return ProfileService(db, projects, cipher)


def test_project_add_defaults_name_and_is_idempotent(tmp_path, projects):
    target = tmp_path / "my app"
    target.mkdir()
    entry = projects.add("", str(target))
    assert entry.name == "my app"
    again = projects.add("my app", str(target))
    assert again.id == entry.id


def test_project_same_name_conflict(projects, tmp_path):
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    projects.add("demo", str(one))
    with pytest.raises(ValidationError):
        projects.add("demo", str(two))


def test_project_name_case_insensitive_and_remove_cascade(db, projects, profiles):
    assert projects.get("demo") is not None
    assert projects.get("DEMO") is not None
    profiles.set_var("DEMO", "dev", "A", "1")
    projects.remove("demo")
    assert db.query("SELECT COUNT(*) AS c FROM profiles")[0]["c"] == 0
    assert db.query("SELECT COUNT(*) AS c FROM profile_vars")[0]["c"] == 0


def test_project_empty_path_invalid(projects):
    with pytest.raises(ValidationError):
        projects.add("x", "   ")


def test_sensitive_value_encrypted_at_rest(db, profiles):
    profiles.set_var("demo", "dev", "DB_PASSWORD", "s3cret")
    row = db.query_one(
        "SELECT * FROM profile_vars WHERE key = 'DB_PASSWORD'"
    )
    assert row is not None
    assert row["value"] == ""
    assert row["encrypted"] == 1
    assert row["secret_blob"]
    assert b"s3cret" not in bytes(row["secret_blob"])


def test_show_masks_and_reveal_decrypts(profiles):
    profiles.set_var("demo", "dev", "API_URL", "http://localhost")
    profiles.set_var("demo", "dev", "API_TOKEN", "abc123")
    masked = profiles.list_vars("demo", "dev")
    values = {item.key: item.value for item in masked}
    assert values["API_URL"] == "http://localhost"
    assert values["API_TOKEN"] == "***"
    reveal = {item.key: item.value for item in profiles.list_vars("demo", "dev", reveal=True)}
    assert reveal["API_TOKEN"] == "abc123"
    value, status = profiles.reveal_var("demo", "dev", "api_token")
    assert value == "abc123" and status == "ok"


def test_plain_override_keeps_value_readable(profiles):
    profiles.set_var("demo", "dev", "API_TOKEN", "plain-value", sensitive=False)
    shown = {item.key: item.value for item in profiles.list_vars("demo", "dev")}
    assert shown["API_TOKEN"] == "plain-value"


def test_unavailable_cipher_stores_no_secret(db, projects, tmp_path):
    if projects.get("demo") is None:
        target = tmp_path / "demo-unavailable"
        target.mkdir(exist_ok=True)
        projects.add("demo", str(target))
    service = ProfileService(db, projects, FakeCipher(available=False))
    _var, warning = service.set_var("demo", "dev", "PASSWORD", "secret")
    assert warning
    item = service.list_vars("demo", "dev")[0]
    assert item.stored is False
    value, status = service.reveal_var("demo", "dev", "PASSWORD")
    assert value is None and status == "unavailable"


def test_invalid_key_rejected(profiles):
    with pytest.raises(ValidationError):
        profiles.set_var("demo", "dev", "BAD KEY", "1")
    with pytest.raises(ValidationError):
        profiles.set_var("demo", "dev", "", "1")


def test_variable_names_are_case_insensitive(db, profiles):
    profiles.set_var("demo", "dev", "api_key", "one")
    profiles.set_var("demo", "dev", "API_KEY", "two")
    assert db.query("SELECT COUNT(*) AS c FROM profile_vars")[0]["c"] == 1
    assert profiles.reveal_var("demo", "dev", "API_KEY")[0] == "two"


def test_unset_missing_variable(profiles):
    with pytest.raises(NotFoundError):
        profiles.unset_var("demo", "dev", "NOPE")


def test_profile_upsert_idempotent_and_list(profiles):
    profiles.upsert_profile("demo", "dev", "开发")
    profiles.upsert_profile("demo", "prod", "生产", active=True)
    names = [item.name for item in profiles.list_profiles("demo")]
    assert set(names) == {"dev", "prod"}
    assert profiles.get_profile("demo", "prod").active is True


def test_export_masked_by_default_and_quotes(profiles):
    profiles.set_var("demo", "dev", "API_URL", "http://x")
    profiles.set_var("demo", "dev", "GREETING", "hello world")
    profiles.set_var("demo", "dev", "DB_PASSWORD", "secret")
    text = profiles.export_dotenv("demo", "dev")
    assert "DB_PASSWORD=***" in text
    assert "secret" not in text
    assert 'GREETING="hello world"' in text
    revealed = profiles.export_dotenv("demo", "dev", reveal=True)
    assert "DB_PASSWORD=secret" in revealed


def test_profile_diff(profiles):
    profiles.set_var("demo", "dev", "A", "1")
    profiles.set_var("demo", "dev", "B", "2")
    profiles.set_var("demo", "prod", "A", "9")
    profiles.set_var("demo", "prod", "C", "3")
    changes = {item.key: item for item in profiles.diff("demo", "dev", "prod")}
    assert changes["A"].before == "1" and changes["A"].after == "9"
    assert changes["B"].after is None
    assert changes["C"].before is None


def test_activation_requires_confirmation(profiles):
    profiles.set_var("demo", "dev", "A", "1")
    with pytest.raises(ConfirmationRequired):
        profiles.activation("demo", "dev")
    env, warnings = profiles.activation("demo", "dev", confirm=True)
    assert env["A"] == "1"
    assert env["ENVFORGE_PROFILE"] == "dev"
    assert warnings == []


def test_activation_skips_undecryptable(db, projects, tmp_path):
    if projects.get("demo") is None:
        target = tmp_path / "demo-undecryptable"
        target.mkdir(exist_ok=True)
        projects.add("demo", str(target))
    unavailable = ProfileService(db, projects, FakeCipher(available=False))
    unavailable.set_var("demo", "dev", "PASSWORD", "x")
    env, warnings = unavailable.activation("demo", "dev", confirm=True)
    assert "PASSWORD" not in env
    assert warnings and "PASSWORD" in warnings[0]


def test_remove_profile_and_project_path_unicode(projects, profiles, tmp_path):
    target = tmp_path / "中文 目录"
    target.mkdir()
    entry = projects.add("中文项目", str(target))
    assert Path(entry.path) == target.resolve()
    profiles.upsert_profile("中文项目", "测试")
    profiles.remove_profile("中文项目", "测试")
    assert profiles.list_profiles("中文项目") == []
