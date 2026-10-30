# EnvForge 架构说明

## 1. 分层

```text
UI 层        src/envforge/ui/qt_app.py（PySide6）   src/envforge/cli.py（argparse）
──────────────────────────────────────────────────────────────────────────
服务层       ProjectService / ProfileService        （profiles.py）
             RuntimeService                          （runtimes.py）
             EnvService                              （envstore.py）
             Doctor                                  （doctor.py）
             常用变量目录                            （library.py，纯静态数据）
──────────────────────────────────────────────────────────────────────────
基础层       Database（SQLite）         SecretCipher（DPAPI）
             EnvProvider（session / registry）       models.py / errors.py
             AuditLog（JSONL 审计 / 崩溃日志，audit.py）   写锁 file_lock（lock.py）
```

GUI 与 CLI 只是两种外壳，两者都只调用服务层，不直接读写注册表或数据库。服务层
依赖注入：`EnvService` 接受 `EnvProvider`，`RuntimeService` 接受 `which_fn` /
`probe_fn`，`ProfileService` 接受 `SecretCipher`。测试因此可以在内存 SQLite 和
假 Provider 上跑完整流程，不会触碰真实注册表。

## 2. 数据模型

| 表 | 用途 |
| --- | --- |
| `meta` | `schema_version` 等元信息 |
| `runtimes` | 已登记运行时：kind、version、exe_path、source、notes |
| `projects` | 项目：name（不区分大小写唯一）、path |
| `profiles` | 档案：project_id、name、sort_order |
| `profile_vars` | 档案变量：key、value、is_secret、updated_at |
| `env_snapshots` | 环境快照：scope、label、payload JSON、created_at |

- `schema_version` 默认 1；启动时若数据库版本高于当前程序支持版本，直接抛出
  `DatabaseError` 拒绝打开（只升不降，避免误损坏数据）。
- 连接使用 `PRAGMA journal_mode=WAL` 与 `foreign_keys=ON`；写入统一走
  `Database.transaction()` 上下文管理器，并在 `RLock` 下串行化。
- 所有时间以 UTC ISO 字符串存储，界面显示时才转换为本地时间。

## 3. 敏感值

`secrets.py` 通过 ctypes 调用 `CryptProtectData` / `CryptUnprotectData`，使用固定的
entropy `b"EnvForge/secret/v1"`。关键约束：

1. `looks_sensitive_key()` 用正则识别敏感键名（`SECRET|TOKEN|PASSWORD|PASSWD|PWD|
   API_KEY|PRIVATE_KEY|CREDENTIAL|AUTH|SALT`），命中即按敏感处理；用户也可用
   `--secret` / `--plain` 覆盖。
2. 敏感值必须先加密再入库；DPAPI 不可用时**不落明文**，只保存键名并把状态标记为
   `unavailable`，同时返回警告文本。
3. 展示路径统一调用 `mask_value()`，默认不泄露任何字符，固定输出 `***`。
4. 解密失败（换用户/换机器）返回 `(None, "error")`，界面显示「不可解密」，不会
   静默降级。

## 4. 常用变量目录

`library.py` 是纯静态数据模块，内置 151 个常用环境变量，分 10 类：运行时与工具链、
路径与缓存、代理与网络、构建与编译、调试与日志、终端与本地化、容器与云、数据库、
应用与密钥、Windows 系统。每个条目包含变量名、分类、中文说明、示例值、注意事项，
敏感标记由 `looks_sensitive_key()` 自动推导，保证目录与运行时脱敏规则一致。

- `search(category, keyword)` 支持按分类与关键词（变量名/分类/说明/示例）过滤。
- `validate()` 在测试中校验：数量不少于 100、变量名唯一且合法、分类非空、敏感标记
  与正则一致、说明不为空。
- 目录只读，不写入数据库；GUI 的「变量库」页可复制示例或把变量加入某个项目档案
  （加入时敏感变量强制加密），CLI 通过 `envforge library list/show/categories` 查询。

敏感键正则同时覆盖 `ACCESS_KEY`、`SIGNING_KEY`、`ENCRYPTION_KEY`、`DATABASE_URL`、
`CONNECTION_STRING`、`MONGO_URI`、`REDIS_URL`、`DSN`，因此 AWS 访问密钥、数据库连接
串等常见配置默认按密钥脱敏。

## 5. 环境作用域与 PATH

- `session`：读写 `os.environ`，只影响当前进程，适合验证。
- `user`：`HKCU\Environment`，唯一允许写入的持久作用域。
- `system`：`HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment`，
  只读，需要管理员权限才可能写入，EnvForge 不提供该路径。

`RegistryProvider` 读取时保留原始字符串（`REG_EXPAND_SZ` 不解引用，`%VAR%` 原样
保留），写入时按值是否包含 `%` 自动选择 `REG_EXPAND_SZ` / `REG_SZ`，写完后调用
`SendMessageTimeoutW(HWND_BROADCAST, WM_SETTINGCHANGE, "Environment")`，广播失败
只忽略，不影响写入结果。

PATH 分析在 `envvars.py`：`split_path()` 去引号、去空白、保留空段；
`analyze_path()` 标记 `duplicate_of` 与 `exists`；`dedupe_path()` 大小写不敏感去重
并返回被移除的段。`envstore.path_report()` 直接读取 Provider 的原始 PATH 字符串，
避免先 split 再 join 丢失末尾空段。

## 6. 写入事务化与回滚

