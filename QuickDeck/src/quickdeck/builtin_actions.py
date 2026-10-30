"""QuickDeck 内置工具的真实实现。

包含安全计算器、文本处理、编码/JSON、单位换算、时间记录、外部命令执行。
所有能力都可以脱离 GUI 在 CLI 中使用。
"""

from __future__ import annotations

import ast
import base64
import binascii
import datetime as _dt
import fnmatch
import json
import math
import operator
import os
import re
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .clipboard_service import ClipboardHistoryService
from .command_registry import Command, CommandContext, CommandRegistry


# =============================================================================
# 异常
# =============================================================================
class ToolError(RuntimeError):
    """内置工具错误的基类。"""


class CalculatorError(ToolError):
    pass


class TextProcessingError(ToolError):
    pass


class EncodingToolError(ToolError):
    pass


class UnitConversionError(ToolError):
    pass


class ExternalCommandError(ToolError):
    pass


class ConfirmationRequired(ExternalCommandError):
    """执行外部命令前没有给出明确确认。"""

    def __init__(self, message: str, preview: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.preview = preview or {}


# =============================================================================
# 计算器：ast 白名单求值，不使用 eval
# =============================================================================
_MAX_EXPRESSION_LENGTH = 1000
_MAX_INT_BITS = 4096
_MAX_EXPONENT = 1000

_ALLOWED_CONSTANTS: dict[str, float] = {
    "pi": math.pi,
    "e": math.e,
    "tau": math.tau,
}

_ALLOWED_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "sqrt": math.sqrt,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "asin": math.asin,
    "acos": math.acos,
    "atan": math.atan,
    "log": math.log,
    "log2": math.log2,
    "log10": math.log10,
    "exp": math.exp,
    "fabs": math.fabs,
    "floor": math.floor,
    "ceil": math.ceil,
    "factorial": math.factorial,
    "gcd": math.gcd,
    "degrees": math.degrees,
    "radians": math.radians,
}

_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_ALLOWED_UNARY = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _check_number(value: Any, context: str = "结果") -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CalculatorError(f"{context}不是数值")
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise CalculatorError("计算结果不是有限数值（可能溢出）")
    if isinstance(value, int) and value.bit_length() > _MAX_INT_BITS:
        raise CalculatorError("数值过大，已拒绝计算")
    return value


class _SafeEvaluator:
    def __init__(self, expression: str) -> None:
        self.expression = expression

    def evaluate(self) -> int | float:
        try:
            tree = ast.parse(self.expression, mode="eval")
        except SyntaxError as exc:
            raise CalculatorError(f"表达式语法错误：{exc.msg}") from exc
        except (MemoryError, RecursionError) as exc:  # pragma: no cover - 极端输入
            raise CalculatorError("表达式过于复杂") from exc
        result = self._eval(tree.body)
        return _check_number(result)

    def _eval(self, node: ast.AST) -> int | float:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise CalculatorError("表达式只允许数字常量")
            return _check_number(node.value, "数字常量")
        if isinstance(node, ast.Name):
            key = node.id
            if key in _ALLOWED_CONSTANTS:
                return _ALLOWED_CONSTANTS[key]
            if key in _ALLOWED_FUNCTIONS:
                raise CalculatorError(f"函数 {key} 必须被调用")
            raise CalculatorError(f"不允许使用名称：{key}")
        if isinstance(node, ast.UnaryOp):
            if type(node.op) not in _ALLOWED_UNARY:
                raise CalculatorError("不支持的一元运算符")
            value = self._eval(node.operand)
            try:
                return _check_number(_ALLOWED_UNARY[type(node.op)](value))
            except (OverflowError, ValueError) as exc:
                raise CalculatorError("一元运算溢出") from exc
        if isinstance(node, ast.BinOp):
            if type(node.op) not in _ALLOWED_BINOPS:
                raise CalculatorError("不支持的二元运算符")
            left = self._eval(node.left)
            right = self._eval(node.right)
            if isinstance(node.op, ast.Pow):
                if abs(right) > _MAX_EXPONENT:
                    raise CalculatorError("指数过大，已拒绝计算")
                if abs(left) > 1e12 and abs(right) > 64:
                    raise CalculatorError("幂运算可能产生过大数值，已拒绝")
            try:
                return _check_number(_ALLOWED_BINOPS[type(node.op)](left, right))
            except ZeroDivisionError as exc:
                raise CalculatorError("除数不能为零") from exc
            except OverflowError as exc:
                raise CalculatorError("计算溢出，结果超出允许范围") from exc
            except (ValueError, TypeError) as exc:
                raise CalculatorError(f"计算失败：{exc}") from exc
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise CalculatorError("只允许调用白名单中的数学函数，禁止属性访问")
            name = node.func.id
            func = _ALLOWED_FUNCTIONS.get(name)
            if func is None:
                raise CalculatorError(f"不允许调用函数：{name}")
            if node.keywords or any(isinstance(a, ast.Starred) for a in node.args):
                raise CalculatorError("函数调用不允许关键字参数或展开参数")
            args = [self._eval(arg) for arg in node.args]
            try:
                return _check_number(func(*args))
            except ZeroDivisionError as exc:
                raise CalculatorError("除数不能为零") from exc
            except OverflowError as exc:
                raise CalculatorError("计算溢出，结果超出允许范围") from exc
            except (ValueError, TypeError) as exc:
                raise CalculatorError(f"数学函数 {name} 参数无效：{exc}") from exc
        if isinstance(node, ast.Attribute):
            raise CalculatorError("不允许属性访问")
        raise CalculatorError(f"不支持的表达式结构：{type(node).__name__}")


