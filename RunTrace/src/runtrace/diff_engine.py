"""两次运行的日志行差异和元数据事实对比。

输出严格区分“事实”（从记录中直接读到）与“推测”（基于差异的可能解释）。
"""

from __future__ import annotations

import difflib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class LineChange:
    kind: str  # changed / added / removed
    old_line_no: int | None
    new_line_no: int | None
    old_text: str | None
    new_text: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "old_line_no": self.old_line_no,
            "new_line_no": self.new_line_no,
            "old_text": self.old_text,
            "new_text": self.new_text,
        }


@dataclass(slots=True)
class LogDiff:
    old_label: str
    new_label: str
    changes: list[LineChange] = field(default_factory=list)
    added: int = 0
    removed: int = 0
    changed: int = 0
    identical: bool = True
    old_text: str = ""
    new_text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_label": self.old_label,
            "new_label": self.new_label,
            "identical": self.identical,
            "counts": {
                "added": self.added,
                "removed": self.removed,
                "changed": self.changed,
            },
            "changes": [change.to_dict() for change in self.changes],
        }


@dataclass(slots=True)
class ComparisonReport:
    facts: list[dict[str, Any]]
    speculations: list[str]
    log_diffs: dict[str, LogDiff]
    legend: str = "facts 为记录中的直接事实；speculations 仅为推测，需人工验证。"

    def to_dict(self) -> dict[str, Any]:
        return {
            "legend": self.legend,
            "facts": self.facts,
            "speculations": list(self.speculations),
            "log_diffs": {name: diff.to_dict() for name, diff in self.log_diffs.items()},
        }

    def render_text(self, *, include_unified: bool = True) -> str:
        lines = ["RunTrace 运行对比", "=" * 60, self.legend, "", "【事实】"]
        for fact in self.facts:
            if fact.get("changed"):
                lines.append(
                    f"- {fact['field']}: {_display(fact.get('old'))} -> {_display(fact.get('new'))} （有变化）"
                )
            else:
                lines.append(f"- {fact['field']}: {_display(fact.get('new'))} （未变化）")
        lines.extend(["", "【推测（非事实）】"])
        if self.speculations:
            lines.extend(f"- {text}" for text in self.speculations)
        else:
            lines.append("- 暂无可生成的推测")
        lines.extend(["", "【日志差异】"])
        for name, log_diff in self.log_diffs.items():
            lines.append(
                f"{name}: 新增 {log_diff.added}，删除 {log_diff.removed}，变化 {log_diff.changed}"
            )
            if log_diff.identical:
                lines.append("  （无差异）")
        if include_unified:
            for name, log_diff in self.log_diffs.items():
                if log_diff.identical:
                    continue
                lines.extend(["", f"--- unified diff: {name} ---"])
                lines.append(
                    render_unified(
                        _log_text_from_arg(log_diff, "old"),
                        _log_text_from_arg(log_diff, "new"),
                        f"{log_diff.old_label}:{name}",
                        f"{log_diff.new_label}:{name}",
                    )
                )
        return "\n".join(lines)


