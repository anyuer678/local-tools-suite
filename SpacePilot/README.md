# SpacePilot —— Windows 磁盘空间分析与安全治理平台

SpacePilot 是一个面向 Windows 10/11 的本地磁盘空间分析与安全治理工具。它遵循固定顺序：

> **先发现问题 → 再解释原因 → 最后提出方案**

所有结论都来自实际文件系统证据（路径、大小、时间、哈希、inode），并保存进本地 SQLite 快照，可随时复查。
程序**默认只读**：扫描、分析、建议都不会改动任何文件；删除 / 移动 / 隔离必须由用户看到预览并明确确认后才会执行。

- 核心逻辑仅使用 Python 标准库（`os.scandir`、`pathlib`、`hashlib`、`sqlite3`、`threading`、`shutil` 等）。
- 图形界面使用 PySide6，且**惰性导入**；未安装 PySide6 时自动回退到功能等价的 CLI，不会崩溃。
- 运行环境：Python 3.12，Windows 10/11（其他平台可运行核心与测试，但路径保护规则面向 Windows）。

---

## 1. 功能一览

### 1.1 扫描（只读）

- 选择根目录，支持排除规则：目录名、文件后缀、最小文件大小。
- 使用 `os.scandir` 迭代式深度优先遍历，不一次性把整盘文件对象读入内存。
- 统计文件数、目录数、总大小、目录占用、类型（后缀）分布。
- 后台线程执行 + 进度回调 + 取消事件；取消后仍会保存已扫描的部分结果。
- 明确区分 **scanned / skipped / failed**：
  - `skipped`：被排除规则过滤、符号链接（默认不跟随）等。
  - `failed`：权限不足、路径过长、目录读取失败、`stat` 失败等。失败会记录为条目，不会“凭空消失”。
- 符号链接默认不跟随并记录原因；跟随时可检测循环引用（基于 `(st_dev, st_ino)`）。
- 扫描期间文件发生变化时，通过前后两次 `stat` 对比标记 `volatile`。
- 未知大小、0 字节文件单独计数。
- 条目按批（默认 2000 条）写入 SQLite，内存不堆积全部记录。
- 扫描结束记录开始/结束时间、耗时、失败数、完整度。

### 1.2 可视化数据

- 目录树聚合大小（逐层汇总子目录）。
- 最大文件、最大目录、类型分布。
- 命令行支持排序、过滤、仅输出路径便于复制。

### 1.3 快照与变化追踪

- 每次扫描保存为独立快照（`scan_id`），历史快照可列出、可比较、可删除。
- 比较两个快照可识别：
  - 新增（ADDED）
  - 删除（DELETED）
  - 变大（GROWN）
  - 变小（SHRUNK）
  - 改名（RENAMED，同大小 + 同哈希，文件名变化）
  - 移动（MOVED，同大小 + 同哈希，路径变化）
  - 内容修改（MODIFIED，同大小但哈希不同，或大小不同）
  - 目录移动（整目录换位置）
- 目录净增长统计，并给出主要增长来源（按父目录聚合正增量）。
- 可为目录设置“增长观察目标”，超过阈值时生成告警与复核建议（界面“快照对比”页的“观察目录…”按钮，或 CLI `watch`）。

> 移动 / 改名识别依赖扫描时保存的 SHA-256。若扫描未开启哈希，工具会**如实**降级为“新增 + 删除”，并在结果中注明“缺少哈希，无法确认移动”，不会猜测。

### 1.4 重复文件检测

- 先按文件大小分组（默认仅检测 ≥ 4096 字节的文件），再对同大小组计算 SHA-256。
- 区分：
  - 完全重复（内容相同，可能同名也可能不同名）。
  - 同名但内容不同（不是重复）。
- 硬链接处理：`st_nlink > 1` 或相同 `(device, inode)` 的条目视为同一份数据，**不重复计入可回收空间**。
- 输出潜在可回收空间（保留一份，其余视为可回收）。
- 哈希结果会回填到快照数据库，便于后续差异比较复用。

### 1.5 安全治理