def evaluate_expression(expression: str) -> int | float:
    """安全计算表达式，返回 int/float。危险表达式抛 ``CalculatorError``。"""

    if not isinstance(expression, str) or not expression.strip():
        raise CalculatorError("表达式不能为空")
    expression = expression.strip()
    if len(expression) > _MAX_EXPRESSION_LENGTH:
        raise CalculatorError("表达式过长")
    return _SafeEvaluator(expression).evaluate()


def format_number(value: int | float) -> str:
    if isinstance(value, int):
        return str(value)
    if float(value).is_integer() and abs(value) < 1e16:
        return str(int(value))
    return format(float(value), ".12g")


def calculator(expression: str) -> str:
    """计算器工具入口，返回适合显示的字符串。"""

    return format_number(evaluate_expression(expression))


# =============================================================================
# 文本处理
# =============================================================================
def dedupe_lines(text: str, *, ignore_case: bool = False, keep_blank: bool = True) -> str:
    seen: set[str] = set()
    result: list[str] = []
    for line in str(text).splitlines():
        if not line.strip() and not keep_blank:
            continue
        key = line.casefold() if ignore_case else line
        if key in seen:
            continue
        seen.add(key)
        result.append(line)
    return "\n".join(result)


def sort_lines(
    text: str,
    *,
    descending: bool = False,
    case_sensitive: bool = False,
    numeric: bool = False,
) -> str:
    lines = str(text).splitlines()
    if numeric:
        def number_key(line: str) -> tuple[int, float | str]:
            try:
                return (0, float(line.strip()))
            except ValueError:
                return (1, line.casefold())

        key = number_key
    else:
        key = (lambda line: line) if case_sensitive else (lambda line: line.casefold())
    try:
        ordered = sorted(lines, key=key, reverse=bool(descending))
    except TypeError as exc:
        raise TextProcessingError(f"排序失败：{exc}") from exc
    return "\n".join(ordered)


def filter_lines(
    text: str,
    pattern: str,
    *,
    mode: str = "include",
    regex: bool = False,
    ignore_case: bool = False,
) -> str:
    if not pattern:
        raise TextProcessingError("过滤模式不能为空")
    mode = mode.casefold()
    if mode not in {"include", "exclude"}:
        raise TextProcessingError("过滤模式必须是 include 或 exclude")
    if regex:
        flags = re.IGNORECASE if ignore_case else 0
        try:
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            raise TextProcessingError(f"正则表达式无效：{exc}") from exc
        matcher = compiled.search
    else:
        needle = pattern.casefold() if ignore_case else pattern
        matcher = lambda line: needle in (line.casefold() if ignore_case else line)
    result: list[str] = []
    for line in str(text).splitlines():
        matched = bool(matcher(line))
        if (mode == "include" and matched) or (mode == "exclude" and not matched):
            result.append(line)
    return "\n".join(result)


def replace_text(
    text: str,
    old: str,
    new: str,
    *,
    regex: bool = False,
    ignore_case: bool = False,
    count: int = 0,
) -> str:
    if not old and not regex:
        raise TextProcessingError("被替换内容不能为空")
    if count < 0:
        raise TextProcessingError("替换次数不能为负数")
    if regex:
        flags = re.IGNORECASE if ignore_case else 0
        try:
            compiled = re.compile(old, flags)
        except re.error as exc:
            raise TextProcessingError(f"正则表达式无效：{exc}") from exc
        return compiled.sub(new, str(text), count=count)
    if ignore_case:
        try:
            compiled = re.compile(re.escape(old), re.IGNORECASE)
        except re.error as exc:  # pragma: no cover - 理论上不会发生
            raise TextProcessingError(str(exc)) from exc
        return compiled.sub(lambda _m: new, str(text), count=count)
    return str(text).replace(old, new, count if count else -1)