def _display(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _log_text_from_arg(log_diff: LogDiff, which: str) -> str:
    return log_diff.old_text if which == "old" else log_diff.new_text


def diff_lines(
    old_text: str,
    new_text: str,
    *,
    old_label: str = "旧",
    new_label: str = "新",
) -> LogDiff:
    """按行比较两段文本，产生 changed/added/removed 条目。"""
    old_lines = (old_text or "").splitlines()
    new_lines = (new_text or "").splitlines()
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    diff = LogDiff(
        old_label=old_label, new_label=new_label, old_text=old_text or "", new_text=new_text or ""
    )
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace":
            paired = min(i2 - i1, j2 - j1)
            for offset in range(paired):
                diff.changes.append(
                    LineChange(
                        kind="changed",
                        old_line_no=i1 + offset + 1,
                        new_line_no=j1 + offset + 1,
                        old_text=old_lines[i1 + offset],
                        new_text=new_lines[j1 + offset],
                    )
                )
                diff.changed += 1
            for idx in range(i1 + paired, i2):
                diff.changes.append(
                    LineChange(
                        kind="removed",
                        old_line_no=idx + 1,
                        new_line_no=None,
                        old_text=old_lines[idx],
                        new_text=None,
                    )
                )
                diff.removed += 1
            for idx in range(j1 + paired, j2):
                diff.changes.append(
                    LineChange(
                        kind="added",
                        old_line_no=None,
                        new_line_no=idx + 1,
                        old_text=None,
                        new_text=new_lines[idx],
                    )
                )
                diff.added += 1
        elif tag == "delete":
            for idx in range(i1, i2):
                diff.changes.append(
                    LineChange(
                        kind="removed",
                        old_line_no=idx + 1,
                        new_line_no=None,
                        old_text=old_lines[idx],
                        new_text=None,
                    )
                )
                diff.removed += 1
        elif tag == "insert":
            for idx in range(j1, j2):
                diff.changes.append(
                    LineChange(
                        kind="added",
                        old_line_no=None,
                        new_line_no=idx + 1,
                        old_text=None,
                        new_text=new_lines[idx],
                    )
                )
                diff.added += 1
    diff.identical = not diff.changes
    return diff


# 常见别名，便于调用者按语义选择。
diff_logs = diff_lines
line_diff = diff_lines


def render_unified(
    old_text: str,
    new_text: str,
    old_label: str = "old",
    new_label: str = "new",
    *,
    context: int = 3,
) -> str:
    old_lines = (old_text or "").splitlines(keepends=True)
    new_lines = (new_text or "").splitlines(keepends=True)
    output = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=old_label,
            tofile=new_label,
            n=max(0, int(context)),
            lineterm="",
        )
    )
    return "\n".join(line.rstrip("\n") for line in output)


