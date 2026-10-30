"""向 RunTrace 写入 3 个可直接运行的样例任务模板。

用法（在 RunTrace 目录或任意位置执行均可）：

    python examples/seed_sample_tasks.py
    python examples/seed_sample_tasks.py --db C:\\temp\\demo.db
    python examples/seed_sample_tasks.py --project C:\\work\\my-project

默认写入 RunTrace 的默认数据库（%LOCALAPPDATA%\\RunTrace\\runtrace.db），
`--project` 默认是本仓库根目录。写入后可用以下命令查看或运行：

    python -m runtrace templates
    python -m runtrace template-run "Python 版本"
    python -m runtrace template-run "Git 状态"

三个模板都只执行只读或安全命令，不修改项目、不联网、不请求管理员权限。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from runtrace.run_store import RunStore, default_db_path  # noqa: E402


def _sample_templates(project: Path) -> list[dict[str, object]]:
    python = sys.executable
    return [
        {
            "name": "Python 版本",
            "cwd": str(project),
            "command": python,
            "args": [
                "-c",
                "import sys, platform; print(sys.version); print(platform.platform())",
            ],
            "timeout": 30.0,
            "project_path": str(project),
        },
        {
            "name": "运行测试",
            "cwd": str(project),
            "command": python,
            "args": ["-m", "pytest", "-q"],
            "timeout": 600.0,
            "project_path": str(project),
        },
        {
            "name": "Git 状态",
            "cwd": str(project),
            "command": "git",
            "args": ["status", "--short", "--branch"],
            "timeout": 30.0,
            "project_path": str(project),
        },
    ]


def seed(store: RunStore, project: Path) -> list[str]:
    names: list[str] = []
    for item in _sample_templates(project):
        # save_template 会覆盖同名模板，因此重复执行是幂等的。
        store.save_template(**item)  # type: ignore[arg-type]
        names.append(str(item["name"]))
    return names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="写入 RunTrace 样例任务模板")
    parser.add_argument(
        "--db",
        default=str(default_db_path()),
        help="SQLite 数据库路径（默认使用 RunTrace 默认数据库）",
    )
    parser.add_argument(
        "--project",
        default=str(_REPO_ROOT),
        help="样例任务的工作目录（默认本仓库根目录）",
    )
    args = parser.parse_args(argv)

    project = Path(args.project).expanduser().resolve()
    if not project.is_dir():
        parser.error(f"工作目录不存在：{project}")

    db_path = Path(args.db).expanduser()
    with RunStore(db_path) as store:
        names = seed(store, project)
        print(f"数据库：{db_path}")
        print(f"项目目录：{project}")
        for row in store.list_templates():
            print(f"  #{row['id']}  {row['name']}  ->  {row['command']} {row['args']}")

    print(f"已写入 {len(names)} 个样例任务：{', '.join(names)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