def change_case(text: str, mode: str = "upper") -> str:
    mode = str(mode).casefold()
    mapping = {
        "upper": str.upper,
        "lower": str.lower,
        "title": str.title,
        "capitalize": str.capitalize,
        "swap": str.swapcase,
        "swapcase": str.swapcase,
    }
    func = mapping.get(mode)
    if func is None:
        raise TextProcessingError("大小写模式必须是 upper/lower/title/capitalize/swap")
    return func(str(text))


def trim_text(text: str, *, each_line: bool = False) -> str:
    if each_line:
        return "\n".join(line.strip() for line in str(text).splitlines())
    return str(text).strip()


def process_text(text: str, operation: str, params: Mapping[str, Any] | None = None) -> str:
    """统一文本处理入口，方便 workflow 与 CLI 调用。"""

    params = dict(params or {})
    op = str(operation).casefold()
    if op in {"dedupe", "去重"}:
        return dedupe_lines(
            text,
            ignore_case=bool(params.get("ignore_case", False)),
            keep_blank=bool(params.get("keep_blank", True)),
        )
    if op in {"sort", "排序"}:
        return sort_lines(
            text,
            descending=bool(params.get("descending", False)),
            case_sensitive=bool(params.get("case_sensitive", False)),
            numeric=bool(params.get("numeric", False)),
        )
    if op in {"filter", "过滤"}:
        return filter_lines(
            text,
            str(params.get("pattern", "")),
            mode=str(params.get("mode", "include")),
            regex=bool(params.get("regex", False)),
            ignore_case=bool(params.get("ignore_case", False)),
        )
    if op in {"replace", "替换"}:
        return replace_text(
            text,
            str(params.get("old", "")),
            str(params.get("new", "")),
            regex=bool(params.get("regex", False)),
            ignore_case=bool(params.get("ignore_case", False)),
            count=int(params.get("count", 0)),
        )
    if op in {"case", "大小写"}:
        return change_case(text, str(params.get("mode", "upper")))
    if op in {"trim", "修剪"}:
        return trim_text(text, each_line=bool(params.get("each_line", False)))
    raise TextProcessingError(f"未知文本操作：{operation}")


# =============================================================================
# 编码 / JSON
# =============================================================================
def url_encode(text: str) -> str:
    return urllib.parse.quote(str(text), safe="")


def url_decode(text: str) -> str:
    try:
        return urllib.parse.unquote(str(text))
    except Exception as exc:  # pragma: no cover - unquote 很宽容
        raise EncodingToolError(f"URL 解码失败：{exc}") from exc


def base64_encode(text: str) -> str:
    return base64.b64encode(str(text).encode("utf-8")).decode("ascii")


def base64_decode(text: str) -> str:
    raw = re.sub(r"\s+", "", str(text))
    if not raw:
        raise EncodingToolError("Base64 内容不能为空")
    try:
        if len(raw) % 4:
            raw += "=" * (4 - len(raw) % 4)
        data = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise EncodingToolError("Base64 内容无效") from exc
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EncodingToolError("Base64 解码结果不是有效的 UTF-8 文本") from exc


_UNICODE_ESCAPE_RE = re.compile(
    r"\\(u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|x[0-9a-fA-F]{2}|n|r|t|\\|')"
)


def unicode_escape(text: str) -> str:
    return str(text).encode("unicode_escape").decode("ascii")


def _unicode_replacer(match: re.Match[str]) -> str:
    token = match.group(1)
    simple = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", "'": "'"}
    if token in simple:
        return simple[token]
    return chr(int(token[1:], 16))


def unicode_unescape(text: str) -> str:
    try:
        return _UNICODE_ESCAPE_RE.sub(_unicode_replacer, str(text))
    except (ValueError, OverflowError) as exc:
        raise EncodingToolError(f"Unicode 转义内容无效：{exc}") from exc


def _reject_json_constant(value: str) -> Any:
    raise EncodingToolError(f"JSON 标准不允许特殊数值：{value}")