def _env_differences(
    old_env: Mapping[str, Any], new_env: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    differences: dict[str, dict[str, Any]] = {}
    all_keys = sorted(set(old_env) | set(new_env))
    for key in all_keys:
        if key not in old_env:
            differences[key] = {"change": "added", "old": None, "new": new_env[key]}
        elif key not in new_env:
            differences[key] = {"change": "removed", "old": old_env[key], "new": None}
        elif old_env[key] != new_env[key]:
            differences[key] = {"change": "changed", "old": old_env[key], "new": new_env[key]}
    return differences


def _fact(field: str, old: Any, new: Any) -> dict[str, Any]:
    return {
        "field": field,
        "old": old,
        "new": new,
        "changed": old != new,
        "classification": "fact",
    }


def compare_runs(
    run_a: Mapping[str, Any],
    run_b: Mapping[str, Any],
    *,
    stdout_a: str = "",
    stdout_b: str = "",
    stderr_a: str = "",
    stderr_b: str = "",
) -> ComparisonReport:
    """对比两个 run 字典；事实与推测明确分离。"""
    facts: list[dict[str, Any]] = [
        _fact("command", run_a.get("command"), run_b.get("command")),
        _fact("args", run_a.get("args"), run_b.get("args")),
        _fact("cwd", run_a.get("cwd"), run_b.get("cwd")),
        _fact("shell", bool(run_a.get("shell")), bool(run_b.get("shell"))),
        _fact("timeout", run_a.get("timeout"), run_b.get("timeout")),
        _fact("status", run_a.get("status"), run_b.get("status")),
        _fact("exit_code", run_a.get("exit_code"), run_b.get("exit_code")),
        _fact("duration_ms", run_a.get("duration_ms"), run_b.get("duration_ms")),
        _fact("project_path", run_a.get("project_path"), run_b.get("project_path")),
        _fact("git_branch", run_a.get("git_branch"), run_b.get("git_branch")),
        _fact("git_commit", run_a.get("git_commit"), run_b.get("git_commit")),
    ]
    env_diff = _env_differences(run_a.get("env") or {}, run_b.get("env") or {})
    facts.append(_fact("env", {}, env_diff))
    env_changed = bool(env_diff)

    stdout_diff = diff_lines(
        stdout_a, stdout_b, old_label=run_a.get("id", "A"), new_label=run_b.get("id", "B")
    )
    stderr_diff = diff_lines(
        stderr_a, stderr_b, old_label=run_a.get("id", "A"), new_label=run_b.get("id", "B")
    )
    speculations: list[str] = []
    old_exit = run_a.get("exit_code")
    new_exit = run_b.get("exit_code")
    old_status = run_a.get("status")
    new_status = run_b.get("status")
    if old_exit != new_exit:
        if old_exit == 0 and new_exit not in (0, None):
            speculations.append(
                f"推测：旧运行成功退出，新运行以退出码 {new_exit} 结束，新运行可能出现了执行错误；"
                "请结合 stderr 与外部依赖核实。"
            )
        elif new_exit == 0 and old_exit not in (0, None):
            speculations.append(
                f"推测：新运行以 0 退出而旧运行以 {old_exit} 退出，之前的问题可能已被修复，"
                "但需确认是否只是错误被忽略。"
            )
        else:
            speculations.append(
                f"推测：退出码由 {old_exit} 变为 {new_exit}，失败类型可能发生变化，但仍需人工验证。"
            )
    elif new_status != old_status:
        speculations.append(
            f"推测：状态由 {old_status} 变为 {new_status} 可能影响结果比较；"
            "超时/取消并不代表命令本身失败原因相同。"
        )
    command_changed = run_a.get("command") != run_b.get("command") or run_a.get("args") != run_b.get("args")
    if command_changed:
        speculations.append("推测：命令或参数发生变化，输出差异可能主要来自输入变化而非环境。")
    if run_a.get("cwd") != run_b.get("cwd"):
        speculations.append("推测：工作目录不同可能改变相对路径解析和读取的配置文件。")
    if env_changed:
        keys = ", ".join(sorted(env_diff.keys())[:20])
        speculations.append(
            f"推测：环境变量差异（{keys}）可能影响程序行为；脱敏值无法用于判断密钥是否变化。"
        )
    if not stdout_diff.identical or not stderr_diff.identical:
        if (old_exit == new_exit == 0) and not command_changed:
            speculations.append(
                "推测：退出码相同但日志不同，可能只是非确定性输出；也可能是业务结果已变化，"
                "不能仅凭退出码判断。"
            )
        elif old_exit == new_exit and new_exit not in (0, None):
            speculations.append("推测：两次均失败且日志不同，错误原因可能不同，需要查看 stderr。")
    else:
        speculations.append(
            "推测：日志与退出码均一致，复现程度可能较高；但未记录的外部服务、文件状态、"
            "交互输入仍可能影响结果。"
        )
    duration_a = run_a.get("duration_ms")
    duration_b = run_b.get("duration_ms")
    if isinstance(duration_a, int) and isinstance(duration_b, int):
        base = max(1, duration_a)
        delta_ratio = abs(duration_b - duration_a) / base
        if delta_ratio >= 0.2 and abs(duration_b - duration_a) >= 300:
            speculations.append(
                f"推测：耗时从 {duration_a}ms 变为 {duration_b}ms（变化 {delta_ratio:.1%}），"
                "可能存在缓存、负载或外部依赖变化；这是观察，不构成原因结论。"
            )
    if run_b.get("shell") or run_a.get("shell"):
        speculations.append("推测/风险：至少一次运行启用了 shell，输出与参数解析可能受 shell 转义影响。")

    report = ComparisonReport(
        facts=facts,
        speculations=speculations,
        log_diffs={"stdout": stdout_diff, "stderr": stderr_diff},
    )
    return report


def render_comparison(report: ComparisonReport) -> str:
    """包含 unified diff 的完整文本对比报告。"""
    base = report.render_text(include_unified=False)
    chunks = [base]
    for name, log_diff in report.log_diffs.items():
        if log_diff.identical:
            continue
        old_text = log_diff.old_text
        new_text = log_diff.new_text
        chunks.extend(
            [
                "",
                f"--- unified diff: {name} ---",
                render_unified(
                    old_text,
                    new_text,
                    f"{log_diff.old_label}:{name}",
                    f"{log_diff.new_label}:{name}",
                ),
            ]
        )
    return "\n".join(chunks)


# 允许调用方按更直观的名称使用。
compare_run_records = compare_runs
