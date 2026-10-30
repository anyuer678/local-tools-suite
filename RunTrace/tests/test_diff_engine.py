from runtrace.diff_engine import compare_runs, diff_lines, render_comparison, render_unified


def _run(rid, *, status, exit_code, duration, env=None, command="python", args=None):
    return {
        "id": rid,
        "command": command,
        "args": args or ["-c", "print(1)"],
        "cwd": "C:/work",
        "shell": False,
        "timeout": 10,
        "status": status,
        "exit_code": exit_code,
        "duration_ms": duration,
        "project_path": "C:/work",
        "git_branch": "main",
        "git_commit": "abc123",
        "env": env or {"PATH": "C:/bin", "MODE": "test"},
    }


def test_line_diff_changed_added_removed():
    diff = diff_lines("alpha\nbeta\ngamma", "alpha\nBETA\ngamma\ndelta")
    assert diff.identical is False
    kinds = [change.kind for change in diff.changes]
    assert kinds.count("changed") == 1
    assert kinds.count("added") == 1
    assert diff.removed == 0
    changed = next(change for change in diff.changes if change.kind == "changed")
    assert changed.old_line_no == 2
    assert changed.new_line_no == 2
    assert changed.old_text == "beta"
    assert changed.new_text == "BETA"

    deleted = diff_lines("one\ntwo\nthree\n", "one\nthree\n")
    assert deleted.removed == 1
    assert deleted.added == 0
    assert deleted.changes[0].old_text == "two"


def test_line_diff_identical_and_unified():
    assert diff_lines("same\n", "same\n").identical is True
    unified = render_unified("a\nb\n", "a\nc\n", "old.log", "new.log")
    assert "-b" in unified
    assert "+c" in unified
    assert "old.log" in unified


def test_compare_runs_facts_and_speculations_are_separate():
    a = _run("A", status="success", exit_code=0, duration=1000)
    b = _run(
        "B",
        status="failed",
        exit_code=3,
        duration=1500,
        env={"PATH": "C:/bin", "MODE": "prod"},
        args=["-c", "print(2)"],
    )
    report = compare_runs(
        a,
        b,
        stdout_a="line1\nline2\n",
        stdout_b="line1\nCHANGED\nline3\nline4\n",
        stderr_a="",
        stderr_b="error\n",
    )
    facts = {fact["field"]: fact for fact in report.facts}
    assert facts["exit_code"]["changed"] is True
    assert facts["args"]["changed"] is True
    assert facts["env"]["changed"] is True
    assert all(fact["classification"] == "fact" for fact in report.facts)
    assert report.log_diffs["stdout"].changed >= 1
    assert report.log_diffs["stdout"].added >= 1
    assert report.log_diffs["stderr"].added >= 1
    assert report.speculations
    assert all(text.startswith("推测") for text in report.speculations)
    text = render_comparison(report)
    assert "【事实】" in text
    assert "【推测（非事实）】" in text
    assert "line2" in text
    assert report.to_dict()["log_diffs"]["stdout"]["counts"]["added"] >= 1


def test_compare_identical_runs_reports_possible_reproduction():
    a = _run("A", status="success", exit_code=0, duration=1000)
    b = _run("B", status="success", exit_code=0, duration=1000)
    report = compare_runs(
        a,
        b,
        stdout_a="same\n",
        stdout_b="same\n",
        stderr_a="",
        stderr_b="",
    )
    assert report.log_diffs["stdout"].identical
    assert any("复现" in item for item in report.speculations)
    assert all(item["changed"] is False for item in report.facts)