def format_json(text: str, *, sort_keys: bool = False, indent: int = 2) -> str:
    if not str(text).strip():
        raise EncodingToolError("JSON 内容不能为空")
    try:
        value = json.loads(text, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as exc:
        raise EncodingToolError(
            f"JSON 语法错误（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}"
        ) from exc
    try:
        return json.dumps(value, ensure_ascii=False, indent=max(0, int(indent)), sort_keys=sort_keys)
    except (TypeError, ValueError) as exc:  # pragma: no cover - json.loads 已限制类型
        raise EncodingToolError(f"JSON 格式化失败：{exc}") from exc


def validate_json(text: str) -> Any:
    if not str(text).strip():
        raise EncodingToolError("JSON 内容不能为空")
    try:
        return json.loads(text, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as exc:
        raise EncodingToolError(
            f"JSON 语法错误（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}"
        ) from exc


# 兼容直观命名
json_format = format_json
json_validate = validate_json


# =============================================================================
# 单位换算
# =============================================================================
_UNIT_FACTORS: dict[str, dict[str, float]] = {
    "length": {
        "m": 1.0,
        "km": 1000.0,
        "cm": 0.01,
        "mm": 0.001,
        "um": 1e-6,
        "mile": 1609.344,
        "mi": 1609.344,
        "ft": 0.3048,
        "foot": 0.3048,
        "inch": 0.0254,
        "in": 0.0254,
        "yard": 0.9144,
        "yd": 0.9144,
        "米": 1.0,
        "千米": 1000.0,
        "公里": 1000.0,
        "厘米": 0.01,
        "毫米": 0.001,
        "英里": 1609.344,
        "英尺": 0.3048,
        "英寸": 0.0254,
        "码": 0.9144,
    },
    "mass": {
        "kg": 1.0,
        "g": 0.001,
        "mg": 1e-6,
        "t": 1000.0,
        "ton": 1000.0,
        "lb": 0.45359237,
        "oz": 0.028349523125,
        "千克": 1.0,
        "公斤": 1.0,
        "克": 0.001,
        "毫克": 1e-6,
        "吨": 1000.0,
        "磅": 0.45359237,
        "盎司": 0.028349523125,
    },
    "time": {
        "s": 1.0,
        "sec": 1.0,
        "ms": 0.001,
        "min": 60.0,
        "h": 3600.0,
        "hour": 3600.0,
        "day": 86400.0,
        "d": 86400.0,
        "week": 604800.0,
        "秒": 1.0,
        "毫秒": 0.001,
        "分钟": 60.0,
        "小时": 3600.0,
        "天": 86400.0,
        "日": 86400.0,
        "周": 604800.0,
    },
    "storage": {
        "b": 1.0,
        "byte": 1.0,
        "kb": 1024.0,
        "kib": 1024.0,
        "mb": 1024.0 ** 2,
        "mib": 1024.0 ** 2,
        "gb": 1024.0 ** 3,
        "gib": 1024.0 ** 3,
        "tb": 1024.0 ** 4,
        "tib": 1024.0 ** 4,
        "pb": 1024.0 ** 5,
        "bit": 1.0 / 8.0,
        "bits": 1.0 / 8.0,
        "字节": 1.0,
        "比特": 1.0 / 8.0,
        "千字节": 1024.0,
        "兆字节": 1024.0 ** 2,
        "吉字节": 1024.0 ** 3,
        "太字节": 1024.0 ** 4,
    },
}

_UNIT_CATEGORY_LABELS = {
    "length": "长度",
    "mass": "质量",
    "time": "时间",
    "storage": "存储",
}


def _normalize_unit(unit: str) -> str:
    return str(unit).strip().casefold()


def unit_categories() -> list[str]:
    return list(_UNIT_FACTORS)


def conversion_categories() -> dict[str, str]:
    return dict(_UNIT_CATEGORY_LABELS)


def _find_category(from_unit: str, to_unit: str, category: str | None) -> str:
    from_n = _normalize_unit(from_unit)
    to_n = _normalize_unit(to_unit)
    if category:
        key = category.casefold()
        # 允许中文分类名
        for name, label in _UNIT_CATEGORY_LABELS.items():
            if key == name or key == label:
                key = name
                break
        if key not in _UNIT_FACTORS:
            raise UnitConversionError(f"未知单位类别：{category}")
        if from_n not in _UNIT_FACTORS[key] or to_n not in _UNIT_FACTORS[key]:
            raise UnitConversionError(f"单位不属于{_UNIT_CATEGORY_LABELS[key]}类别")
        return key
    matches = [
        key
        for key, factors in _UNIT_FACTORS.items()
        if from_n in factors and to_n in factors
    ]
    if not matches:
        raise UnitConversionError(f"无法识别单位换算：{from_unit} → {to_unit}")
    return matches[0]


def convert_unit(
    value: int | float | str,
    from_unit: str,
    to_unit: str,
    *,
    category: str | None = None,
) -> float:
    try:
        numeric = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError) as exc:
        raise UnitConversionError(f"数值无效：{value}") from exc
    if math.isnan(numeric) or math.isinf(numeric):
        raise UnitConversionError("数值必须是有限数")
    key = _find_category(from_unit, to_unit, category)
    from_n = _normalize_unit(from_unit)
    to_n = _normalize_unit(to_unit)
    result = numeric * _UNIT_FACTORS[key][from_n] / _UNIT_FACTORS[key][to_n]
    if math.isnan(result) or math.isinf(result):
        raise UnitConversionError("换算结果超出范围")
    return result


def format_unit_result(
    value: int | float | str,
    from_unit: str,
    to_unit: str,
    *,
    category: str | None = None,
) -> str:
    result = convert_unit(value, from_unit, to_unit, category=category)
    return f"{format_number(result)} {to_unit}"


# =============================================================================
# 时间记录
# =============================================================================
@dataclass
class ActivityRecord:
    id: int
    name: str
    recorded_at: str
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "recorded_at": self.recorded_at,
            "note": self.note,
        }