- 生成清理建议，每条建议包含：证据（路径、大小、哈希、重复组、增长数据）、预期收益、风险等级（low / medium / high）、建议动作。
- 执行流程严格固定：**生成预览 → 展示计划与令牌 → 用户确认 → 执行 → 写日志 → 复核结果**。
- 默认仅支持只读分析、隔离（quarantine）、用户明确指定的移动。
- 删除必须显式开启（`allow_delete=True`），且：
  - 禁止系统关键目录（Windows / System32 / Program Files / ProgramData / 驱动器根目录 / 用户根目录等）。
  - 高风险（如 ≥1GB 的未知文件）不允许删除，只能复核。
  - 不允许自动处理未知文件。
- 回滚只对**隔离副本**承诺有效：隔离是复制 + 校验，原文件保持不动，可随时回滚。
- **删除不可撤销**；移动也仅记录结果，不承诺由工具回滚。

### 1.6 工程能力

- SQLite 事务、WAL、`busy_timeout`、索引、`PRAGMA user_version` schema 迁移。
- 审计日志（JSONL，每个操作与整次执行各一条）。
- 导出 JSON / CSV。
- 排除规则可通过 JSON 配置文件追加。
- 模块边界清晰，核心与 UI 解耦。

---

## 2. 安装

```powershell
cd SpacePilot

# 推荐：可编辑安装（安装后可直接使用 python -m spacepilot 与 spacepilot 命令）
python -m pip install -e .

# 仅安装测试依赖
python -m pip install -e ".[test]"

# 安装 GUI 依赖（可选）
python -m pip install -e ".[gui]"
# 或
python -m pip install -r requirements.txt
```

也可以不安装，直接使用源码运行（需要把 `src` 加入 `PYTHONPATH`）：

```powershell
$env:PYTHONPATH = "src"
python -m spacepilot --help
```

---

## 3. 快速开始

### 3.1 图形界面

```powershell
python -m spacepilot          # 无参数时优先启动 GUI
python -m spacepilot --gui    # 显式启动 GUI
```

GUI 主要区域：

- 顶部：根目录选择、排除目录、排除后缀、最小文件大小、是否计算 SHA-256、是否跟随符号链接、开始 / 取消。
- 标签页：
  1. **概览**：扫描统计、完整度、失败项、最大目录 / 文件
  2. **目录树**：分层目录大小
  3. **类型分布**：后缀与占用
  4. **重复文件**：重复组、可回收空间
  5. **清理建议**：证据、收益、风险等级；可对选中项发起隔离
  6. **快照对比**：选择两个快照查看变化
- 隔离操作会弹窗展示完整预览与预览令牌，只有点击确认后才会复制到隔离目录；GUI 不提供删除功能。

如果 PySide6 未安装或 Qt 平台插件加载失败，程序会打印 CLI 帮助并继续以 CLI 模式工作。

### 3.2 命令行

```powershell
# 查看帮助
python -m spacepilot --help

# 扫描 D:\Data 并保存快照（排除缓存目录与临时后缀，忽略小于 1MB 的文件）
python -m spacepilot scan "D:\Data" --exclude-dir node_modules --exclude-suffix .tmp --min-size 1048576

# 扫描并计算 SHA-256（之后才能识别改名/移动）
python -m spacepilot scan "D:\Data" --hash --top 20 --json-out report.json --csv-out entries.csv

# 查看历史快照
python -m spacepilot snapshots --limit 20

# 按大小排序查看最大的文件；--copy 只输出绝对路径
python -m spacepilot files --min-size 104857600 --sort size --limit 30
python -m spacepilot files --suffix .log --copy

# 比较两个快照（ID 支持前缀）
python -m spacepilot compare 3f9c1a 7b2d40

# 检测重复文件
python -m spacepilot duplicates --min-size 4096 --csv-out duplicates.csv

# 生成带证据的清理建议
python -m spacepilot suggest --json-out suggestions.json

# 目录增长观察
python -m spacepilot watch --add "Downloads" --threshold 104857600
python -m spacepilot watch --list
python -m spacepilot watch --check 3f9c1a

# 生成隔离计划（只生成预览与令牌，不改动文件）
python -m spacepilot quarantine "D:\Data\cache\old.bin" --dest "D:\Quarantine" --root "D:\Data" --save-plan plan.json

# 执行已保存的隔离计划（必须携带预览令牌）
python -m spacepilot quarantine --dest "D:\Quarantine" --execute-plan plan.json --token "<令牌>"

# 回滚某次隔离执行（仅隔离副本）
python -m spacepilot rollback <execution_id>
```

