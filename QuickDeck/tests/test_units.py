from __future__ import annotations

import pytest

from quickdeck.builtin_actions import (
    UnitConversionError,
    convert_unit,
    format_unit_result,
)


@pytest.mark.parametrize(
    ("value", "source", "target", "expected"),
    [
        (1, "km", "m", 1000.0),
        (1000, "m", "km", 1.0),
        (1, "mile", "m", 1609.344),
        (1, "kg", "g", 1000.0),
        (1, "lb", "kg", 0.45359237),
        (1, "h", "min", 60.0),
        (1, "day", "h", 24.0),
        (1, "GB", "MB", 1024.0),
        (1, "MB", "KB", 1024.0),
        (1, "KB", "B", 1024.0),
        (1, "TB", "GB", 1024.0),
        (1, "字节", "字节", 1.0),
        (1, "公里", "米", 1000.0),
        (0, "km", "m", 0.0),
        (-1, "km", "m", -1000.0),
        ("1,024", "m", "km", 1.024),
    ],
)
def test_convert_unit_normal(value, source, target, expected) -> None:
    assert convert_unit(value, source, target) == pytest.approx(expected)


def test_storage_uses_1024() -> None:
    assert convert_unit(1, "gb", "b") == 1024 ** 3
    assert convert_unit(1, "mb", "kb") == 1024


def test_unit_category_and_format() -> None:
    assert convert_unit(1, "km", "mile", category="length") == pytest.approx(0.621371192)
    assert convert_unit(1, "km", "m", category="长度") == 1000
    assert format_unit_result(1, "km", "m") == "1000 m"
    assert format_unit_result(0.5, "h", "min") == "30 min"


@pytest.mark.parametrize(
    ("value", "source", "target", "category"),
    [
        (1, "km", "kg", None),
        (1, "km", "kg", "length"),
        (1, "unknown", "m", None),
        ("bad", "m", "km", None),
        (1, "kb", "s", None),
    ],
)
def test_convert_unit_errors(value, source, target, category) -> None:
    with pytest.raises(UnitConversionError):
        convert_unit(value, source, target, category=category)


def test_convert_unit_non_finite() -> None:
    with pytest.raises(UnitConversionError, match="有限"):
        convert_unit("inf", "m", "km")
