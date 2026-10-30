from runtrace.redaction import (
    REDACTED,
    is_sensitive_key,
    redact_env,
    redact_text_secrets,
    redacted_key_names,
    scan_sensitive_content,
)


def test_sensitive_env_keys_are_redacted():
    raw = {
        "TOKEN": "abc",
        "MY_SECRET": "super-secret-value",
        "DB_PASSWORD": "p@ss",
        "API_KEY": "sk-real",
        "NORMAL": "visible",
        "not_key": "another",
    }
    redacted = redact_env(raw)
    assert redacted["TOKEN"] == REDACTED
    assert redacted["MY_SECRET"] == REDACTED
    assert redacted["DB_PASSWORD"] == REDACTED
    assert redacted["API_KEY"] == REDACTED
    assert redacted["NORMAL"] == "visible"
    assert redacted["not_key"] == REDACTED  # KEY 片段命中默认策略


def test_explicit_allow_override_is_limited():
    raw = {"ALLOWED_KEY": "value", "EXTRA_TOKEN": "x"}
    redacted = redact_env(
        raw,
        extra_sensitive_keys=["EXTRA_TOKEN"],
        allow_plain_keys=["ALLOWED_KEY"],
    )
    assert redacted["ALLOWED_KEY"] == "value"
    assert redacted["EXTRA_TOKEN"] == REDACTED


def test_secret_values_are_absent_from_serialized_redaction():
    raw = {"GITHUB_TOKEN": "ghp_thisisasecretvalue1234567890", "PASSWORD": "hunter2"}
    redacted = redact_env(raw)
    blob = repr(redacted)
    assert "ghp_" not in blob
    assert "hunter2" not in blob
    assert redacted_key_names(raw) == ["GITHUB_TOKEN", "PASSWORD"]


def test_scan_reports_without_leaking_full_secret():
    secret = "abcdef1234567890"
    findings = scan_sensitive_content(f"MY_TOKEN={secret}\nrest ok", context="log")
    assert findings
    blob = repr(findings)
    assert secret not in blob
    assert "已遮蔽" in blob


def test_redact_text_secrets_and_is_sensitive_key():
    text = "token=abcdefghij and password: hunter2"
    redacted = redact_text_secrets(text)
    assert "abcdefghij" not in redacted
    assert "hunter2" not in redacted
    assert REDACTED in redacted
    assert is_sensitive_key("Authorization")
    assert not is_sensitive_key("DISPLAY")
