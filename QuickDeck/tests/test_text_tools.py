from __future__ import annotations

import pytest

from quickdeck.builtin_actions import (
    TextProcessingError,
    change_case,
    dedupe_lines,
    filter_lines,
    process_text,
    replace_text,
    sort_lines,
    trim_text,
)


def test_dedupe_lines_preserves_first_order() -> None:
    assert dedupe_lines("b\na\nb\nc\na") == "b\na\nc"
    assert dedupe_lines("A\na\nB", ignore_case=True) == "A\nB"
    assert dedupe_lines("a\n\nb\n", keep_blank=False) == "a\nb"
    assert dedupe_lines("") == ""


def test_sort_lines() -> None:
    assert sort_lines("b\nA\nc") == "A\nb\nc"
    assert sort_lines("b\nA\nc", descending=True) == "c\nb\nA"
    assert sort_lines("b\nA\nc", case_sensitive=True) == "A\nb\nc"
    assert sort_lines("10\n2\n1", numeric=True) == "1\n2\n10"
    assert sort_lines("10\n2\n1", numeric=True, descending=True) == "10\n2\n1"
    assert sort_lines("") == ""


def test_filter_lines_include_exclude_and_regex() -> None:
    text = "apple\nbanana\napricot\ncherry"
    assert filter_lines(text, "ap", mode="include") == "apple\napricot"
    assert filter_lines(text, "ap", mode="exclude") == "banana\ncherry"
    assert filter_lines(text, "AP", mode="include", ignore_case=True) == "apple\napricot"
    assert filter_lines(text, r"^(a|b)", mode="include", regex=True) == "apple\nbanana\napricot"
    assert filter_lines("A1\nb2\nC3", r"^[a-z]", mode="include", regex=True, ignore_case=True) == "A1\nb2\nC3"
    with pytest.raises(TextProcessingError, match="正则表达式无效"):
        filter_lines(text, "[", regex=True)
    with pytest.raises(TextProcessingError, match="过滤模式"):
        filter_lines(text, "x", mode="bad")


def test_replace_text() -> None:
    assert replace_text("one two one", "one", "1") == "1 two 1"
    assert replace_text("one two one", "one", "1", count=1) == "1 two one"
    assert replace_text("One two ONE", "one", "x", ignore_case=True) == "x two x"
    assert replace_text("a1b2", r"(a)(\d)", r"\2\1", regex=True) == "1ab2"
    assert replace_text("a1b2", r"(\d)", r"[\1]", regex=True) == "a[1]b[2]"
    with pytest.raises(TextProcessingError, match="不能为空"):
        replace_text("abc", "", "x")
    with pytest.raises(TextProcessingError, match="正则表达式无效"):
        replace_text("abc", "(", "x", regex=True)


def test_change_case_and_trim() -> None:
    assert change_case("Hello", "upper") == "HELLO"
    assert change_case("Hello", "lower") == "hello"
    assert change_case("hello world", "title") == "Hello World"
    assert change_case("Hello", "swap") == "hELLO"
    with pytest.raises(TextProcessingError):
        change_case("Hello", "weird")
    assert trim_text("  a  \n  b  ") == "a  \n  b"
    assert trim_text("  a  \n  b  ", each_line=True) == "a\nb"


def test_process_text_dispatch() -> None:
    assert process_text("a\nb\na", "dedupe") == "a\nb"
    assert process_text("b\na", "sort", {"descending": True}) == "b\na"
    assert process_text("abc", "case", {"mode": "upper"}) == "ABC"
    with pytest.raises(TextProcessingError, match="未知文本操作"):
        process_text("abc", "nope")