`EnvService.apply()` 的完整流程（[envstore.py:213](../src/envforge/envstore.py#L213-L296)）：

1. 校验 `confirm=True`（否则抛 `ConfirmationRequired`，CLI 返回退出码 3）。
2. 校验目标 Provider 可写（`system` 直接拒绝）。
3. **写入前**创建 `pre-apply` 快照，记录当前全部变量与 PATH。
4. 获取跨进程写锁 `file_lock("env-<scope>")`（默认等待 8 秒，冲突抛
   `EnvAccessError` 中文提示，见 [lock.py:40](../src/envforge/lock.py#L40-L100)）。
5. 执行 `provider.write()`。
6. **回读校验**：重新 `list_vars()`，逐键比对实际值与预期（[envstore.py:298](../src/envforge/envstore.py#L298-L312)）。
7. 若写入抛错或回读不一致：用 `pre` 快照回滚**本次涉及的键**（不碰其他变量），
   然后抛 `EnvWriteFailure`，携带 `pre_snapshot_id`、`failed_keys`、`rollback_ok`、
   `partial`。回滚失败时 `partial=True`，消息里给出可复制的 `env restore` 命令。
8. 成功返回 `verified=True`，并写一条 `env.apply` 审计。

`restore()` 复用同一套流程：计算差异后调用 `apply()`，因此同样具备锁、回读校验与
失败回滚，并额外写一条 `env.restore` 审计。`delete_extras` 可选择删除快照中不存在
的键（默认不删，避免误删他人新增的变量）。

已知边界：回滚只撤销本次写入的键；若第三方工具在锁内也改了同名键，最终以
「最后写入」为准。`restore` 的差异比较目前对键名大小写敏感（见 RISK_REGISTER R-01）。

## 7. 运行时

`RuntimeService.detect()` 遍历已知命令名（`python`、`python3`、`node`、`java`、
`go`、`cargo` 等），通过 `shutil.which` 解析真实路径，去重 realpath，再执行
`--version` / `-V` / `java --version` 探测版本（5 秒超时）。

- **探测不到版本的候选会被跳过**：Windows 的 `WindowsApps\python3.exe` 是应用商店
  存根，运行不出版本，跳过它可以避免登记无效条目；用户仍可用 `runtime add` 手动
  登记。
- 重新探测命中同一路径时更新版本与时间，不重复插入。
- `register()` 校验路径存在且是文件；版本为空时保留数据库中已有的旧版本，
  避免一次探测失败把已知版本清空。

激活脚本只把条目目录前置到当前会话的 PATH，不写任何系统变量；路径用单引号包裹，
内部单引号转义为 `''`，避免 PowerShell 解析问题。

## 8. 健康检查

`Doctor.run(scopes)` 汇总以下发现：

| 检查 | 级别 |
| --- | --- |
| PATH 重复段 / 失效目录 / 空段 | warn |
| 同一运行时存在多个版本 | info |
| PATH 中的可执行文件未登记 | info |
| 变量键名敏感但值未加密 | warn |
| 已登记项目目录不存在 | error |

`--fail-on warn` 让脚本在出现 warn 及以上问题时返回非 0，便于接入 CI。

## 9. 界面线程模型

`ui/qt_app.py` 中所有耗时操作（探测、体检、快照）都放在 `_Worker(QThread)` 中执行，
通过 `succeeded` / `failed` 信号回传结果，主线程只负责渲染。每个 Worker 都保存在
`self._workers` 集合里，并在 `finished` 时移除——保留显式 Python 引用可以避免对象
被 GC 回收导致线程无法结束。表格数据单元格统一设置 tooltip，长路径不会被截断成
无法辨认的内容。

## 10. 审计与崩溃日志

`audit.py` 用按天的 JSONL 文件记录「谁在什么时候改了什么键」：

- 字段固定为 `ts / action / scope / keys / key_count / result / snapshot_id / detail`，
  **只写变量名，不写变量值**；`detail` 只允许传固定文案。
- 文件位于数据库旁的 `logs/`（自定义 `--db` 时跟随数据库），保留 30 天；
  崩溃日志为 `crash-YYYYMMDD.log`，保留 90 天。
- 接入点：`EnvService.apply/restore`、`ProfileService.set_var/unset_var/remove_profile/
  activation`、`env db-check`、`env db-backup`、GUI 启动。
- `append()` 失败只记 `last_error` 并返回 `None`，**不阻断业务**（见 RISK_REGISTER R-03）。
- GUI 通过 `sys.excepthook`（`install_crash_hook`）把未捕获异常写入崩溃日志，再交回
  原钩子；「系统环境」页的「操作历史」按钮展示最近 50 条审计。

## 11. 数据保护

- 打开数据库时执行 `PRAGMA quick_check`（[db.py:179](../src/envforge/db.py#L179-L191)），
  结果存于 `db.integrity`；失败只提示恢复路径，不阻断启动。
- 每日首次启动用 SQLite 在线备份 API 复制一份到数据库旁 `backups/`
  （`envforge-YYYYMMDD-HHMMSS.db`，保留 7 份）；`env db-backup` 可随时手动备份。
- 备份包含 WAL 中尚未落盘的数据，是用 `Connection.backup()` 而非文件复制的原因。

## 12. 已知限制

- 只支持 Windows（DPAPI、注册表、`msvcrt` 文件锁均为 Windows 专属）。
- 运行时自动安装/卸载未实现，只做登记与切换。
- 跨进程互斥只覆盖 EnvForge 自身的写入；其他工具直接改注册表时不参与该锁，
  仍遵循「后写覆盖」。
- `system` 作用域只读；如需修改请使用系统自带工具或管理员 PowerShell。
- 数据库完整性检查失败后仍允许写入（有意权衡），建议后续改为只读模式（R-04）。
- 详见 [RISK_REGISTER.md](RISK_REGISTER.md) 与 [FEATURE_MATRIX.md](FEATURE_MATRIX.md)。
