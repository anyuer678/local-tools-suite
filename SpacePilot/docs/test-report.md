# SpacePilot 测试报告

## 1. 测试环境

| 项 | 值 |
| --- | --- |
| 操作系统 | Windows 11 10.0.26200 |
| Python | 3.12.3 (64-bit, MSC v.1938) |
| pytest | 9.0.3（项目要求 `pytest>=8.0`） |
| 项目版本 | SpacePilot 0.1.0 |
| 数据库 | 内存 SQLite / 临时文件 SQLite（`tmp_path`） |
| GUI 测试 | `QT_QPA_PLATFORM=offscreen` 离屏模式 |
| 测试根目录 | `SpacePilot`（仓库内相对路径） |

所有测试均使用 pytest 的 `tmp_path` 创建真实文件，不依赖开发者机器的既有文件；权限失败场景通过 monkeypatch `os.stat` / `os.scandir` 构造。

## 2. 执行命令与结果

```powershell
cd SpacePilot
python -m pytest -q
```

结果：

```
................................                                         [100%]
32 passed in 1.07s
```

另外执行：

```powershell
python -m spacepilot --help
```

退出码 0，输出包含 `scan`、`snapshots`、`files`、`compare`、`duplicates`、`suggest`、`watch`、`quarantine`、`rollback` 等子命令，证明 CLI 入口在无 GUI / 无参数场景下可正常工作。

GUI 离屏冒烟测试（`tests/test_ui.py`）实测输出：

```
tabs: 6
tree top: 1
gui smoke ok
```

## 3. 测试文件与覆盖点

| 测试文件 | 用例数 | 覆盖内容 |
| --- | --- | --- |
| `tests/test_scanner.py` | 7 | 统计、聚合、排除、失败记录、取消、符号链接、volatile |
| `tests/test_diff.py` | 2 | 全部变化类型、无哈希时降级策略 |
| `tests/test_duplicates.py` | 3 | 重复判定、同名不同内容、硬链接去重、最小阈值 |
| `tests/test_policy.py` | 5 | 排除规则规范化、关键路径、文件分类、增长观察、建议证据 |
| `tests/test_executor.py` | 9 | 确认/令牌/预校验/隔离/移动/删除/回滚 |
| `tests/test_store.py` | 3 | schema、索引、重开一致、v1→v2 迁移、观察目标 |
| `tests/test_cli.py` | 2 | CLI 端到端、模块帮助 |
| `tests/test_ui.py` | 1 | 离屏 GUI 冒烟 |
| **合计** | **32** | |

### 3.1 `test_scanner.py`

- `test_scan_counts_and_aggregates`：构造已知层级目录树（6 个文件、390 字节、5 个目录，含 0 字节文件与空目录），断言文件数、总大小、目录聚合大小（root=390、a=280、a/deep=200、b=10、empty=0）、类型分布。
- `test_exclude_rules_and_min_size`：目录名 / 后缀排除与最小大小过滤后，被排除项不进入统计。
- `test_unreadable_file_is_failed_not_missing`：monkeypatch `os.stat` 抛 `PermissionError`，文件被记为 `failed`，`size=None`，`unknown_size` 计数增加，而不是消失。
- `test_unreadable_directory_is_recorded`：monkeypatch `os.scandir` 抛 `PermissionError`，目录以失败条目保存。
- `test_cancel_saves_partial_results`：扫描 80 个文件过程中触发取消，已落库条目数 > 5 且 < 80，证明取消后停止并保存部分结果。
- `test_symbolic_link_skipped_by_default`：默认不跟随符号链接，记录为 skipped；平台不支持创建符号链接时 `skip`。
- `test_volatile_file_detected`：monkeypatch 第二次 `stat` 修改 `mtime`，条目被标记 `volatile`。

### 3.2 `test_diff.py`

- `test_diff_detects_all_change_types`：
  - 新增、删除、变大、变小；
  - 改名（同大小 + 同哈希，basename 变化）→ `RENAMED`；
  - 同大小内容修改 → `MODIFIED`，证据包含新旧 SHA-256；
  - 目录整体移动 → `dir_moves` 记录 old/new 与文件数；
  - 断言无“缺少哈希”提示（因为扫描已开启哈希）。
- `test_diff_without_hashes_does_not_guess_moves`：未计算哈希时，不把同大小文件猜成移动，只报告 `ADDED` + `DELETED`，并在结果中给出“缺少 SHA-256”说明。

