"""环境变量与导出内容的敏感信息处理。

默认策略是“宁可多脱敏”：变量名中出现 TOKEN、SECRET、PASSWORD、KEY、
CREDENTIAL、AUTH 等片段时，值一律替换为 ``***``。调用方可以显式传入
``allow_plain_keys`` 覆盖该策略，但该能力只应在用户明确确认后使用。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

REDACTED = "***"

_SENSITIVE_KEY_RE = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE[_-]?KEY|API[_-]?KEY|APIKEY|"
    r"CREDENTIAL|AUTH|_KEY$|^KEY$|KEY$)",
    re.IGNORECASE,
)

# 日志/文本导出前的启发式敏感内容检测。只报告位置的键名或模式，
# 不会把命中的完整值拼进报告。
_CONTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "key-value secret",
        re.compile(
            r"(?i)[A-Za-z0-9_.-]*(?:token|secret|password|passwd|api[_-]?key|"
            r"private[_-]?key|credential)[A-Za-z0-9_.-]*[ \t]*[:=][ \t]*([^\s\"']{6,})"
        ),
    ),
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("GitLab token", re.compile(r"glpat-[A-Za-z0-9_-]{16,}")),
    ("AWS access key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("OpenAI-like key", re.compile(r"sk-[A-Za-z0-9_-]{20,}")),
    ("private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
)


def is_sensitive_key(name: str) -> bool:
    """判断变量名是否应按默认策略脱敏。"""
    return bool(_SENSITIVE_KEY_RE.search(str(name)))


def mask_value(value: object) -> str:
    return REDACTED if value is None or str(value) == "" else REDACTED


def redact_env(
    env: Mapping[str, object] | None,
    *,
    extra_sensitive_keys: Iterable[str] | None = None,
    allow_plain_keys: Iterable[str] | None = None,
) -> dict[str, str]:
    """返回脱敏后的环境变量副本。

    ``extra_sensitive_keys`` 可额外指定必须脱敏的键；
    ``allow_plain_keys`` 可显式允许某些原本会被脱敏的键保持原值。
    键名大小写按 Windows 习惯进行比较。
    """
    forced = {str(k).upper() for k in (extra_sensitive_keys or ())}
    allowed = {str(k).upper() for k in (allow_plain_keys or ())}
    result: dict[str, str] = {}
    for raw_key, raw_value in (env or {}).items():
        key = str(raw_key)
        upper = key.upper()
        if upper in allowed:
            result[key] = "" if raw_value is None else str(raw_value)
        elif upper in forced or is_sensitive_key(key):
            result[key] = REDACTED
        else:
            result[key] = "" if raw_value is None else str(raw_value)
    return result


def redacted_key_names(
    env: Mapping[str, object] | None,
    *,
    extra_sensitive_keys: Iterable[str] | None = None,
) -> list[str]:
    """列出被默认/显式规则脱敏的键名（不返回值）。"""
    forced = {str(k).upper() for k in (extra_sensitive_keys or ())}
    return sorted(
        str(k)
        for k in (env or {})
        if str(k).upper() in forced or is_sensitive_key(str(k))
    )


def scan_sensitive_content(text: str, *, context: str = "") -> list[dict[str, str]]:
    """扫描文本中的疑似敏感信息，返回值已被遮蔽的发现列表。"""
    findings: list[dict[str, str]] = []
    for label, pattern in _CONTENT_PATTERNS:
        for match in pattern.finditer(text or ""):
            if match.groups():
                raw = match.group(1)
                preview = raw[:4] + "...[已遮蔽]"
            else:
                preview = "[已遮蔽]"
            findings.append(
                {
                    "type": label,
                    "context": context,
                    "preview": preview,
                    "offset": str(match.start()),
                }
            )
            # 每个模式最多报告 10 处，避免导出报告本身膨胀。
            if sum(1 for item in findings if item["type"] == label) >= 10:
                break
    return findings


def redact_text_secrets(text: str) -> str:
    """将文本中的高置信度密钥替换为 ``***``，用于必须脱敏的展示场景。"""
    redacted = text or ""
    for _, pattern in _CONTENT_PATTERNS:
        if pattern.groups:
            redacted = pattern.sub(lambda m: m.group(0).replace(m.group(1), REDACTED), redacted)
        else:
            redacted = pattern.sub(REDACTED, redacted)
    return redacted
