from __future__ import annotations

import sys

import pytest

from quickdeck.hotkey_manager import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    MOD_WIN,
    HotkeyManager,
    HotkeyParseError,
    HotkeyRegistrationError,
    HotkeySpec,
    HotkeyUnavailable,
    parse_hotkey,
)


def test_parse_ctrl_alt_space() -> None:
    spec = parse_hotkey("Ctrl+Alt+Space")
    assert isinstance(spec, HotkeySpec)
    assert spec.modifiers == MOD_CONTROL | MOD_ALT
    assert spec.vk == 0x20
    assert spec.key == "Space"
    assert spec.display == "Ctrl+Alt+Space"
    assert spec.as_tuple() == (MOD_CONTROL | MOD_ALT, 0x20)


@pytest.mark.parametrize(
    ("raw", "display"),
    [
        ("ctrl+alt+space", "Ctrl+Alt+Space"),
        ("Shift+Ctrl+F8", "Ctrl+Shift+F8"),
        ("Win+Shift+A", "Shift+Win+A"),
        ("alt+f12", "Alt+F12"),
        ("Ctrl+Enter", "Ctrl+Enter"),
        ("Control+PageUp", "Ctrl+PageUp"),
        ("Ctrl+Escape", "Ctrl+Esc"),
        ("Ctrl+Alt+1", "Ctrl+Alt+1"),
    ],
)
def test_parse_normalizes_modifier_order(raw: str, display: str) -> None:
    assert parse_hotkey(raw).display == display


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "Ctrl",
        "Ctrl+Alt",
        "Ctrl+BadKey",
        "Ctrl+Alt+Space+F8",
        "Ctrl+Ctrl+Space",
        "Ctrl++Space",
        "+Space",
        "Ctrl+",
    ],
)
def test_parse_rejects_invalid(raw: str) -> None:
    with pytest.raises(HotkeyParseError):
        parse_hotkey(raw)


def test_key_virtual_codes() -> None:
    spec = parse_hotkey("Ctrl+Alt+F12")
    assert spec.vk == 0x7B
    spec = parse_hotkey("Win+Shift+Escape")
    assert spec.modifiers == MOD_WIN | MOD_SHIFT
    assert spec.vk == 0x1B


def test_non_windows_registration_is_explicitly_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    manager = HotkeyManager()
    with pytest.raises(HotkeyUnavailable, match="Windows"):
        manager.register("Ctrl+Alt+Space", lambda: None)
    assert manager.is_registered is False


def test_real_registration_or_skip() -> None:
    if sys.platform != "win32":
        pytest.skip("真实全局快捷键仅 Windows 可测试")
    manager = HotkeyManager()
    try:
        manager.register("Ctrl+Alt+F11", lambda: None)
    except HotkeyRegistrationError as exc:
        pytest.skip(f"当前桌面/系统无法注册测试热键：{exc}")
    try:
        assert manager.is_registered
        assert manager.spec is not None
        assert manager.spec.display == "Ctrl+Alt+F11"
    finally:
        manager.unregister()
    assert manager.is_registered is False
