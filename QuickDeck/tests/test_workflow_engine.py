from __future__ import annotations

import time
from pathlib import Path

import pytest

from quickdeck.clipboard_service import ClipboardHistoryService, MemoryClipboardBackend
from quickdeck.command_registry import CommandRegistry
from quickdeck.workflow_engine import (
    ActionDefinition,
    ActionError,
    ActionRunner,
    ActionStore,
    StepTimeout,
    WorkflowEngine,
    WorkflowError,
    WorkflowStep,
)


def test_workflow_chains_text_steps() -> None:
    engine = WorkflowEngine()
    result = engine.run(
        [
            WorkflowStep("text.trim", name="trim"),
            WorkflowStep("text.upper", name="upper"),
            WorkflowStep("text.replace", params={"old": "A", "new": "B"}),
        ],
        "  a a  ",
    )
    assert result.output == "B B"
    assert len(result.steps) == 3
    assert all(step.error == "" for step in result.steps)


def test_workflow_params_and_original_input_source() -> None:
    engine = WorkflowEngine()
    result = engine.run(
        [
            WorkflowStep("text.replace", params={"old": "foo", "new": "bar"}),
            WorkflowStep("text.upper", input_source="original"),
        ],
        "foo",
    )
    assert result.output == "FOO"


def test_workflow_empty_and_unknown_operation() -> None:
    engine = WorkflowEngine()
    assert engine.run([], "abc").output == "abc"
    with pytest.raises(WorkflowError, match="未知步骤操作"):
        engine.run([WorkflowStep("does.not.exist")], "abc")


def test_workflow_error_propagation_and_skip_default() -> None:
    engine = WorkflowEngine()
    with pytest.raises(WorkflowError, match="执行失败"):
        engine.run([WorkflowStep("text.upper"), WorkflowStep("does.not.exist")], "a")

    result = engine.run(
        [
            WorkflowStep("text.upper"),
            WorkflowStep("does.not.exist", on_error="skip"),
            WorkflowStep("text.upper"),
        ],
        "a",
    )
    assert result.output == "A"
    assert result.steps[1].skipped
    assert "未知步骤" in result.steps[1].error

    result = engine.run(
        [
            WorkflowStep("does.not.exist", on_error="default", default="fallback"),
            WorkflowStep("text.upper"),
        ],
        "a",
    )
    assert result.output == "FALLBACK"
    assert result.steps[0].skipped


def test_workflow_timeout_and_skip_timeout() -> None:
    engine = WorkflowEngine({"test.slow": lambda text, **_: (time.sleep(0.3), text)[1]})
    with pytest.raises(StepTimeout, match="执行超时"):
        engine.run([WorkflowStep("test.slow", timeout=0.05)], "abc")
    result = engine.run(
        [
            WorkflowStep("text.upper"),
            WorkflowStep("test.slow", timeout=0.05, on_error="skip"),
        ],
        "abc",
    )
    assert result.output == "ABC"
    assert result.steps[1].timed_out
    assert result.steps[1].skipped


def test_workflow_timeout_validation() -> None:
    with pytest.raises(WorkflowError, match="超时"):
        WorkflowStep("text.upper", timeout=0)
    with pytest.raises(WorkflowError, match="文本输出"):
        WorkflowStep("text.upper", output_type="json")


def test_action_runner_output_methods(tmp_path: Path) -> None:
    backend = MemoryClipboardBackend()
    clipboard = ClipboardHistoryService(backend=backend)
    runner = ActionRunner(clipboard=clipboard)

    display_action = ActionDefinition(
        id="display",
        name="显示动作",
        steps=[WorkflowStep("text.upper")],
        output_method="display",
    )
    result = runner.execute(display_action, "abc")
    assert result.output == "ABC"
    assert result.delivered_to == "display"

    clipboard_action = ActionDefinition(
        id="clip",
        name="复制动作",
        steps=[WorkflowStep("text.upper")],
        output_method="clipboard",
    )
    result = runner.execute(clipboard_action, "abc")
    assert result.delivered_to == "clipboard"
    assert backend.get_text() == "ABC"

    target = tmp_path / "out" / "result.txt"
    file_action = ActionDefinition(
        id="file",
        name="文件动作",
        steps=[WorkflowStep("text.upper")],
        output_method="file",
        output_path=str(target),
    )
    result = runner.execute(file_action, "abc")
    assert result.delivered_to == str(target)
    assert target.read_text(encoding="utf-8") == "ABC"


def test_action_validation() -> None:
    with pytest.raises(ActionError, match="至少需要一个步骤"):
        ActionDefinition(id="x", name="x", steps=[])
    with pytest.raises(ActionError, match="output_path"):
        ActionDefinition(
            id="x",
            name="x",
            steps=[WorkflowStep("text.upper")],
            output_method="file",
        )
    with pytest.raises(ActionError, match="输出方式"):
        ActionDefinition(
            id="x",
            name="x",
            steps=[WorkflowStep("text.upper")],
            output_method="bad",
        )


def test_action_store_persistence_and_registry(tmp_path: Path) -> None:
    db = tmp_path / "q.db"
    store = ActionStore(db)
    backend = MemoryClipboardBackend()
    clipboard = ClipboardHistoryService(backend=backend)
    runner = ActionRunner(clipboard=clipboard)
    action = ActionDefinition.from_dict(
        {
            "id": "upper",
            "name": "转大写",
            "aliases": ["upper"],
            "steps": [{"operation": "text.upper"}],
            "output_method": "display",
        }
    )
    store.save(action)
    assert store.get("upper").name == "转大写"  # type: ignore[union-attr]
    assert len(store.all()) == 1

    registry = CommandRegistry(db)
    assert store.register_into(registry, runner) == 1
    command_id = "action.upper"
    assert registry.search("upper")[0].id == command_id
    assert registry.execute(command_id, "abc") == "ABC"

    store.close()
    reopened = ActionStore(db)
    assert reopened.get("upper") is not None
    assert reopened.delete("upper")
    assert reopened.get("upper") is None
    registry.close()
    reopened.close()
