from __future__ import annotations

import math

import pytest

from quickdeck.builtin_actions import CalculatorError, calculator, evaluate_expression


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("1 + 2 * 3", 7),
        ("(1 + 2) * 3", 9),
        ("2 ** 10", 1024),
        ("10 // 3", 3),
        ("10 % 3", 1),
        ("-2 ** 2", -4),
        ("sqrt(16) + pi", 7.141592653589793),
        ("factorial(5)", 120),
        ("abs", None),
    ],
)
def test_calculator_normal(expression: str, expected) -> None:
    if expression == "abs":
        with pytest.raises(CalculatorError):
            evaluate_expression(expression)
        return
    result = evaluate_expression(expression)
    if isinstance(expected, float):
        assert math.isclose(result, expected, rel_tol=1e-9)  # type: ignore[arg-type]
    else:
        assert result == expected


def test_calculator_formatting() -> None:
    assert calculator("1/2") == "0.5"
    assert calculator("2/1") == "2"
    assert calculator("0.1 + 0.2") == "0.3"
    assert calculator("sin(pi/2)") == "1"


@pytest.mark.parametrize("expression", ["1/0", "5 % 0", "5 // 0"])
def test_calculator_division_by_zero(expression: str) -> None:
    with pytest.raises(CalculatorError, match="除数不能为零"):
        evaluate_expression(expression)


@pytest.mark.parametrize(
    "expression",
    [
        "1 +",
        "((1)",
        "1 2 3",
        "",
        "   ",
        "1; 2",
        "x = 1",
    ],
)
def test_calculator_syntax_errors(expression: str) -> None:
    with pytest.raises(CalculatorError):
        evaluate_expression(expression)


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('dir')",
        "open('secret.txt')",
        "(1).__class__",
        "math.pi",
        "print(1)",
        "[x for x in (1,2)]",
        "lambda x: x",
        "unknown_name",
        "globals()",
    ],
)
def test_calculator_rejects_dangerous_expressions(expression: str) -> None:
    with pytest.raises(CalculatorError):
        evaluate_expression(expression)


def test_calculator_overflow_and_non_finite() -> None:
    with pytest.raises(CalculatorError, match="指数过大"):
        evaluate_expression("10 ** 10000")
    with pytest.raises(CalculatorError, match="有限数值"):
        evaluate_expression("1e999")
    with pytest.raises(CalculatorError, match="溢出"):
        evaluate_expression("exp(1000)")


def test_calculator_function_argument_error() -> None:
    with pytest.raises(CalculatorError, match="参数无效"):
        evaluate_expression("sqrt(-1)")
    with pytest.raises(CalculatorError, match="参数无效"):
        evaluate_expression("factorial(-1)")
