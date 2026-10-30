# RunTrace

RunTrace 是面向 Windows 开发者的本地任务运行历史、差异分析与复现平台。它记录
本地命令的运行方式、工作目录、脱敏环境、实时输出、退出码、耗时和项目/Git
上下文，并支持检索、导出、两次运行对比与复现包。

所有核心逻辑只使用 Python 标准库；桌面界面使用可选的 PySide6。PySide6 缺失时
`python -m runtrace` 会自动回退到可用 CLI，不会直接崩溃。

## 功能

- **运行任务**：名称、工作目录、命令、JSON 参数数组、环境覆盖、超时。默认
  `shell=False`，只有显式勾选/传入 shell 模式才会经过系统 shell，并始终显示风险
  提示。运行前展示完整命令与工作目录；运行中实时显示 stdout/stderr、状态和耗时。
- **精确终止**：停止/超时会用 Windows `taskkill /T /F` 终止子进程树；子进程以
  新进程组启动。
- **运行历史**：唯一 run id，SQLite 事务与索引，保存命令、参数、cwd、脱敏环境、
  工具/Python 版本、日志、元数据。支持 LIKE 全文搜索（含日志）、项目/状态/时间
  筛选。
- **流式日志**：reader 线程按块读取，64 KiB 窗口批量写入 SQLite，后台定时 flush；
  不把完整日志常驻内存。默认不自动删除日志。
- **异常恢复**：启动时把遗留的 `running` 记录标记为 `interrupted`，并补齐结束时间。
- **对比**：`difflib` 行级新增/删除/变化，同时对比命令、参数、退出码、耗时、环境、
  Git；明确区分“事实”和“推测”。
- **导出**：JSON、文本、ZIP 归档；导出前扫描疑似敏感内容并报告（不会静默隐藏）。
- **复现包**：zip 内含 manifest、run 元数据、命令配置、日志、SHA-256 校验和、
  复现说明；列出脱敏/未记录环境和外部依赖。执行必须显式确认。
- **项目工作区**：读取 Git branch/commit/dirty；仅调用只读查询，`GIT_OPTIONAL_LOCKS=0`，
  不会提交、修改或清理仓库。
- **模板**：保存、覆盖、复制、加载、删除；再次运行历史任务。敏感环境值不会明文
  持久化。

## 目录

```text
RunTrace/
  pyproject.toml
  requirements.txt
  README.md
  runtrace.py               # 源码检出时的 python -m runtrace 兼容启动器
  docs/
    architecture.md
    test-report.md
    packaging.md
  examples/
    seed_sample_tasks.py   # 写入 3 个可直接运行的样例模板
  src/runtrace/
    __init__.py  __main__.py  cli.py
    command_builder.py  process_runner.py  stream_capture.py
    run_store.py  diff_engine.py  replay_manager.py
    project_context.py  redaction.py
    ui/__init__.py  ui/qt_app.py
  tests/
```

## 环境与安装

- Python 3.12，Windows 10/11。
- 核心零第三方依赖；测试使用 pytest 9.0.3。
- 可选的 GUI 依赖：`PySide6>=6.7`（`requirements.txt` 中仅作注释，默认不强制安装）。

在源码目录直接运行（不安装项目）：

```powershell
cd RunTrace
python -m runtrace --help
python -m runtrace
```

根目录 `runtrace.py` 只负责把启动交给 `src/runtrace`；它不会进入正式 wheel。
如果要在交互式 Python 中 `import runtrace`，请设置 `$env:PYTHONPATH = "src"` 或
安装项目。

或安装当前项目（无网络、无额外核心依赖）：

```powershell
python -m pip install -e .
# 如需 GUI：
python -m pip install -e ".[gui]"
```

## 启动

```powershell
# 有 PySide6 时启动桌面界面；没有时自动打印 CLI 帮助并回退。
python -m runtrace

# 强制 GUI（缺少 PySide6 时返回非零退出码）
python -m runtrace --gui

# CLI 入门
python -m runtrace run --name demo --cwd . -- python -c "print('hello')"
python -m runtrace list --limit 20
python -m runtrace show <run-id>
python -m runtrace show <run-id> --stream stderr
python -m runtrace diff <run-a> <run-b>
python -m runtrace export <run-id> --format json --output run.json
python -m runtrace replay <run-id> --output replay.zip
python -m runtrace replay-inspect replay.zip
python -m runtrace replay-execute replay.zip --yes   # 仅在确认后
python -m runtrace project .
python -m runtrace rerun <run-id>
python -m runtrace templates
```

