import shutil
import subprocess
from pathlib import Path

import pytest

from runtrace.project_context import get_git_context, project_label


def test_non_git_directory_does_not_raise(tmp_path):
    context = get_git_context(tmp_path)
    assert context["is_git"] is False
    assert context["branch"] is None
    assert context["commit"] is None
    assert context["dirty"] is None
    assert context["error"]  # 给出说明，但不抛出
    assert str(tmp_path.resolve()) in project_label(context)


def test_missing_path_does_not_raise(tmp_path):
    context = get_git_context(tmp_path / "missing")
    assert context["is_git"] is False
    assert context["error"]
    assert project_label(context)


@pytest.mark.skipif(shutil.which("git") is None, reason="git 未安装")
def test_git_context_is_read_only_and_reports_branch_commit_dirty(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git = shutil.which("git")
    assert git
    subprocess.run([git, "-C", str(repo), "init", "-b", "main"], check=True, capture_output=True)
    subprocess.run(
        [git, "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@example.com", "add", "."],
        check=True,
        capture_output=True,
    )
    # 空仓库没有可 add 文件，先创建一个文件再提交。
    (repo / "a.txt").write_text("one", encoding="utf-8")
    subprocess.run([git, "-C", str(repo), "add", "a.txt"], check=True, capture_output=True)
    subprocess.run(
        [
            git,
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "init",
        ],
        check=True,
        capture_output=True,
    )
    context = get_git_context(repo)
    assert context["is_git"] is True
    assert context["branch"] == "main"
    assert context["commit"]
    assert context["commit_short"]
    assert context["dirty"] is False
    assert "/" in project_label(context).replace("\\", "/")

    (repo / "b.txt").write_text("dirty", encoding="utf-8")
    dirty_context = get_git_context(repo)
    assert dirty_context["dirty"] is True
    # 查询过程不会修改仓库；新增未跟踪文件状态保持。
    assert (repo / "b.txt").exists()
