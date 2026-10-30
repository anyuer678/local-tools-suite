# EnvForge 风险登记册

> 审计基准：2026-10-30，版本 0.1.0，114 项测试全绿。
> 只登记**代码/发布流程中可验证的风险**。每条都给出证据位置、复现方式、修复建议与
> 验收标准，便于直接拆成开发任务。

## 0. 分级与状态

| 项目 | 含义 |
| --- | --- |
| P0 | 会造成**数据丢失或环境被改坏**，应尽快修 |
| P1 | 有明确用户可见错误或一致性缺口，排入下个迭代 |
| P2 | 体验、可维护性或发布层面的问题，可排期 |
| 状态 | `已修复`（本轮 P0-1～P0-4 已处理）/ `待修复` / `接受`（有意权衡，记录在案） |

本轮已闭环的 4 项（原登记为 P0，现已修复并补齐测试）：

| 编号 | 问题 | 修复 | 测试 |
| --- | --- | --- | --- |
| D-01 | 写入成功与否不做校验，失败后环境处于半改状态 | 写后回读逐键比对，不一致即用写前快照回滚，抛 `EnvWriteFailure`（含 `partial` / `rollback_ok`） | `test_apply_marks_verified`、`test_apply_rolls_back_on_write_exception`、`test_apply_rolls_back_on_readback_mismatch` |
| D-02 | 没有任何操作记录，出问题无法追溯 | JSONL 审计（只记键名/结果/快照号）+ GUI「操作历史」+ `audit list` | `test_audit_records_keys_but_not_values`、`test_audit_records_failed_write_as_rolled_back`、`test_cli.py::test_audit_list_shows_actions_without_values` |
| D-03 | 数据库损坏只能靠用户自求多福；无独立备份 | 打开时 `quick_check` + 每日 SQLite 在线备份（保留 7 份）+ `env db-check/db-backup` | `test_integrity_check_on_healthy_database`、`test_backup_to_creates_consistent_copy`、`test_backup_daily_creates_once_per_day` |
| D-04 | 两个实例并发写同一注册表键会互相覆盖 | msvcrt 跨进程文件锁 + 进程内锁，超时中文提示 | `test_apply_rejects_concurrent_writer` |

## 1. 真实缺陷（已确认，待修复）

### R-01 `restore(delete_extras=True)` 按大小写敏感比较，可能误删变量