显式 shell（有风险，不要用于不可信输入）：

```powershell
python -m runtrace run --shell --name risky -- "echo hello"
```

GUI 中同样可以新建运行、停止、查看历史/日志、选择两条记录对比、导出、生成与
执行复现包、保存/复制/加载模板。

## 样例任务

源码仓库提供 3 个安全的样例模板（Python 版本、运行测试、Git 状态）。便携 ZIP
只包含可执行程序与文档，不包含源码，因此样例脚本需在源码目录执行：

```powershell
python examples\seed_sample_tasks.py
python examples\seed_sample_tasks.py --db C:\temp\demo.db --project C:\work\my-project
python -m runtrace --db C:\temp\demo.db templates
python -m runtrace --db C:\temp\demo.db template-run "Python 版本"
```

模板只执行只读/安全命令；重复执行脚本会覆盖同名模板，不会产生重复项。

## 架构

模块边界、运行时数据流、SQLite 表结构、线程/子进程模型与安全策略见
[`docs/architecture.md`](docs/architecture.md)。

## 数据位置

默认数据库：`%LOCALAPPDATA%\RunTrace\runtrace.db`。所有内部日志都存在 SQLite，
没有用户可指定的日志文件路径；导出/复现包由用户通过文件对话框或 CLI 指定。

可通过环境变量 `RUNTRACE_DATA_DIR` 覆盖数据目录，或在 CLI 中用 `--db` 指定数据库。

## 测试

```powershell
python -m pytest -q
```

测试全部使用 `tmp_path` 下的独立数据库，覆盖成功/失败/stderr、中文空格路径、超时、
取消、3 MB 流式输出、持久化/搜索/筛选/导出、行差异、脱敏、非 Git 目录、异常恢复、
复现包校验与确认门槛、CLI 及 PySide6 缺失回退。详细结果见
[`docs/test-report.md`](docs/test-report.md)。

## 已知限制

- 日志按行解码：优先 UTF-8，失败时回退系统本地编码（中文 Windows 为 GBK），同一任务混合编码的输出也能还原；超过 64 KiB 的无换行数据按块解码。子程序自身的输出缓冲
  也会影响“实时”程度，RunTrace 只能展示程序实际 flush 的内容。
- 记录的是运行时的合并环境快照；敏感值被替换为 `***`，模板/再次运行不会恢复密钥。
  这是安全设计，不是自动注入凭证的功能。
- Git 信息是只读快照，不记录外部服务、网络、依赖版本、交互输入、未跟踪的运行时
  文件状态；这些在复现包中会列为缺口。
- 复现包只做完整性校验（SHA-256，自描述，不是数字签名），不打包第三方可执行文件；
  不要执行来源不明的包。
- 日志默认永久保留，当前没有自动清理/轮转 UI；需要时由用户自行管理数据库。
- SQLite 是本机单文件存储，非多机协作/服务端方案；没有多用户权限模型。
- shell 模式存在注入与转义风险，RunTrace 只能提示，不能保证任意命令安全。
- PySide6 只是可选 UI；自动化测试和核心功能均不依赖它。

## 安全说明

- 环境变量名包含 `TOKEN`、`SECRET`、`PASSWORD`、`KEY`、`AUTH`、`CREDENTIAL`
  等片段时，默认在持久化前替换为 `***`。核心 API 可通过 `allow_plain_keys`
  显式覆盖，UI 不提供关闭脱敏的入口。
- 导出和复现包生成前会扫描日志/命令中的高置信度密钥模式；发现时在结果或界面中
  警告。扫描是启发式的，不能保证不漏报/不误报，分享前仍需人工检查。
- 命令默认使用参数数组执行（`shell=False`）。shell 模式必须显式选择，UI 与导出
  清单均给出风险提示。
- 复现包执行前必须人工确认（GUI 二次确认，CLI 使用 `--yes` 或交互输入 `YES`）。
- 应用不安装依赖、不请求管理员权限、不执行提权命令；Git 查询不会修改仓库。
- 数据库和导出文件是本地明文数据，请按组织安全策略保护；不要提交数据库或日志。
