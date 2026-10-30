# EnvForge 功能完成度矩阵

> 审计基准：2026-10-30，代码版本 0.1.0，测试 114 项全绿。
> 本文件只描述**代码里真实存在的能力**；每一项都附代码位置与对应测试，
> 未实现或只有半成品的能力单列在「未完成能力」一节，不与已完成功能混排。

## 1. 状态定义

| 标记 | 含义 | 判定标准 |
| --- | --- | --- |
| ✅ | 完成 | 有服务层实现 + CLI/GUI 入口 + 自动化测试，且失败路径有明确错误提示 |
| ◐ | 部分完成 | 主路径可用，但缺少关键场景（如无自动化、只读、只覆盖单一作用域） |
| ✗ | 未实现 | 没有可用入口，或只是文档/口头规划 |

## 2. 功能矩阵

### 2.1 运行时版本

| 功能 | 状态 | 入口 | 代码证据 | 测试证据 | 说明 |
| --- | --- | --- | --- | --- | --- |
| 自动探测 PATH 中的运行时并登记 | ✅ | `runtime detect` / GUI「运行时版本」 | [runtimes.py:148](../src/envforge/runtimes.py#L148-L161) | test_runtimes.py | 支持 python/node/java/go/cargo 等，realpath 去重 |
| 手动登记运行时（含版本自动探测） | ✅ | `runtime add` | [runtimes.py:162](../src/envforge/runtimes.py#L162-L200) | test_runtimes.py | 校验路径存在且为文件；版本为空时保留旧版本 |
| 已登记运行时列表 / 移除 | ✅ | `runtime list/remove` | [runtimes.py](../src/envforge/runtimes.py) | test_runtimes.py、test_cli.py | 移除只删记录，不卸载软件 |
| 生成会话级激活脚本 | ✅ | `runtime activate` / GUI 按钮 | [runtimes.py:229](../src/envforge/runtimes.py#L229-L260) | test_runtimes.py | 只改当前会话 PATH，不写系统变量 |
| 跨版本一键切换（持久化到 PATH） | ✗ | — | — | — | 目前只生成会话脚本；持久切换需自行拼接 PATH 并写入（可用 `env apply` 手工完成） |
| 运行时自动下载 / 安装 / 卸载 | ✗ | — | — | — | 属于未来需求，见 RISK_REGISTER R-F03 |

### 2.2 项目与环境档案

| 功能 | 状态 | 入口 | 代码证据 | 测试证据 | 说明 |
| --- | --- | --- | --- | --- | --- |
| 项目登记（名称唯一、路径标准化） | ✅ | `project add/list/remove` / GUI | [profiles.py:47](../src/envforge/profiles.py#L47-L119) | test_profiles.py | 删除项目级联删除档案（外键 CASCADE） |
| 多环境档案（dev/test/prod） | ✅ | `profile set/list/remove` | [profiles.py:122](../src/envforge/profiles.py#L122-L231) | test_profiles.py | 档案名不区分大小写唯一；active 标记互斥 |
| 档案变量写入 / 删除（敏感值自动加密） | ✅ | `profile set/unset` | [profiles.py:234](../src/envforge/profiles.py#L234-L305) | test_profiles.py | 写入时敏感键自动 DPAPI 加密，`--secret/--plain` 可覆盖 |
| 档案展示（默认脱敏，显式 reveal） | ✅ | `profile show --reveal` / GUI 勾选 | [profiles.py:344](../src/envforge/profiles.py#L344-L388) | test_profiles.py、test_gui.py | 解密失败返回空值 + 状态，不静默降级 |
| 两档案差异 | ✅ | `profile diff` | [profiles.py:442](../src/envforge/profiles.py#L442-L465) | test_profiles.py | 按键名大小写不敏感比较 |
| `.env` 导出（默认脱敏） | ✅ | `profile export` | [profiles.py:419](../src/envforge/profiles.py#L419-L440) | test_profiles.py | 含空格/引号的值自动加引号转义 |
| 档案激活（解密并生成会话脚本） | ✅ | `profile activate --yes` / GUI | [profiles.py:390](../src/envforge/profiles.py#L390-L417) | test_profiles.py | 必须显式确认；不可解密的值跳过并告警 |
| 从 `.env` / 文件导入档案 | ✗ | — | — | — | 未实现，见 RISK_REGISTER R-F01 |

### 2.3 敏感值保护

| 功能 | 状态 | 入口 | 代码证据 | 测试证据 | 说明 |
| --- | --- | --- | --- | --- | --- |
| 敏感键名识别 | ✅ | 所有写入/展示路径 | [models.py:36](../src/envforge/models.py#L36-L38) | test_core.py | 覆盖 SECRET/TOKEN/PASSWORD/API_KEY/DATABASE_URL/MONGO_URI 等 |
| DPAPI 加密落库（当前用户绑定） | ✅ | `profile set` | [secrets.py:112](../src/envforge/secrets.py#L112-L136) | test_core.py（真实 DPAPI 往返） | 固定 entropy `EnvForge/secret/v1`；密文不落明文 |
| DPAPI 不可用时拒绝明文落库 | ✅ | 同上 | [profiles.py:246](../src/envforge/profiles.py#L246-L260) | test_profiles.py（FakeCipher 不可用） | 只记录键名，返回中文警告，doctor 会再次提示 |
| 展示 / 导出脱敏 | ✅ | 各展示路径 | [secrets.py:28](../src/envforge/secrets.py#L28-L38) | test_core.py、test_gui.py | 固定 `***`，默认不泄露任何字符 |

### 2.4 系统环境变量与 PATH

| 功能 | 状态 | 入口 | 代码证据 | 测试证据 | 说明 |
| --- | --- | --- | --- | --- | --- |
| 三种作用域读取（user/system/session） | ✅ | `env show` / GUI 作用域下拉 | [envvars.py:102](../src/envforge/envvars.py#L102-L176) | test_env.py | system 读取 HKLM，session 读 `os.environ` |
| user 作用域写入（HKCU\Environment） | ✅ | `env apply` / GUI 编辑 | [envvars.py:181](../src/envforge/envvars.py#L181-L204) | test_env.py（注入 Provider） | 含 `%` 自动用 REG_EXPAND_SZ；写后广播 WM_SETTINGCHANGE |
| system 作用域写入（HKLM） | ✗ | — | [envvars.py:183](../src/envforge/envvars.py#L183-L186) | test_env.py（断言只读拒绝） | 设计上只读，需要管理员权限；见 R-F02 |
| PATH 分析（重复 / 失效 / 空段） | ✅ | `env path` / GUI PATH 表格 | [envvars.py:57](../src/envforge/envvars.py#L57-L88) | test_env.py | 原始 PATH 直接分析，避免 split/join 丢失空段 |
| PATH 一键去重（预览 + 写入） | ✅ | `env dedupe --apply --yes` / GUI | [envvars.py:36](../src/envforge/envvars.py#L36-L54) | test_env.py、test_cli.py | 大小写不敏感去重；写入走事务化 apply |

### 2.5 写入、快照与回滚（P0-1 / P0-4）

| 功能 | 状态 | 入口 | 代码证据 | 测试证据 | 说明 |
| --- | --- | --- | --- | --- | --- |
| 写入前自动快照 | ✅ | 所有 apply/restore | [envstore.py:97](../src/envforge/envstore.py#L97-L110) | test_env.py | 快照保存全量变量 + PATH 列表 + checksum |
| **写后回读校验** | ✅ | 同上 | [envstore.py:298](../src/envforge/envstore.py#L298-L312) | test_apply_marks_verified、test_apply_rolls_back_on_readback_mismatch | 逐键比对实际值与预期；只修，不猜 |
| **写入失败自动回滚** | ✅ | 同上 | [envstore.py:314](../src/envforge/envstore.py#L314-L338)、[envstore.py:213](../src/envforge/envstore.py#L213-L296) | test_apply_rolls_back_on_write_exception | 只撤销本次涉及的键，不动其他变量；返回 `rolled_back` 审计 |
| **部分成功标记** | ✅ | 异常对象 | [errors.py:26](../src/envforge/errors.py#L26-L48) | 同上（`partial` / `rollback_ok` 断言） | 回滚失败时 `partial=True`，提示手动 restore 命令 |
| **跨进程写锁** | ✅ | 自动生效 | [lock.py:40](../src/envforge/lock.py#L40-L100) | test_apply_rejects_concurrent_writer | msvcrt 文件锁 + 进程内锁；默认等待 8 秒，超时中文提示 |
| 快照列表 / 差异 / 导出 / 回滚 | ✅ | `env snapshots/diff/export/restore` | [envstore.py:170](../src/envforge/envstore.py#L170-L210)、[envstore.py:367](../src/envforge/envstore.py#L367-L407) | test_env.py、test_cli.py | `--delete-extras` 可选删除额外变量 |
| 快照保留策略 / 自动清理 | ✗ | — | — | — | 目前快照无限增长；见 R-F05 |

### 2.6 审计、崩溃日志与数据保护（P0-2 / P0-3）

| 功能 | 状态 | 入口 | 代码证据 | 测试证据 | 说明 |
| --- | --- | --- | --- | --- | --- |
| JSONL 操作审计（只记键名） | ✅ | 自动写入；`audit list`；GUI「操作历史」 | [audit.py:64](../src/envforge/audit.py#L64-L96) | test_audit_records_keys_but_not_values、test_cli.py | 按日文件 `audit-YYYYMMDD.jsonl`，保留 30 天 |
| 审计覆盖范围 | ◐ | 自动 | [envstore.py:346](../src/envforge/envstore.py#L346-L365)、[profiles.py:307](../src/envforge/profiles.py#L307-L324) | test_env.py、test_cli.py | 覆盖 env.apply/restore、profile.set/unset/remove/activate、db-check/backup、gui.start；**未覆盖** runtime/project 的记录 |
| 崩溃日志 | ✅ | GUI 自动安装钩子 | [audit.py:168](../src/envforge/audit.py#L168-L199)、[audit.py:201](../src/envforge/audit.py#L201-L213) | 无自动化（需真实崩溃） | 写 `crash-YYYYMMDD.log` 后交回原钩子；保留 90 天 |
| 数据库完整性自检 | ✅ | 打开时自动；`env db-check` | [db.py:179](../src/envforge/db.py#L179-L191) | test_core.py、test_cli.py | `PRAGMA quick_check`；失败给出备份目录与恢复指引 |
| 每日独立备份 | ✅ | 启动自动；`env db-backup` | [db.py:193](../src/envforge/db.py#L193-L238) | test_backup_to_creates_consistent_copy、test_backup_daily_creates_once_per_day | SQLite 在线备份 API；保留最近 7 份，目录 `backups/` |
| 备份恢复的一键入口 | ✗ | — | — | — | 目前需手工替换 db 文件；见 R-F04 |

### 2.7 变量库、健康检查与入口

| 功能 | 状态 | 入口 | 代码证据 | 测试证据 | 说明 |
| --- | --- | --- | --- | --- | --- |
| 内置 151 个常用变量 / 10 分类 | ✅ | `library list/show/categories` / GUI 变量库 | [library.py:248](../src/envforge/library.py#L248-L290) | test_library.py | 敏感标记由正则自动推导，与运行时一致 |
| 变量库一键加入档案 | ✅ | GUI 预设对话框 | [qt_app.py:263](../src/envforge/ui/qt_app.py#L263-L330) | test_gui.py（对话框约束） | 敏感变量强制加密 |
| 健康检查（PATH/多版本/未登记/未加密/项目失效） | ✅ | `doctor` / GUI 健康体检 | [doctor.py:45](../src/envforge/doctor.py#L45-L237) | test_env.py、test_gui.py | `--fail-on warn` 可接 CI |
| CLI（含 `--json`、退出码 0/1/2/3） | ✅ | `envforge` | [cli.py:254](../src/envforge/cli.py#L254-L343) | test_cli.py | 破坏性操作一律要求 `--yes` |
| GUI 五个功能区（后台线程） | ✅ | `gui` | [qt_app.py:331](../src/envforge/ui/qt_app.py#L331-L372) | test_gui.py | 探测/体检走 QThread；写入在主线程（见 R-02） |
| 打包分发（PyInstaller + Inno Setup + 便携 ZIP） | ✅ | 见 packaging.md | [EnvForge.spec](../EnvForge.spec)、[installer.iss](../packaging/installer.iss) | release/verify-release.ps1 | 无代码签名（见 P-08） |

## 3. 未完成能力（不对外宣称）

| 能力 | 缺口 | 优先级 | 关联风险 |
| --- | --- | --- | --- |
| `.env` / 文件导入 | 无导入入口；导入时的路径与变量名校验尚未设计 | P1 | R-F01 |
| system 作用域写入 | 明确只读，无提权路径 | P2 | R-F02 |
| 运行时自动安装/卸载 | 只登记与生成脚本 | P2 | R-F03 |
| 备份一键恢复 | 只有备份与校验，无恢复向导 | P1 | R-F04 |
| 快照归档/清理 | 快照只增不删（可手动 `delete` API，CLI 未暴露） | P1 | R-F05 |
| 自动更新 / 多语言 / Dashboard | 完全未实现 | P2 | R-F06 |

## 4. 复核命令

```powershell
cd EnvForge
python -m pytest -q                              # 114 项
python -m pytest -q --cov=envforge --cov-report=term-missing
envforge env db-check
envforge audit list --limit 20
```