### 3.3 `test_duplicates.py`

- `test_duplicates_same_content_and_hardlinks`：同内容不同名文件被判为重复；硬链接（`os.link`）只算一份，`hardlink_skipped` 计数，可回收空间不重复计算；平台不支持硬链接时 `skip`。
- `test_same_name_different_content_not_duplicate`：同名但内容不同，不构成重复组。
- `test_min_size_excludes_small_files`：小于阈值的文件不参与检测。

### 3.4 `test_policy.py`

- `test_exclude_rules_normalization`：目录名、后缀大小写规范化，路径匹配不区分大小写。
- `test_critical_paths`：Windows / Program Files / 驱动器根目录 / 用户目录等被识别为关键路径。
- `test_classify_file_rules`：临时目录中的临时文件 → low/隔离；备份目录日志 → medium/隔离；≥1GB 未知文件 → high/复核。
- `test_watch_target_detects_growth`：观察目标基线 100 → 200，超阈值触发告警；增长建议标记 `requires_review`。
- `test_duplicate_recommendation_has_evidence`：重复组建议包含 SHA-256 证据与收益；临时文件建议正确生成。

### 3.5 `test_executor.py`

- `test_quarantine_requires_confirm_and_token`：未确认或令牌错误时，文件不变且不写日志。
- `test_quarantine_copy_verify_and_rollback`：保存计划 → 加载计划 → 携带令牌执行；原文件仍在、隔离副本存在且校验通过、日志记录 SHA-256；回滚后副本清理、`restored=1`。
- `test_move_plan_and_rollback_not_promised`：移动执行成功；回滚结果明确标记 skipped（不承诺回滚移动）。
- `test_delete_requires_explicit_allow`：`allow_delete=False` 拒绝删除；显式开启后成功。
- `test_delete_refuses_high_risk_unknown`：高风险未知文件拒绝删除。
- `test_delete_refuses_critical_path`：monkeypatch 关键路径后拒绝删除。
- `test_plan_detects_file_change_after_preview`：预览后文件被修改（大小变化），执行抛 `UnsafeOperation`，文件不变、无日志。
- `test_quarantine_target_must_stay_inside_root`：隔离目标越界（不在隔离根目录内）被拒绝。
- `test_null_confirm_never_modifies_even_with_valid_token`：即使令牌正确，`confirm=False` 也绝不修改文件。

### 3.6 `test_store.py`

- `test_fresh_schema_and_reopen`：新库 `schema_version=2`，索引存在；关闭重开后数据一致；JSON / CSV 导出可用。
- `test_migrate_v1_to_v2_preserves_data`：用 `SCHEMA_V1_SQL` 构造 v1 库，打开时迁移到 v2，原有数据保留、`hash` 列存在、完整性检查通过。
- `test_watch_targets_upsert`：观察目标重复写入时更新而非重复插入。

### 3.7 `test_cli.py`

- `test_cli_end_to_end`：真实调用 CLI 完成 scan → JSON/CSV 导出 → 修改文件 → 再次 scan → snapshots → compare → duplicates → suggest → files `--copy`，验证输出与退出码。
- `test_module_help_runs_without_gui`：子进程执行 `python -m spacepilot --help`，退出码 0，输出包含 `scan` 与 `duplicates`。

### 3.8 `test_ui.py`

- `test_main_window_smoke`：设置 `QT_QPA_PLATFORM=offscreen`，构造内存数据库与扫描结果，实例化 `MainWindow`，断言 6 个标签页、目录树顶层项、概览摘要非空；PySide6 缺失时通过 `importorskip` 跳过。

## 4. 手工验证记录

在安装为可编辑包后，手工执行完整链路：

1. `scan` 生成快照（开启 `--hash`）。
2. `quarantine --save-plan` 生成计划与预览令牌，确认文件未被修改。
3. `quarantine --execute-plan --token` 执行成功（`成功 1，失败 0`），原文件仍存在，隔离目录内出现副本。
4. `rollback <execution_id>` 恢复 1、跳过 0、失败 0，隔离副本被清理。

## 5. 结论

- 全部 32 个测试通过，无失败、无错误。
- 安全约束（未确认不修改、令牌校验、预校验、关键路径保护、删除不可撤销）均有测试覆盖。
- CLI 与 GUI 两条入口均可运行；GUI 在无显示环境下可被离屏实例化。
- 已知限制见 README 第 10 节，测试未覆盖超大规模数据集的性能表现。
