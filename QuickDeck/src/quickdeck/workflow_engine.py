"""纯文本工作流引擎与自定义动作存储。

一个动作由多个文本步骤串联而成；每步可声明输入/输出类型、超时和错误策略。
超时通过守护线程实现，插件/外部调用不会拖住主进程。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .builtin_actions import (
    base64_decode,
    base64_encode,
    change_case,
    dedupe_lines,
    filter_lines,
    format_json,
    format_unit_result,
    replace_text,
    sort_lines,
    trim_text,
    unicode_escape,
    unicode_unescape,
    url_decode,
    url_encode,
    validate_json,
)
from .command_registry import Command, CommandContext, CommandRegistry


class WorkflowError(RuntimeError):
    pass


class StepTimeout(WorkflowError):
    pass


class ActionError(WorkflowError):
    pass


Operation = Callable[..., str]


def _op_identity(text: str, **_params: Any) -> str:
    return text


def _op_reverse(text: str, **_params: Any) -> str:
    return str(text)[::-1]


def _op_char_count(text: str, **_params: Any) -> str:
    return str(len(str(text)))


def _op_line_count(text: str, **_params: Any) -> str:
    return str(len(str(text).splitlines()))


def _op_json_validate(text: str, **_params: Any) -> str:
    value = validate_json(text)
    return f"JSON 有效（{type(value).__name__}）"


def _op_unit_convert(text: str, **params: Any) -> str:
    return format_unit_result(
        params.get("value", text),
        str(params.get("from_unit", params.get("from", ""))),
        str(params.get("to_unit", params.get("to", ""))),
        category=params.get("category"),
    )


def _op_calc(text: str, **_params: Any) -> str:
    from .builtin_actions import calculator

    return calculator(text)


def default_operations() -> dict[str, Operation]:
    """内置纯文本步骤操作。"""

    return {
        "identity": _op_identity,
        "noop": _op_identity,
        "text.reverse": _op_reverse,
        "text.char_count": _op_char_count,
        "text.line_count": _op_line_count,
        "text.upper": lambda text, **_: change_case(text, "upper"),
        "text.lower": lambda text, **_: change_case(text, "lower"),
        "text.title": lambda text, **_: change_case(text, "title"),
        "text.swapcase": lambda text, **_: change_case(text, "swap"),
        "text.trim": lambda text, **params: trim_text(
            text, each_line=bool(params.get("each_line", False))
        ),
        "text.dedupe": lambda text, **params: dedupe_lines(
            text,
            ignore_case=bool(params.get("ignore_case", False)),
            keep_blank=bool(params.get("keep_blank", True)),
        ),
        "text.sort": lambda text, **params: sort_lines(
            text,
            descending=bool(params.get("descending", False)),
            case_sensitive=bool(params.get("case_sensitive", False)),
            numeric=bool(params.get("numeric", False)),
        ),
        "text.filter": lambda text, **params: filter_lines(
            text,
            str(params.get("pattern", "")),
            mode=str(params.get("mode", "include")),
            regex=bool(params.get("regex", False)),
            ignore_case=bool(params.get("ignore_case", False)),
        ),
        "text.replace": lambda text, **params: replace_text(
            text,
            str(params.get("old", "")),
            str(params.get("new", "")),
            regex=bool(params.get("regex", False)),
            ignore_case=bool(params.get("ignore_case", False)),
            count=int(params.get("count", 0)),
        ),
        "encoding.url_encode": lambda text, **_: url_encode(text),
        "encoding.url_decode": lambda text, **_: url_decode(text),
        "encoding.base64_encode": lambda text, **_: base64_encode(text),
        "encoding.base64_decode": lambda text, **_: base64_decode(text),
        "encoding.unicode_escape": lambda text, **_: unicode_escape(text),
        "encoding.unicode_unescape": lambda text, **_: unicode_unescape(text),
        "encoding.json_format": lambda text, **params: format_json(
            text,
            sort_keys=bool(params.get("sort_keys", False)),
            indent=int(params.get("indent", 2)),
        ),
        "encoding.json_validate": _op_json_validate,
        "unit.convert": _op_unit_convert,
        "calc.eval": _op_calc,
    }


@dataclass
class WorkflowStep:
    operation: str
    params: dict[str, Any] = field(default_factory=dict)
    name: str = ""
    input_type: str = "text"
    output_type: str = "text"
    input_source: str = "previous"
    timeout: float = 5.0
    on_error: str = "fail"
    default: str = ""

    def __post_init__(self) -> None:
        self.operation = str(self.operation).strip()
        if not self.operation:
            raise WorkflowError("步骤 operation 不能为空")
        self.input_type = str(self.input_type or "text").casefold()
        self.output_type = str(self.output_type or "text").casefold()
        if self.input_type not in {"text", "string"}:
            raise WorkflowError(f"暂只支持文本输入，收到：{self.input_type}")
        if self.output_type != "text":
            raise WorkflowError(f"暂只支持文本输出，收到：{self.output_type}")
        self.input_source = str(self.input_source or "previous").casefold()
        if self.input_source not in {"previous", "original"}:
            raise WorkflowError("input_source 只能是 previous 或 original")
        self.timeout = float(self.timeout)
        if self.timeout <= 0:
            raise WorkflowError("步骤超时必须大于 0")
        self.on_error = str(self.on_error or "fail").casefold()
        if self.on_error not in {"fail", "skip", "default"}:
            raise WorkflowError("on_error 只能是 fail/skip/default")
        if not isinstance(self.params, dict):
            raise WorkflowError("步骤 params 必须是对象")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WorkflowStep":
        if not isinstance(data, Mapping):
            raise WorkflowError("步骤必须是对象")
        return cls(
            operation=str(data.get("operation", "")),
            params=dict(data.get("params") or {}),
            name=str(data.get("name", "")),
            input_type=str(data.get("input_type", "text")),
            output_type=str(data.get("output_type", "text")),
            input_source=str(data.get("input_source", "previous")),
            timeout=float(data.get("timeout", 5.0)),
            on_error=str(data.get("on_error", "fail")),
            default=str(data.get("default", "")),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "name": self.name,
            "params": dict(self.params),
            "input_type": self.input_type,
            "output_type": self.output_type,
            "input_source": self.input_source,
            "timeout": self.timeout,
            "on_error": self.on_error,
            "default": self.default,
        }


@dataclass
class StepResult:
    index: int
    name: str
    operation: str
    output: str
    skipped: bool = False
    error: str = ""
    timed_out: bool = False
    duration: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "operation": self.operation,
            "output": self.output,
            "skipped": self.skipped,
            "error": self.error,
            "timed_out": self.timed_out,
            "duration": self.duration,
        }


@dataclass
class WorkflowResult:
    output: str
    steps: list[StepResult] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"output": self.output, "steps": [step.as_dict() for step in self.steps]}


class WorkflowEngine:
    def __init__(
        self,
        operations: Mapping[str, Operation] | None = None,
        *,
        default_timeout: float = 5.0,
    ) -> None:
        self.operations: dict[str, Operation] = default_operations()
        if operations:
            self.operations.update(operations)
        self.default_timeout = float(default_timeout)
        if self.default_timeout <= 0:
            raise WorkflowError("默认超时必须大于 0")

    def run(
        self,
        workflow: Sequence[WorkflowStep] | Mapping[str, Any] | None,
        input_text: str = "",
        params: Mapping[str, Any] | None = None,
    ) -> WorkflowResult:
        steps = self._coerce_steps(workflow)
        original = str(input_text)
        current = original
        results: list[StepResult] = []
        for index, step in enumerate(steps, start=1):
            effective_timeout = (
                step.timeout if step.timeout is not None else self.default_timeout
            )
            source_text = original if step.input_source == "original" else current
            started = time.monotonic()
            try:
                output = self._run_step(step, source_text, params or {}, effective_timeout)
                if not isinstance(output, str):
                    raise WorkflowError(
                        f"步骤 {index}（{step.name or step.operation}）输出不是文本"
                    )
            except Exception as exc:
                elapsed = time.monotonic() - started
                error_text = str(exc) or type(exc).__name__
                timed_out = isinstance(exc, StepTimeout)
                if step.on_error == "skip":
                    results.append(
                        StepResult(
                            index=index,
                            name=step.name,
                            operation=step.operation,
                            output=current,
                            skipped=True,
                            error=error_text,
                            timed_out=timed_out,
                            duration=elapsed,
                        )
                    )
                    continue
                if step.on_error == "default":
                    current = str(step.default)
                    results.append(
                        StepResult(
                            index=index,
                            name=step.name,
                            operation=step.operation,
                            output=current,
                            skipped=True,
                            error=error_text,
                            timed_out=timed_out,
                            duration=elapsed,
                        )
                    )
                    continue
                label = step.name or step.operation
                if timed_out:
                    raise StepTimeout(
                        f"步骤 {index}（{label}）执行超时（{effective_timeout:g} 秒）"
                    ) from exc
                raise WorkflowError(f"步骤 {index}（{label}）执行失败：{error_text}") from exc
            elapsed = time.monotonic() - started
            current = output
            results.append(
                StepResult(
                    index=index,
                    name=step.name,
                    operation=step.operation,
                    output=output,
                    duration=elapsed,
                )
            )
        return WorkflowResult(output=current, steps=results)

    def _run_step(
        self,
        step: WorkflowStep,
        text: str,
        params: Mapping[str, Any],
        timeout: float,
    ) -> str:
        operation = self.operations.get(step.operation)
        if operation is None:
            raise WorkflowError(f"未知步骤操作：{step.operation}")
        merged = dict(params)
        merged.update(step.params)
        return self._call_with_timeout(
            operation,
            text,
            merged,
            timeout,
            label=step.name or step.operation,
        )

    @staticmethod
    def _call_with_timeout(
        operation: Operation,
        text: str,
        params: Mapping[str, Any],
        timeout: float,
        *,
        label: str = "",
    ) -> str:
        done = threading.Event()
        box: dict[str, Any] = {}

        def target() -> None:
            try:
                box["value"] = operation(text, **dict(params))
            except Exception as exc:  # 操作错误由主线程转换
                box["error"] = exc
            finally:
                done.set()

        thread = threading.Thread(
            target=target,
            name=f"quickdeck-step-{label}",
            daemon=True,
        )
        thread.start()
        if not done.wait(timeout):
            raise StepTimeout(f"步骤 {label} 超过 {timeout:g} 秒")
        if "error" in box:
            exc = box["error"]
            if isinstance(exc, WorkflowError):
                raise exc
            raise WorkflowError(str(exc) or type(exc).__name__)
        return box.get("value")

    @staticmethod
    def _coerce_steps(
        workflow: Sequence[WorkflowStep] | Mapping[str, Any] | None,
    ) -> list[WorkflowStep]:
        if workflow is None:
            return []
        if isinstance(workflow, Mapping):
            raw_steps = workflow.get("steps", [])
        else:
            raw_steps = workflow
        if not isinstance(raw_steps, Iterable) or isinstance(raw_steps, (str, bytes)):
            raise WorkflowError("workflow.steps 必须是数组")
        steps: list[WorkflowStep] = []
        for raw in raw_steps:
            if isinstance(raw, WorkflowStep):
                steps.append(raw)
            elif isinstance(raw, Mapping):
                steps.append(WorkflowStep.from_dict(raw))
            else:
                raise WorkflowError("步骤必须是 WorkflowStep 或对象")
        return steps


@dataclass
class ActionDefinition:
    id: str
    name: str
    steps: list[WorkflowStep] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    category: str = "动作"
    keywords: list[str] = field(default_factory=list)
    input_params: list[str] = field(default_factory=list)
    output_method: str = "display"
    output_path: str | None = None

    def __post_init__(self) -> None:
        self.id = str(self.id).strip()
        self.name = str(self.name).strip()
        if not self.id or not self.name:
            raise ActionError("动作 id 与名称不能为空")
        self.output_method = str(self.output_method or "display").casefold()
        if self.output_method not in {"clipboard", "file", "display"}:
            raise ActionError("输出方式必须是 clipboard/file/display")
        if self.output_method == "file" and not self.output_path:
            raise ActionError("文件输出必须提供 output_path")
        if not isinstance(self.steps, list):
            self.steps = list(self.steps)
        self.steps = [
            step if isinstance(step, WorkflowStep) else WorkflowStep.from_dict(step)
            for step in self.steps
        ]
        if not self.steps:
            raise ActionError("动作至少需要一个步骤")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ActionDefinition":
        if not isinstance(data, Mapping):
            raise ActionError("动作必须是对象")
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            steps=[WorkflowStep.from_dict(step) for step in data.get("steps", [])],
            aliases=[str(x) for x in data.get("aliases", [])],
            description=str(data.get("description", "")),
            category=str(data.get("category", "动作")),
            keywords=[str(x) for x in data.get("keywords", [])],
            input_params=[str(x) for x in data.get("input_params", [])],
            output_method=str(data.get("output_method", data.get("output", "display"))),
            output_path=(
                str(data["output_path"]) if data.get("output_path") is not None else None
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "aliases": list(self.aliases),
            "description": self.description,
            "category": self.category,
            "keywords": list(self.keywords),
            "input_params": list(self.input_params),
            "output_method": self.output_method,
            "output_path": self.output_path,
            "steps": [step.as_dict() for step in self.steps],
        }

    def command(self, *, source: str = "action") -> Command:
        return Command(
            id=f"action.{self.id}",
            name=self.name,
            aliases=list(self.aliases),
            category=self.category,
            keywords=list(self.keywords) + ["action", "动作"],
            description=self.description,
            source=source,
        )


@dataclass
class ActionExecution:
    output: str
    delivered_to: str
    message: str
    workflow: WorkflowResult

    def as_dict(self) -> dict[str, Any]:
        return {
            "output": self.output,
            "delivered_to": self.delivered_to,
            "message": self.message,
            "workflow": self.workflow.as_dict(),
        }


class ActionRunner:
    def __init__(
        self,
        engine: WorkflowEngine | None = None,
        clipboard: Any | None = None,
    ) -> None:
        self.engine = engine or WorkflowEngine()
        self.clipboard = clipboard

    def execute(
        self,
        action: ActionDefinition | Mapping[str, Any],
        input_text: str,
        params: Mapping[str, Any] | None = None,
    ) -> ActionExecution:
        definition = action if isinstance(action, ActionDefinition) else ActionDefinition.from_dict(action)
        result = self.engine.run(definition.steps, input_text, params)
        output = result.output
        if definition.output_method == "clipboard":
            if self.clipboard is None:
                raise ActionError("动作要求复制到剪贴板，但剪贴板服务不可用")
            try:
                self.clipboard.set_text(output)
            except Exception as exc:
                raise ActionError(f"写剪贴板失败：{exc}") from exc
            return ActionExecution(
                output=output,
                delivered_to="clipboard",
                message="结果已复制到剪贴板",
                workflow=result,
            )
        if definition.output_method == "file":
            path = Path(str(definition.output_path)).expanduser()
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(output, encoding="utf-8")
            except OSError as exc:
                raise ActionError(f"写入文件失败：{exc}") from exc
            return ActionExecution(
                output=output,
                delivered_to=str(path),
                message=f"结果已写入 {path}",
                workflow=result,
            )
        return ActionExecution(
            output=output,
            delivered_to="display",
            message=output,
            workflow=result,
        )


class ActionStore:
    """自定义动作的 SQLite 持久化与命令注册。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS custom_actions (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                payload TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
            """
        )
        self._conn.commit()

    def save(self, action: ActionDefinition | Mapping[str, Any]) -> ActionDefinition:
        definition = action if isinstance(action, ActionDefinition) else ActionDefinition.from_dict(action)
        self._conn.execute(
            """
            INSERT INTO custom_actions(id, name, payload, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, payload=excluded.payload, updated_at=excluded.updated_at
            """,
            (
                definition.id,
                definition.name,
                json.dumps(definition.as_dict(), ensure_ascii=False),
                time.time(),
            ),
        )
        self._conn.commit()
        return definition

    def get(self, action_id: str) -> ActionDefinition | None:
        row = self._conn.execute(
            "SELECT payload FROM custom_actions WHERE id = ?", (action_id,)
        ).fetchone()
        if row is None:
            return None
        return ActionDefinition.from_dict(json.loads(row["payload"]))

    def all(self) -> list[ActionDefinition]:
        rows = self._conn.execute(
            "SELECT payload FROM custom_actions ORDER BY name COLLATE NOCASE"
        ).fetchall()
        return [ActionDefinition.from_dict(json.loads(row["payload"])) for row in rows]

    def delete(self, action_id: str) -> bool:
        cursor = self._conn.execute("DELETE FROM custom_actions WHERE id = ?", (action_id,))
        self._conn.commit()
        return bool(cursor.rowcount)

    def register_into(
        self,
        registry: CommandRegistry,
        runner: ActionRunner,
        *,
        source: str = "action",
    ) -> int:
        count = 0
        for action in self.all():
            command = action.command(source=source)

            def handler(ctx: CommandContext, _action_id: str = action.id) -> str:
                current = self.get(_action_id)
                if current is None:
                    raise ActionError(f"动作已不存在：{_action_id}")
                execution = runner.execute(current, ctx.text, ctx.params)
                return execution.message

            registry.register(command, handler)
            count += 1
        return count

    def close(self) -> None:
        self._conn.close()