class ActivityStore:
    """把自定义活动名与时间写入 SQLite。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS activity_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_activity_name
                    ON activity_records(name);
                CREATE INDEX IF NOT EXISTS idx_activity_recorded_at
                    ON activity_records(recorded_at DESC);
                """
            )
            self._conn.commit()

    def record(
        self,
        name: str,
        note: str = "",
        *,
        now: _dt.datetime | None = None,
    ) -> ActivityRecord:
        activity = str(name).strip()
        if not activity:
            raise ToolError("活动名称不能为空")
        timestamp = now or _dt.datetime.now().astimezone()
        recorded_at = timestamp.isoformat(timespec="seconds")
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO activity_records(name, recorded_at, note, created_at) "
                "VALUES (?, ?, ?, ?)",
                (activity, recorded_at, str(note or ""), timestamp.timestamp()),
            )
            self._conn.commit()
            return ActivityRecord(
                id=int(cursor.lastrowid),
                name=activity,
                recorded_at=recorded_at,
                note=str(note or ""),
            )

    def last(self, name: str | None = None) -> ActivityRecord | None:
        records = self.history(name=name, limit=1)
        return records[0] if records else None

    def history(self, name: str | None = None, limit: int = 10) -> list[ActivityRecord]:
        if limit < 0:
            raise ValueError("limit 不能为负数")
        sql = "SELECT id, name, recorded_at, note FROM activity_records"
        args: list[Any] = []
        if name:
            sql += " WHERE name = ?"
            args.append(str(name).strip())
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        args.append(int(limit))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [
            ActivityRecord(
                id=int(row["id"]),
                name=row["name"],
                recorded_at=row["recorded_at"],
                note=row["note"],
            )
            for row in rows
        ]

    def names(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT name FROM activity_records ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [row["name"] for row in rows]

    def clear(self, name: str | None = None) -> int:
        with self._lock:
            if name:
                cur = self._conn.execute("DELETE FROM activity_records WHERE name = ?", (name,))
            else:
                cur = self._conn.execute("DELETE FROM activity_records")
            self._conn.commit()
            return int(cur.rowcount or 0)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# =============================================================================
# 外部命令执行
# =============================================================================
@dataclass
class ExternalCommandResult:
    ok: bool
    argv: list[str]
    stdout: str = ""
    stderr: str = ""
    returncode: int | None = None
    timed_out: bool = False
    error: str = ""

    def as_text(self) -> str:
        if self.ok:
            text = self.stdout.rstrip("\r\n")
            if self.stderr.strip():
                text += f"\n[stderr]\n{self.stderr.rstrip()}"
            return text or "(命令执行成功，无输出)"
        if self.timed_out:
            return f"命令执行超时：{self.error}"
        detail = self.error or self.stderr.strip() or "未知错误"
        suffix = f"（退出码 {self.returncode}）" if self.returncode is not None else ""
        return f"命令执行失败{suffix}：{detail}"


@dataclass
class ExternalCommandSpec:
    argv: list[str]
    name: str = ""
    cwd: str | None = None
    timeout: float = 10.0
    description: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.argv, (list, tuple)):
            raise ExternalCommandError("命令必须使用参数数组，禁止 shell 字符串")
        if not self.argv:
            raise ExternalCommandError("命令参数不能为空")
        clean = []
        for item in self.argv:
            if not isinstance(item, str):
                raise ExternalCommandError("命令参数必须是字符串")
            clean.append(item)
        self.argv = clean
        if not str(self.argv[0]).strip():
            raise ExternalCommandError("可执行文件名不能为空")
        if self.timeout <= 0:
            raise ExternalCommandError("超时时间必须大于 0")
        if self.cwd:
            cwd_path = Path(self.cwd).expanduser()
            if not cwd_path.is_dir():
                raise ExternalCommandError(f"工作目录不存在：{self.cwd}")
            self.cwd = str(cwd_path)

    def preview(self) -> dict[str, Any]:
        return {
            "name": self.name or self.argv[0],
            "argv": list(self.argv),
            "cwd": self.cwd or os.getcwd(),
            "timeout": self.timeout,
            "description": self.description,
            "安全说明": "以参数数组执行，不经过 shell；真正执行前需要 confirm=True。",
        }


