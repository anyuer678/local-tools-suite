# RunTrace 架构说明

- 版本：0.1.0
- 平台：Windows 10/11，Python 3.12
- 文档目的：说明模块边界、数据流、持久化模型、并发模型与安全策略，便于维护和二次开发。

## 1. 设计目标

RunTrace 解决的是“同一个命令，为什么这次和上次不一样”的问题。它把一次本地任务
运行中**可观测的事实**固定下来，并让这些事实可检索、可对比、可复现：

1. 记录：命令、参数、工作目录、脱敏后的环境覆盖、退出码、耗时、stdout/stderr。
2. 关联：项目路径与只读 Git 快照（分支、提交、是否有未提交改动）。
3. 检索：按项目/状态/时间筛选，日志全文搜索。
4. 对比：两次运行的行级差异与元数据差异，严格区分事实与推测。
5. 复现：导出带校验和的复现包，执行前必须人工确认。
6. 安全默认：参数数组 + `shell=False`，敏感环境值不落盘，导出前扫描疑似密钥。

非目标：不做远程协作、不管理依赖安装、不注入凭证、不做系统级监控。

## 2. 模块边界

源码位于 `src/runtrace/`，核心逻辑只依赖标准库；PySide6 仅用于可选界面。

| 模块 | 职责 | 不负责 |
|---|---|---|
| `command_builder.py` | 校验运行配置，构造参数数组或显式 shell 命令，生成安全预览与风险提示 | 不启动进程、不写数据库 |
| `process_runner.py` | 创建子进程、采集输出、超时/取消、进程树终止、落库状态 | 不做 UI、不解析业务语义 |
| `stream_capture.py` | 有限窗口缓冲，按 64 KiB 或定时批量写库 | 不知道 run 语义，只接收 writer 回调 |
| `run_store.py` | SQLite schema、事务、运行/日志/模板的 CRUD、搜索筛选、导出 | 不执行命令、不做差异计算 |
| `diff_engine.py` | 日志行级 diff 与元数据对比，输出 facts/speculations | 不读写数据库 |
| `replay_manager.py` | 复现包生成、SHA-256 清单、完整性校验、需确认的执行 | 不负责 UI 提示文案 |
| `project_context.py` | 只读获取 Git 分支/提交/脏标记 | 不提交、不修改仓库 |
| `redaction.py` | 环境变量脱敏、导出内容敏感模式扫描 | 不保证穷尽所有密钥格式 |
| `cli.py` | 参数解析、子命令分发、文本输出 | 不含核心业务规则 |
| `ui/qt_app.py` | 桌面界面：新建运行、日志流、历史、对比、导出、模板、复现包 | 不直接操作 SQLite 细节 |
| `__main__.py` | 启动分流：有 PySide6 起 GUI，否则回退 CLI | 不做业务逻辑 |

依赖方向是单向的：`ui`/`cli` → `process_runner`/`run_store`/`diff_engine`/
`replay_manager` → `command_builder`/`stream_capture`/`redaction`/
`project_context`。底层模块不反向导入上层，便于单独测试。

## 3. 运行时数据流

一次成功运行的时间顺序：

```text
用户配置
  └─ command_builder.validate_config()      校验 cwd、参数、超时、shell
  └─ command_builder.build_command()        生成 argv 与安全预览
        │
        ├─ project_context.get_git_context()  只读 Git 快照
        ├─ run_store.create_run()             插入 status=running，env 脱敏后落盘
        ├─ Popen(argv, cwd=..., env=os.environ+overrides,
                  stdout=PIPE, stderr=PIPE, creationflags=进程组)
        │     ├─ reader 线程 × 2              逐块读取，UTF-8 → 本地编码回退
        │     └─ stream_capture               64 KiB 窗口 / 0.25 s 定时 flush
        │           └─ run_store.append_logs() 批量事务写入 run_logs
        ├─ 等待：正常退出 / 超时 / 取消
        │     └─ _terminate_process_tree()    taskkill /T /F
        ├─ run_store.finish_run()             status、exit_code、ended_at、duration_ms
        └─ 可选导出 / 对比 / 复现包
```

关键点：

- `create_run` 是唯一入口，任何调用方都无法绕过脱敏写库。
- 日志分块写入，完整日志在 SQLite 中，内存只保留固定窗口。
- 停止不是杀线程，而是置位取消事件并由主流程终止子进程树，保证状态可判定。

## 4. 持久化模型

单文件 SQLite，默认 `%LOCALAPPDATA%\RunTrace\runtrace.db`；可用
`RUNTRACE_DATA_DIR` 或 CLI `--db` 覆盖。启用 WAL（失败时静默回退），
`foreign_keys=ON`，写操作使用 `RLock` + 事务。

### 4.1 `runs`

| 字段 | 说明 |
|---|---|
| `id` | 主键，`rt_<UTC 时间戳>_<随机后缀>` |
| `name` / `command` / `args_json` / `cwd` | 运行身份与调用方式 |
| `env_json` / `env_overrides_json` | 已脱敏的环境快照与覆盖项 |
| `shell` / `timeout` | 是否经系统 shell、超时秒数 |
| `status` / `exit_code` | `running/success/failed/timeout/cancelled/interrupted` |
| `started_at` / `ended_at` / `duration_ms` | UTC 微秒级时间与耗时 |
| `project_path` / `git_root` / `git_branch` / `git_commit` / `git_dirty` | 项目上下文 |
| `tool_version` / `metadata_json` | 版本、Python/平台、脱敏键名、recorded_at |

