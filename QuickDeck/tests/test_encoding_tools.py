from __future__ import annotations

import base64
import json

import pytest

from quickdeck.builtin_actions import (
    EncodingToolError,
    base64_decode,
    base64_encode,
    format_json,
    unicode_escape,
    unicode_unescape,
    url_decode,
    url_encode,
    validate_json,
)


def test_url_roundtrip() -> None:
    text = "你好 world & a=1"
    encoded = url_encode(text)
    assert "%E4%BD%A0%E5%A5%BD" in encoded
    assert " " not in encoded
    assert url_decode(encoded) == text
    assert url_decode("") == ""


def test_base64_roundtrip_unicode() -> None:
    text = "你好 QuickDeck"
    encoded = base64_encode(text)
    assert encoded == base64.b64encode(text.encode()).decode()
    assert base64_decode(encoded) == text
    assert base64_decode(encoded + "\n") == text


@pytest.mark.parametrize("bad", ["", "!!!!", "a", "abc"])
def test_base64_invalid(bad: str) -> None:
    with pytest.raises(EncodingToolError):
        base64_decode(bad)


def test_base64_invalid_utf8() -> None:
    raw = base64.b64encode(b"\xff\xfe").decode()
    with pytest.raises(EncodingToolError, match="UTF-8"):
        base64_decode(raw)


def test_unicode_escape_roundtrip() -> None:
    text = "你好\nworld\\"
    escaped = unicode_escape(text)
    assert "\\u4f60" in escaped
    assert unicode_unescape(escaped) == text
    assert unicode_unescape(r"\x41\u0042") == "AB"


def test_json_format_and_validate() -> None:
    raw = '{"b": 2, "a": "你好"}'
    formatted = format_json(raw, sort_keys=True)
    assert json.loads(formatted) == {"a": "你好", "b": 2}
    assert '\n  "a"' in formatted
    assert "\\u4f60" not in formatted
    assert validate_json(raw) == {"b": 2, "a": "你好"}
    assert validate_json("123") == 123


@pytest.mark.parametrize("bad", ["", "{", '{"a": 1,}', "NaN"])
def test_json_invalid(bad: str) -> None:
    with pytest.raises(EncodingToolError):
        format_json(bad)
    with pytest.raises(EncodingToolError):
        validate_json(bad)


def test_json_error_reports_line_and_column() -> None:
    with pytest.raises(EncodingToolError, match="第 2 行"):
        format_json('{\n"a": ,\n}')