- **级别**：P1 **状态**：待修复
- **证据**：[envstore.py:382](../src/envforge/envstore.py#L382-L389)
- **现象**：快照里存的是 `Path`，注册表当前枚举出的是 `PATH`。回滚时
  `key not in snapshot.values` 判定为「快照中不存在」，`delete_extras` 会执行
  `DeleteValue("PATH")`——而 Windows 注册表值名不区分大小写，变量被真实删除。
- **复现**：构造 Provider 返回 `{"PATH": "..."}`，快照由 `{"Path": "..."}` 生成，
  调用 `restore(id, confirm=True, delete_extras=True)`，断言 PATH 消失。
- **修复**：比较前统一 `key.upper()` 建集合；写入时沿用现有键名的真实大小写。
- **验收**：新增用例「大小写不同的同名变量 + delete_extras」回滚后变量仍存在，
  且 `changed` 列表为空（无需无意义重写）。

### R-02 GUI 写入在 UI 线程执行，锁等待可达 8 秒

- **级别**：P1 **状态**：待修复
- **证据**：[qt_app.py:1247](../src/envforge/ui/qt_app.py#L1247-L1252)、
  [envstore.py:220](../src/envforge/envstore.py#L220-L240)
- **现象**：另一实例持锁时，点击「写入」会让主线程阻塞到 `lock_timeout=8s` 超时，
  Windows 会把窗口标记为「无响应」。
- **复现**：用一个进程持锁 9 秒，另一个 GUI 实例点写入；窗口出现未响应。
- **修复**：把写入放进 `_Worker`（已有线程设施），锁超时降到 2～3 秒并在失败时
  提供「重试」按钮；写入结果通过信号回主线程刷新。
- **验收**：锁被占用时 GUI 仍可拖动/切换页签；超时提示含「稍后重试」。

### R-03 审计写入失败只置 `last_error`，调用方从不检查

- **级别**：P2 **状态**：待修复
- **证据**：[audit.py:64](../src/envforge/audit.py#L64-L96)、
  [envstore.py:346](../src/envforge/envstore.py#L346-L365)
- **现象**：日志目录不可写（只读盘、权限策略）时审计静默丢失，用户以为有记录。
- **修复**：`apply` 成功但审计失败时，在返回结果里加 `audit_ok=False`，CLI/GUI 给
  一次性警告；或改为在 `doctor` 中增加「审计不可写」检查项。
- **验收**：把日志目录设为只读后执行写入，CLI 输出警告且退出码仍为 0（业务成功）。

### R-04 数据库完整性失败后仍允许继续写入

- **级别**：P1 **状态**：接受（有意权衡）→ 建议下一迭代改为只读模式
- **证据**：[db.py:121](../src/envforge/db.py#L121-L122)、
  [cli.py:315](../src/envforge/cli.py#L315-L321)、[qt_app.py:1477](../src/envforge/ui/qt_app.py#L1477-L1484)
- **现象**：`quick_check` 失败时只弹警告/打印提示，随后仍可 `apply`、`profile set`，
  损坏可能被进一步写入放大。
- **修复**：完整性失败时把 `Database` 标记为 `read_only`，写方法抛
  `DatabaseError`（中文提示恢复备份后再写）；GUI 只保留查看与体检。
- **验收**：人为截断 db 文件后，`env snapshot`（只读）成功，`env apply` 明确失败
  且提示备份目录。

### R-05 敏感键正则存在漏判

- **级别**：P2 **状态**：待修复
- **证据**：[models.py:14](../src/envforge/models.py#L14-L19)
- **现象**：`WEBHOOK_URL`、`PASS`、`CERT`、`COOKIE`、`SESSION_ID` 等不含现有关键词，
  会被当作普通值明文展示/导出。
- **修复**：补充关键词（`WEBHOOK|COOKIE|SESSION|CERT|PASS$|KEY$` 需谨慎避免误伤），
  并在 GUI 保存时允许用户勾选「按敏感值处理」。
- **验收**：`library` 敏感标记测试扩展 5 个新样例；误判样例（如 `KEYBOARD_LAYOUT`）
  仍为普通值。

### R-06 审计日志跨进程追加没有锁

- **级别**：P2 **状态**：待修复
- **证据**：[audit.py:76](../src/envforge/audit.py#L76-L84)（只有 `threading.Lock`）
- **现象**：GUI 与 CLI 同时写日志时，极端情况下可能丢行或行内容交错。
- **修复**：复用 [lock.py:40](../src/envforge/lock.py#L40-L100) 的 `file_lock` 包住
  `append`（超时 1 秒，失败降级为直接写）。
- **验收**：两个进程并发写 200 条，日志行数等于 200 且每行可被 JSON 解析。

## 2. 潜在风险（设计内或环境相关）

| 编号 | 风险 | 影响 | 证据 | 缓解 |
| --- | --- | --- | --- | --- |
| P-01 | `WM_SETTINGCHANGE` 广播失败被静默忽略 | 已启动的资源管理器/终端不刷新，新开进程才看到变更 | [envvars.py:206](../src/envforge/envvars.py#L206-L216) | 写入结果里说明「需新开终端」；下次可加手动「刷新环境」按钮 |
| P-02 | DPAPI 密文绑定 Windows 用户与机器 | 换机/换账户后敏感值不可解密（显示为空，不是明文） | [secrets.py:112](../src/envforge/secrets.py#L112-L160) | 已在 UI/文档说明；导出需显式 `--reveal` |
| P-03 | 备份与数据库同目录、同磁盘 | 磁盘故障或同步冲突时备份同时失效 | [db.py:213](../src/envforge/db.py#L213-L238) | 文档建议把 `backups/` 指向另一块盘（`--db` 自定义目录即可） |
| P-04 | GUI 后台线程与主线程共享单个 sqlite 连接 | 长备份/大查询会短暂阻塞另一线程（有 RLock，无数据竞争） | [db.py:113](../src/envforge/db.py#L113-L119)、[db.py:155](../src/envforge/db.py#L155-L159) | 库规模小、备份毫秒级；库增长后改用连接池或只读连接 |
| P-05 | `--reveal` 把明文输出到终端与剪贴板 | 可能进入 shell 历史/剪贴板管理器 | [cli.py:522](../src/envforge/cli.py#L522-L526)、[cli.py:466](../src/envforge/cli.py#L466-L468) | 已有确认语义；后续可加「仅显示 10 秒」 |
| P-06 | 变量值长度无上限 | 超长值写入注册表失败 | [envstore.py:240](../src/envforge/envstore.py#L240-L296) | 现在会回滚并报告失败键，不会留下半改状态 |
| P-07 | 审计/崩溃日志含文件路径与变量名 | 日志被他人读取时泄露「键名拓扑」（无值） | [audit.py:64](../src/envforge/audit.py#L64-L96) | 文档明确说明；后续可加「关闭审计」开关 |
| P-08 | 安装器未签名 | SmartScreen「未知发布者」 | [packaging.md](packaging.md#L97-L98) | 发布说明中提示；后续购买代码签名证书 |

## 3. 未来需求（功能缺口）

| 编号 | 需求 | 为什么需要 | 引入的新风险 | 验收标准 |
| --- | --- | --- | --- | --- |
| R-F01 | 从 `.env` / JSON 导入档案 | 用户已有配置文件，手工重录成本高 | 路径穿越、变量名注入、超大文件 | 导入前预览差异；拒绝非法键名/超长值；单文件上限 2 MB；有对应测试 |
| R-F02 | system 作用域写入 | 部分团队需要机器级变量 | 提权、UAC、误改全机环境 | 独立按钮 + 二次确认 + 自动备份；提权失败有中文说明；测试用假 Provider |
| R-F03 | 运行时自动下载/安装 | 新机初始化仍需手工装 Python/Node | 供应链、磁盘占用、卸载残留 | 校验 SHA256 与签名；安装到独立目录；可一键卸载；离线可用 |
| R-F04 | 备份一键恢复向导 | 现在只能手工替换 db 文件 | 覆盖当前库导致新数据丢失 | 恢复前自动备份当前库；列出备份时间/大小；恢复后自动 `db-check` |
| R-F05 | 快照保留与归档 | 快照只增不减，长期使用会膨胀 | 误删仍需的回滚点 | 默认保留 50 条/90 天；删除前提示；CLI `env snapshots --prune` |
| R-F06 | 自动更新 / 多语言 / Dashboard | 分发与规模化使用 | 更新通道被劫持 | 更新包签名校验；可关闭自动更新；失败可回退旧版本 |
| R-F07 | 审计导出（CSV/JSON）与合规留存 | 团队审计需要带走记录 | 导出文件含键名 | 只导出键名/动作/时间；导出需确认；可设置保留天数 |
| R-F08 | GUI 端到端自动化（pytest-qt） | 现在 GUI 覆盖率 50%，回调靠人工 | 测试不稳定 | 关键按钮路径（写入/回滚/操作历史）纳入 CI，稳定性 ≥ 99% |

## 4. 复核方式

```powershell
cd EnvForge
python -m pytest -q
python -m pytest -q --cov=envforge --cov-report=term-missing
envforge env db-check
envforge audit list --limit 20
```

风险条目与 [FEATURE_MATRIX.md](FEATURE_MATRIX.md) 的编号一一对应；修复任一条时，
请同时更新两份文档的「状态」列。