class ExternalCommandRunner:
    """在确认后执行预配置命令，不经过 shell。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def preview(self, spec: ExternalCommandSpec | Mapping[str, Any] | Sequence[str]) -> dict[str, Any]:
        return self._normalize(spec).preview()

    def execute(
        self,
        spec: ExternalCommandSpec | Mapping[str, Any] | Sequence[str],
        *,
        confirm: bool = False,
        timeout: float | None = None,
    ) -> ExternalCommandResult:
        normalized = self._normalize(spec)
        if timeout is not None:
            if timeout <= 0:
                raise ExternalCommandError("超时时间必须大于 0")
            normalized.timeout = float(timeout)
        if confirm is not True:
            raise ConfirmationRequired(
                "执行外部命令前必须展示动作信息并明确确认（confirm=True）。",
                normalized.preview(),
            )
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with self._lock:
            try:
                completed = subprocess.run(
                    normalized.argv,
                    cwd=normalized.cwd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=normalized.timeout,
                    shell=False,
                    creationflags=creationflags,
                )
            except subprocess.TimeoutExpired:
                return ExternalCommandResult(
                    ok=False,
                    argv=list(normalized.argv),
                    timed_out=True,
                    error=f"超过 {normalized.timeout:g} 秒",
                )
            except FileNotFoundError as exc:
                return ExternalCommandResult(
                    ok=False,
                    argv=list(normalized.argv),
                    error=f"找不到可执行文件：{exc.filename or normalized.argv[0]}",
                )
            except OSError as exc:
                return ExternalCommandResult(
                    ok=False,
                    argv=list(normalized.argv),
                    error=f"无法启动命令：{exc}",
                )
        return ExternalCommandResult(
            ok=completed.returncode == 0,
            argv=list(normalized.argv),
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
            error="" if completed.returncode == 0 else "进程返回非零退出码",
        )

    @staticmethod
    def _normalize(
        spec: ExternalCommandSpec | Mapping[str, Any] | Sequence[str],
    ) -> ExternalCommandSpec:
        if isinstance(spec, ExternalCommandSpec):
            return spec
        if isinstance(spec, str):
            raise ExternalCommandError("命令必须使用参数数组，禁止 shell 字符串")
        if isinstance(spec, Mapping):
            if "argv" in spec:
                argv = spec["argv"]
            elif "command" in spec:
                argv = spec["command"]
            else:
                raise ExternalCommandError("命令配置缺少 argv 参数数组")
            if isinstance(argv, str):
                raise ExternalCommandError("argv 必须是参数数组，不能是字符串")
            return ExternalCommandSpec(
                argv=list(argv),
                name=str(spec.get("name", "")),
                cwd=str(spec["cwd"]) if spec.get("cwd") else None,
                timeout=float(spec.get("timeout", 10.0)),
                description=str(spec.get("description", "")),
            )
        if isinstance(spec, (list, tuple)):
            return ExternalCommandSpec(argv=list(spec))
        raise ExternalCommandError("不支持的命令配置类型")


# =============================================================================
# 内置工具集合与命令注册
# =============================================================================
class BuiltinToolkit:
    """把内置工具组装成可注册的命令集合。"""

    def __init__(
        self,
        db_path: str | Path,
        *,
        data_dir: str | Path | None = None,
        clipboard_enabled: bool = False,
        clipboard_max_items: int = 50,
        clipboard_exclude: Sequence[str] | None = None,
        clipboard_backend: Any = None,
        clipboard_db_path: str | Path | None = None,
    ) -> None:
        self.db_path = str(db_path)
        self.data_dir = Path(data_dir) if data_dir else Path(self.db_path).parent
        self.activities = ActivityStore(self.db_path)
        self.external_runner = ExternalCommandRunner()
        if clipboard_db_path is None:
            clipboard_db_path = self.db_path
        self.clipboard = ClipboardHistoryService(
            backend=clipboard_backend,
            enabled=clipboard_enabled,
            max_items=clipboard_max_items,
            exclude_patterns=clipboard_exclude or [],
            db_path=clipboard_db_path,
        )

    # ------------------------------------------------------------------ 命令
    def command_definitions(self) -> list[Command]:
        return [
            Command("calc.eval", "计算器", ["calc", "计算", "="], "数学",
                    ["calculator", "math", "表达式", "算"], "安全计算数学表达式"),
            Command("text.dedupe", "文本去重", ["dedupe", "去重"], "文本",
                    ["unique", "行", "重复"], "按行去重，保留首次出现"),
            Command("text.sort", "文本排序", ["sort", "排序"], "文本",
                    ["order", "行", "升序", "降序"], "按行排序，可降序"),
            Command("text.filter", "行过滤", ["filter", "过滤"], "文本",
                    ["grep", "包含", "排除", "正则"], "按包含/排除/正则过滤行"),
            Command("text.replace", "批量替换", ["replace", "替换"], "文本",
                    ["regex", "替换", "文本"], "批量替换文本，支持正则"),
            Command("text.case", "大小写转换", ["case", "大小写"], "文本",
                    ["upper", "lower", "case"], "转换文本大小写"),
            Command("text.trim", "文本修剪", ["trim", "修剪"], "文本",
                    ["strip", "空白", "缩进"], "去除首尾空白或每行空白"),
            Command("encoding.url_encode", "URL 编码", ["urlencode", "url编码"], "编码",
                    ["percent", "quote", "url"], "URL 百分号编码"),
            Command("encoding.url_decode", "URL 解码", ["urldecode", "url解码"], "编码",
                    ["unquote", "percent", "url"], "URL 百分号解码"),
            Command("encoding.base64_encode", "Base64 编码", ["b64encode", "base64编码"], "编码",
                    ["base64", "encode"], "Base64 编码"),
            Command("encoding.base64_decode", "Base64 解码", ["b64decode", "base64解码"], "编码",
                    ["base64", "decode"], "Base64 解码"),
            Command("encoding.unicode_escape", "Unicode 转义", ["uescape", "unicode转义"], "编码",
                    ["unicode", "escape"], "把文本转为 Unicode 转义"),
            Command("encoding.unicode_unescape", "Unicode 反转义", ["uunescape", "unicode反转义"], "编码",
                    ["unicode", "unescape"], "还原 Unicode 转义文本"),
            Command("encoding.json_format", "JSON 格式化", ["json", "json格式化"], "编码",
                    ["json", "format", "pretty"], "格式化和校验 JSON"),
            Command("encoding.json_validate", "JSON 校验", ["json校验", "validate-json"], "编码",
                    ["json", "validate"], "校验 JSON 并返回摘要"),
            Command("unit.convert", "单位换算", ["convert", "换算", "单位"], "换算",
                    ["unit", "length", "mass", "storage", "time"], "长度/质量/时间/存储换算"),
            Command("time.record", "记录时间", ["time", "记录时间", "打卡"], "时间",
                    ["activity", "log", "记录"], "记录当前时间到 SQLite"),
            Command("time.history", "时间记录历史", ["history", "时间记录"], "时间",
                    ["activity", "log", "历史"], "查看最近的时间记录"),
            Command("command.run", "执行外部命令", ["exec", "执行命令"], "系统",
                    ["command", "subprocess", "进程"], "执行预配置命令，执行前必须确认"),
            Command("clipboard.history", "剪贴板历史", ["clip", "剪贴板"], "剪贴板",
                    ["clipboard", "history", "历史"], "查看剪贴板历史（需启用）"),
            Command("clipboard.clear", "清空剪贴板历史", ["clip-clear", "清空剪贴板"], "剪贴板",
                    ["clipboard", "clear", "清空"], "清空剪贴板历史"),
        ]

    def register_commands(self, registry: CommandRegistry, *, source: str = "builtin") -> int:
        definitions = self.command_definitions()
        for definition in definitions:
            definition.source = source
            registry.register(definition, self._handler_for(definition.id))
        return len(definitions)

    def _handler_for(self, command_id: str) -> Callable[[CommandContext], Any]:
        def handler(ctx: CommandContext) -> str:
            return self.execute_builtin(command_id, ctx.text, ctx.params)

        return handler

    def execute_builtin(
        self,
        command_id: str,
        text: str = "",
        params: Mapping[str, Any] | None = None,
    ) -> str:
        params = dict(params or {})
        if command_id == "calc.eval":
            return calculator(text)
        if command_id == "text.dedupe":
            return dedupe_lines(
                text,
                ignore_case=bool(params.get("ignore_case", False)),
                keep_blank=bool(params.get("keep_blank", True)),
            )
        if command_id == "text.sort":
            return sort_lines(
                text,
                descending=bool(params.get("descending", False)),
                case_sensitive=bool(params.get("case_sensitive", False)),
                numeric=bool(params.get("numeric", False)),
            )
        if command_id == "text.filter":
            return filter_lines(
                text,
                str(params.get("pattern", "")),
                mode=str(params.get("mode", "include")),
                regex=bool(params.get("regex", False)),
                ignore_case=bool(params.get("ignore_case", False)),
            )
        if command_id == "text.replace":
            return replace_text(
                text,
                str(params.get("old", "")),
                str(params.get("new", "")),
                regex=bool(params.get("regex", False)),
                ignore_case=bool(params.get("ignore_case", False)),
                count=int(params.get("count", 0)),
            )
        if command_id == "text.case":
            return change_case(text, str(params.get("mode", "upper")))
        if command_id == "text.trim":
            return trim_text(text, each_line=bool(params.get("each_line", False)))
        if command_id == "encoding.url_encode":
            return url_encode(text)
        if command_id == "encoding.url_decode":
            return url_decode(text)
        if command_id == "encoding.base64_encode":
            return base64_encode(text)
        if command_id == "encoding.base64_decode":
            return base64_decode(text)
        if command_id == "encoding.unicode_escape":
            return unicode_escape(text)
        if command_id == "encoding.unicode_unescape":
            return unicode_unescape(text)
        if command_id == "encoding.json_format":
            return format_json(
                text,
                sort_keys=bool(params.get("sort_keys", False)),
                indent=int(params.get("indent", 2)),
            )
        if command_id == "encoding.json_validate":
            value = validate_json(text)
            kind = type(value).__name__
            preview = json.dumps(value, ensure_ascii=False)[:120]
            return f"JSON 有效（{kind}）：{preview}"
        if command_id == "unit.convert":
            value = params.get("value", text)
            from_unit = str(params.get("from_unit", params.get("from", "")))
            to_unit = str(params.get("to_unit", params.get("to", "")))
            if not from_unit or not to_unit:
                raise UnitConversionError("单位换算需要 from_unit 与 to_unit 参数")
            return format_unit_result(
                value,
                from_unit,
                to_unit,
                category=params.get("category"),
            )
        if command_id == "time.record":
            activity = str(params.get("activity", text)).strip()
            record = self.activities.record(activity, str(params.get("note", "")))
            return f"已记录 {record.name}：{record.recorded_at}"
        if command_id == "time.history":
            activity = params.get("activity") or None
            records = self.activities.history(name=activity, limit=int(params.get("limit", 10)))
            if not records:
                return "暂无时间记录"
            return "\n".join(
                f"{record.recorded_at}  {record.name}" + (f"  [{record.note}]" if record.note else "")
                for record in records
            )
        if command_id == "command.run":
            spec = self._spec_from_params(params)
            result = self.external_runner.execute(spec, confirm=params.get("confirm") is True)
            return result.as_text()
        if command_id == "clipboard.history":
            if not self.clipboard.enabled:
                return "剪贴板历史未启用（默认关闭）"
            entries = self.clipboard.history()
            if not entries:
                return "剪贴板历史为空"
            return "\n".join(f"{entry.captured_at}  {entry.text}" for entry in entries)
        if command_id == "clipboard.clear":
            count = self.clipboard.clear()
            return f"已清空 {count} 条剪贴板历史"
        raise ToolError(f"未知内置命令：{command_id}")

    def _spec_from_params(self, params: Mapping[str, Any]) -> ExternalCommandSpec | Mapping[str, Any]:
        if "spec" in params:
            return params["spec"]
        if "argv" in params:
            return params
        if "command" in params:
            return params
        raise ExternalCommandError("外部命令需要 argv 参数数组与 confirm=True")

    def preview_external(
        self,
        spec: ExternalCommandSpec | Mapping[str, Any] | Sequence[str],
    ) -> dict[str, Any]:
        return self.external_runner.preview(spec)

    def execute_external(
        self,
        spec: ExternalCommandSpec | Mapping[str, Any] | Sequence[str],
        *,
        confirm: bool = False,
        timeout: float | None = None,
    ) -> ExternalCommandResult:
        return self.external_runner.execute(spec, confirm=confirm, timeout=timeout)

    def close(self) -> None:
        try:
            self.clipboard.close()
        finally:
            self.activities.close()