> 删除功能只能在代码中显式构造 `Executor(allow_delete=True)` 才会生效；CLI 与 GUI 默认都不提供删除入口。

---

## 4. 数据与状态位置

默认状态目录：`%LOCALAPPDATA%\SpacePilot`（非 Windows 回退到 `~/.spacepilot`）。

| 内容 | 默认路径 |
| --- | --- |
| 快照数据库 | `%LOCALAPPDATA%\SpacePilot\spacepilot.db` |
| 隔离目录 | `%LOCALAPPDATA%\SpacePilot\quarantine` |
| 执行审计日志 | `%LOCALAPPDATA%\SpacePilot\executions.jsonl` |

- 数据库路径可用全局参数 `--db <path>` 覆盖。
- 隔离目录可用 `quarantine --dest <path>` 覆盖。
- 日志路径可用 `--log <path>` 覆盖。
- 数据库为 WAL 模式，支持并发读取；写入通过事务与批处理完成。

---

## 5. 排除规则与配置文件

默认排除目录名：`$RECYCLE.BIN`、`System Volume Information`、`.git`、`.svn`、`node_modules`、`__pycache__`。
默认不会排除任何后缀；最小文件大小默认 0。

可通过 JSON 配置文件**追加**规则（不会整体覆盖默认值）：

```json
{
  "exclude_dirs": ["build", "dist", "Temp"],
  "exclude_suffixes": [".tmp", ".log", ".bak"],
  "min_file_size": 1048576
}
```

```powershell
python -m spacepilot scan "D:\Data" --config rules.json
```

命令行参数 `--exclude-dir` / `--exclude-suffix` / `--min-size` 会与该配置合并（取并集；最小大小取较大值）。

---

## 6. 安全模型（重要）

### 6.1 默认只读

扫描、统计、重复检测、建议生成都不写入用户文件，只写快照数据库与用户指定的导出文件。

### 6.2 预览 + 令牌 + 确认

执行任何操作前必须走完三步：

1. `build_*_plan()` 生成计划，计算预览令牌（对计划内容的 SHA-256，规范化 JSON）。
2. 用户阅读预览（包含源路径、目标路径、大小、风险等级、令牌）。
3. `execute(plan, confirm=True, token=<令牌>)`。

执行器会再次校验：

- `confirm` 必须为 `True`；
- 令牌必须匹配；
- 重新计算令牌，确保计划在预览后没有被篡改；
- 源文件仍存在、是普通文件、不是符号链接、大小未变化；
- 隔离目标必须位于隔离根目录内；
- 移动的源 / 目标不得是系统关键路径，目标不得已存在；
- 删除需要 `allow_delete=True`，且风险等级不是 high、路径不是关键路径。

任一项不满足都会抛错，且**不会修改任何文件、不会写执行日志**。

### 6.3 隔离与回滚

- 隔离 = 复制到隔离目录 + 校验大小与 SHA-256，**原文件保持不动**。
- 回滚某次隔离：
  - 若原文件仍在：校验隔离副本哈希后清理隔离副本；
  - 若原文件已不存在：复制回原位置并校验；
- 移动 / 删除操作不承诺回滚。

### 6.4 删除不可撤销

> **删除是不可撤销的。** 工具只为“隔离副本”承诺回滚能力；一旦执行删除，数据无法通过本工具恢复。
> 请优先使用隔离，确认无影响后再由用户自行决定是否清理。

---

## 7. 架构与模块边界

```
src/spacepilot/
├── __init__.py          版本号，不导入任何 UI 依赖
├── __main__.py          入口：优先 GUI（惰性导入），失败回退 CLI
├── models.py            枚举与数据类、人类可读大小格式化
├── scanner.py           扫描引擎（迭代 DFS、批处理、取消、volatile、符号链接）
├── snapshot_store.py    SQLite 存储（事务、索引、schema 迁移、查询聚合、导出）
├── duplicates.py        重复文件检测（大小分组 + SHA-256 + 硬链接去重）
├── diff_engine.py       快照比较（新增/删除/增缩/改名/移动/修改/目录移动/净增长）
├── policy.py            排除规则、系统关键路径保护、分类、建议、增长观察
├── executor.py          计划构建、令牌、预校验、隔离/移动/删除、日志、回滚
├── cli.py               命令行接口与结果展示
└── ui/
    ├── __init__.py      空包，不含 PySide6 导入
    └── qt_app.py        唯一导入 PySide6 的模块（QThread 后台任务、6 个标签页）
```