索引：`started_at DESC`、`status`、`project_path`、`cwd`，支撑历史列表与筛选。

### 4.2 `run_logs`

`id` 自增、`run_id` 外键（`ON DELETE CASCADE`）、`stream`、`content`、`created_at`。
索引 `(run_id, id)` 与 `(run_id, stream, id)`，保证按流顺序还原；删除运行会级联
删除日志。

### 4.3 `task_templates`

模板以 `name`（`COLLATE NOCASE` 唯一）标识，保存 cwd、command、args、脱敏后的
env_overrides、timeout、shell、project_path。`ON CONFLICT(name)` 实现覆盖保存，
`copy_template` 复用 `save_template`，不复制明文密钥。

### 4.4 迁移策略

首版 schema 幂等（`CREATE TABLE IF NOT EXISTS`）。升级时在 `RunStore.__init__`
追加迁移步骤；新增字段应带默认值，避免旧库无法打开。测试使用独立临时库，
不共享状态。

## 5. 并发与进程模型

- 主线程/UI 线程只做交互；每次运行由 `ProcessRunner.run()` 在独立线程中阻塞等待。
- 每个子进程有两条 daemon reader 线程（stdout、stderr），按块解码后交给
  `StreamCapture`。
- `StreamCapture` 内部持锁，达阈值或定时器到点即批量写库；关闭时强制 flush，
  保证最后一行不丢。
- 活动运行表 `run_id → cancel_event`、`run_id → Popen` 由 `RLock` 保护；
  `stop(run_id)` 只置位事件，实际的树终止由运行线程执行。
- Windows 下子进程以 `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW` 启动，
  终止使用 `taskkill /PID <pid> /T /F`，覆盖孙进程；非 Windows 回退进程组信号。
- SQLite 连接 `check_same_thread=False` 并全程加锁，允许 reader/flush 线程写入。

## 6. 安全与脱敏策略

1. **执行面**：默认 `shell=False`，参数以数组传递；shell 模式必须显式选择，
   UI 与导出清单都带风险提示，且不对不可信输入提供任何“安全保证”。
2. **持久化面**：`create_run` 与 `save_template` 强制调用 `redact_env`，变量名
   命中 `TOKEN/SECRET/PASSWORD/KEY/CREDENTIAL/AUTH` 等模式即替换为 `***`，
   并把被脱敏的键名记录到 metadata，便于复现时知道缺了什么。
3. **导出面**：`scan_sensitive_content` 对日志与命令做启发式密钥模式扫描，
   发现即报告位置与模式名，不隐藏、不静默修改内容。
4. **复现面**：包内 `checksums.json` 为自描述 SHA-256 清单（不是数字签名），
   执行前校验完整性，并要求显式确认（CLI `--yes`，GUI 二次确认）。
5. **只读面**：Git 查询设置 `GIT_OPTIONAL_LOCKS=0`、`GIT_TERMINAL_PROMPT=0`，
   仅执行只读子命令，不提交、不清理、不改写仓库。
6. **权限面**：应用不请求管理员权限、不安装依赖、不执行提权命令。

## 7. 失败处理与恢复

| 场景 | 行为 |
|---|---|
| 启动失败（命令不存在/权限不足） | `status=failed`，错误写入 stderr 日志，仍然完成 `finish_run` |
| 非零退出 | `status=failed`，保留真实 exit_code 与输出 |
| 超时 | `status=timeout`，终止进程树，记录耗时 |
| 用户取消 | `status=cancelled`，终止进程树 |
| 应用崩溃遗留 `running` | 启动时 `recover_interrupted_runs()` 标记为 `interrupted` 并补结束时间 |
| 数据库写入异常 | 事务回滚，异常向上抛出；不产生半条记录 |
| 复现包损坏/篡改 | `ReplayError`，拒绝执行 |
| 非 Git 目录 | 返回 `is_git=False` 与说明，不抛异常 |

## 8. 扩展方式

- 新增元数据字段：在 `runs` 表加列 + `create_run` 参数 + `_row` 映射，保持默认值。
- 新增 CLI 子命令：在 `cli.py` 注册解析器，调用现有 store/runner API，不复制逻辑。
- 新增对比维度：在 `diff_engine.py` 增加字段比较并归入 `facts` 或 `speculations`。
- 新增导出格式：复用 `run_store.export_*` 的读取接口，先扫描再序列化。
- 新增 UI 能力：`qt_app.py` 只调用核心 API；耗时任务放入线程并通过信号回到 UI 线程。

新增模块必须保持“底层不导入上层”，并为新行为补测试。

## 9. 已知限制

- 日志按行解码：优先 UTF-8，失败回退系统本地编码；混合编码可还原，但超过
  64 KiB 的无换行输出按块解码。
- “实时”受子进程自身缓冲影响，RunTrace 只能展示程序实际 flush 的内容。
- 环境是运行时快照；敏感值被替换为 `***`，模板/再次运行不会恢复凭证。
- Git 为只读快照，不记录网络、外部服务、依赖版本、交互输入等复现缺口。
- 复现包是完整性校验而非签名；应用不打包第三方可执行文件。
- 日志默认永久保留，没有自动轮转/清理 UI。
- SQLite 为本机单用户存储，无多用户权限模型，无服务端协作。
- PySide6 为可选依赖；无 GUI 环境自动回退 CLI。
