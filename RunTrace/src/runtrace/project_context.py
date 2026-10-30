"""只读获取项目/Git 上下文。

这里只调用 git 的查询命令，并设置 ``GIT_OPTIONAL_LOCKS=0``，避免刷新索引等
可选写操作。任何失败都应被转换成结果字段，而不是抛出到调用者。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _git(
    cwd: str,
    args: list[str],
    *,
    timeout: float = 5.0,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        ["git", "-C", cwd, *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
        check=False,
        **kwargs,
    )


def get_git_context(path: str | os.PathLike[str]) -> dict:
    """返回只读 Git 快照；非 Git 目录、路径不存在或 git 缺失都不抛异常。"""
    raw = str(path)
    result: dict = {
        "path": raw,
        "is_git": False,
        "root": None,
        "branch": None,
        "commit": None,
        "commit_short": None,
        "dirty": None,
        "git_version": None,
        "error": None,
    }
    try:
        resolved = Path(raw).expanduser().resolve()
    except OSError as exc:
        result["error"] = f"路径无法解析：{exc}"
        return result
    result["path"] = str(resolved)
    if not resolved.is_dir():
        result["error"] = "路径不存在或不是目录"
        return result

    try:
        inside = _git(str(resolved), ["rev-parse", "--is-inside-work-tree"])
    except FileNotFoundError:
        result["error"] = "未找到 git 可执行文件"
        return result
    except (subprocess.TimeoutExpired, OSError) as exc:
        result["error"] = f"调用 git 失败：{exc}"
        return result

    if inside.returncode != 0 or inside.stdout.strip().lower() != "true":
        result["error"] = "不是 Git 工作区（只读检测）"
        return result

    result["is_git"] = True
    try:
        root = _git(str(resolved), ["rev-parse", "--show-toplevel"])
    except (subprocess.TimeoutExpired, OSError) as exc:
        result["error"] = f"读取 git 根目录失败：{exc}"
        return result
    if root.returncode == 0:
        result["root"] = root.stdout.strip()

    try:
        branch = _git(str(resolved), ["symbolic-ref", "--short", "HEAD"])
        if branch.returncode == 0:
            result["branch"] = branch.stdout.strip()
        else:
            branch = _git(str(resolved), ["rev-parse", "--abbrev-ref", "HEAD"])
            if branch.returncode == 0:
                label = branch.stdout.strip()
                result["branch"] = None if label == "HEAD" else label
    except (subprocess.TimeoutExpired, OSError) as exc:
        result["error"] = f"读取分支失败：{exc}"

    try:
        commit = _git(str(resolved), ["rev-parse", "HEAD"])
        if commit.returncode == 0:
            full = commit.stdout.strip()
            result["commit"] = full
            result["commit_short"] = full[:12]
    except (subprocess.TimeoutExpired, OSError) as exc:
        result["error"] = f"读取提交失败：{exc}"

    try:
        # status 只读，--porcelain 输出为空表示干净工作区。
        status = _git(str(resolved), ["status", "--porcelain", "--untracked-files=normal"])
        if status.returncode == 0:
            result["dirty"] = bool(status.stdout.strip())
        else:
            result["error"] = status.stderr.strip() or "git status 执行失败"
    except (subprocess.TimeoutExpired, OSError) as exc:
        result["error"] = f"读取工作区状态失败：{exc}"

    try:
        version = _git(str(resolved), ["--version"])
        if version.returncode == 0:
            result["git_version"] = version.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return result


def project_label(context: dict) -> str:
    """用于历史列表的简短项目标签。"""
    if context.get("is_git"):
        root = context.get("root") or context.get("path") or ""
        branch = context.get("branch") or "detached"
        dirty = "dirty" if context.get("dirty") else "clean"
        return f"{root} [{branch}, {dirty}]"
    return context.get("path") or ""