数据流：

```
扫描 → SnapshotStore(SQLite) → 查询聚合 / 重复检测 / 差异比较 → 建议 → 计划 → 确认 → 执行 → 日志/回滚
```

关键边界：

- `scanner` 不依赖 UI，只依赖 `models` / `snapshot_store` / `policy`。
- `diff_engine` 只读快照，必要时读盘计算哈希；不会修改文件。
- `executor` 是唯一会修改文件系统的模块，且必须经过计划、令牌、确认与预校验。
- `ui/qt_app.py` 是唯一导入 PySide6 的文件；删除 PySide6 不会影响核心与 CLI。

---

## 8. 数据库与迁移

- `SCHEMA_VERSION = 2`，通过 `PRAGMA user_version` 记录版本。
- 主要表：`scans`、`entries`、`dirs`、`watch_targets`。
- 索引覆盖：扫描 ID + 相对路径、状态、大小、后缀、inode、哈希。
- 迁移：
  - 版本 0（新库）→ 创建完整 schema；
  - 版本 1 → 增加 `entries.hash` 列、创建 `watch_targets` 表，升级到 2；
  - 版本高于程序支持值时拒绝打开并提示升级。
- 重复打开、关闭后重开数据保持一致；测试覆盖迁移保留数据与完整性检查。

---

## 9. 测试

```powershell
python -m pytest -q
```

当前测试覆盖：

- 已知大小的目录树统计、目录聚合、类型分布；
- 排除规则、最小大小、0 字节、不可读文件 / 目录记为 failed；
- 取消扫描后保存部分结果；
- 符号链接默认 skipped、volatile 检测；
- 差异：新增 / 删除 / 变大 / 变小 / 改名 / 移动 / 同大小内容修改 / 目录移动；
- 无哈希时不猜测移动；
- 重复文件：同内容不同名、同名不同内容、硬链接不计入可回收空间、最小阈值；
- 策略：规则规范化、关键路径、文件分类、增长观察、建议证据；
- 执行器：未确认 / 错误令牌 / 计划后文件变化 / 越界目标均不修改文件；隔离复制后可回滚；移动不承诺回滚；删除需显式开启且拒绝高风险与关键路径；
- 存储：schema 版本、索引、重开一致、v1 → v2 迁移、观察目标 upsert；
- CLI 端到端与 `python -m spacepilot --help`；
- GUI 离屏冒烟（`QT_QPA_PLATFORM=offscreen`）。

详细结果见 [docs/test-report.md](docs/test-report.md)。

---

## 10. 已知限制

- 差异比较会加载旧快照的一份文件字典，新快照改为流式读取；峰值约为“旧快照 + 变更集”，不再是两份完整字典。数百万文件的超大快照仍建议分目录或分批比较。
- 改名 / 移动识别依赖扫描时保存的 SHA-256；未开启哈希时只能如实报告“新增 + 删除”。
- 硬链接识别依赖文件系统提供有效 inode；少数文件系统 inode 为 0 时无法识别。
- 回滚只对隔离副本有效；移动与删除不可由本工具回滚，**删除不可撤销**。
- Windows 路径按 `casefold` 规范化匹配，仅改变大小写的改名可能被视为移动。
- 大型目录树的 GUI 渲染仍可能较慢；建议先用 CLI 过滤再处理。
- 当前不解析 NTFS 压缩 / 稀疏文件的物理占用，大小按逻辑字节统计。
- 系统关键路径保护基于环境变量与固定规则，不作为对抗恶意改名的安全边界。

---

## 11. 打包

PyInstaller 打包步骤、数据目录约定与发布检查清单见 [docs/packaging.md](docs/packaging.md)。

---

## 12. 许可

本仓库为本地工具示例项目，未附带许可证文件；如需分发请自行补充许可证。
