# EnvForge 测试报告

## 1. 环境

| 项目 | 值 |
| --- | --- |
| 操作系统 | Windows 11 x64 |
| Python | 3.12.3 |
| pytest | 9.0.3 |
| coverage | 7.14.1 |
| PySide6 | 6.12.0（测试使用 `QT_QPA_PLATFORM=offscreen`） |
| PyInstaller | 6.20.0 |
| Inno Setup | 6.7.3 |

测试命令：

```powershell
python -m pytest -q --cov=envforge --cov-report=term-missing
```

## 2. 结果汇总

```text
114 passed in 5.53s
```

| 测试文件 | 用例数 | 覆盖内容 |
| --- | --- | --- |
| tests/test_core.py | 13 | 数据模型、版本解析、敏感键识别、数据库与迁移、完整性自检、备份与每日备份 |
| tests/test_profiles.py | 17 | 项目登记、档案 CRUD、敏感值加密、激活、差异、导出 |
| tests/test_runtimes.py | 12 | 探测去重、存根跳过、登记校验、默认版本、激活脚本 |
| tests/test_env.py | 29 | PATH 分析、作用域 Provider、快照、差异、写入回读校验/自动回滚、并发写锁、审计脱敏 |
| tests/test_cli.py | 25 | 子命令输出、`--json` 位置无关、退出码、确认拦截、变量库查询、db-check/db-backup、audit list |
| tests/test_gui.py | 11 | 窗口与五个 Tab、变量库筛选、预设对话框、时间格式化、脱敏展示 |
| tests/test_library.py | 7 | 目录数量 ≥100、键名唯一合法、分类非空、搜索、敏感标记一致性 |

所有测试使用内存或临时目录数据库，注册表操作通过 `MemoryProvider` 注入，
不会读写真实的 `HKCU`/`HKLM`。

## 3. 覆盖率

| 模块 | 语句 | 未覆盖 | 覆盖率 |
| --- | --- | --- | --- |
| `errors.py` | 14 | 0 | 100% |
| `models.py` | 106 | 2 | 98% |
| `doctor.py` | 116 | 3 | 97% |
| `envstore.py` | 193 | 12 | 94% |
| `profiles.py` | 232 | 15 | 94% |
| `runtimes.py` | 151 | 10 | 93% |
| `secrets.py` | 90 | 8 | 91% |
| `library.py` | 81 | 8 | 90% |
| `lock.py` | 65 | 10 | 85% |
| `db.py` | 133 | 23 | 83% |
| `envvars.py` | 141 | 28 | 80% |
| `cli.py` | 581 | 170 | 71% |
| `audit.py` | 130 | 51 | 61% |
| `ui/qt_app.py` | 1108 | 555 | 50% |
| **合计** | **3147** | **897** | **71%** |

核心引擎（models / db / secrets / profiles / runtimes / envvars / envstore /
doctor / library）整体覆盖率约 **91%**，高于 80% 的验收门槛。`audit.py` 未覆盖
部分集中在保留期清理与崩溃钩子（需要真实崩溃/跨天场景）；`cli.py` 未覆盖部分主要是
各子命令的错误分支与格式化输出；`ui/qt_app.py` 的交互分支依赖人工点击，自动化只覆盖
窗口构建、变量库筛选与展示逻辑。

## 4. 真实环境验证

在开发机上用真实数据（非 mock）完成了以下验证：

1. **运行时探测**：识别 Python 3.12.3、Node 24.15.0、JDK 21.0.12，路径去重正确；
   `WindowsApps\python3.exe` 存根因探测不出版本被正确跳过（有回归用例
   `test_detect_skips_unprobeable_stubs`）。
2. **DPAPI**：`--secret` 写入后直接查询数据库，密文中不含明文；`profile show`
   显示 `***`；`--reveal` 才显示明文。
3. **PATH 诊断**：真实 PATH 上检出重复目录与失效目录，`env path` 的分级统计与
   人工核对一致。
4. **快照与回滚**：对 `session` 作用域执行 apply → diff，行为符合预期
   （session 写入不跨进程，CLI 每次调用是新进程，这是设计语义而非缺陷）。
5. **变量库**：`library categories` 输出 151 个变量、10 个分类；`library show
   DATABASE_URL` 正确标记为敏感；GUI 变量库页在 1120×720 窗口下列宽完整、无横向
   滚动条，分类筛选与关键词搜索均生效。
6. **打包产物**：安装器静默安装、启动（主窗口标题 `EnvForge 环境工坊 v0.1.0`）、
   静默卸载均通过；卸载后安装目录与卸载项已清除。

## 5. 已知缺口

- GUI 的按钮回调、后台线程信号链没有逐条自动化，靠人工在打包产物上走查。
- 注册表广播 `WM_SETTINGCHANGE` 的失败分支未覆盖（需要构造异常环境）。
- 未在 Windows 10 上实测，仅在 Windows 11 x64 验证。
- 崩溃日志钩子只做了手工验证，未纳入自动化（需要真实未捕获异常）。
- 跨进程锁的自动化用例是「同进程双句柄 + 进程内锁」；真实双进程竞争用临时脚本
  验证过：子进程 A 持锁 6 秒，子进程 B 以 `timeout=2` 获取，2 秒后得到
  `另一个 EnvForge 窗口或命令行进程正在写入环境变量…请关闭其他窗口后重试`，
  未出现互斥失效。该验证是手工执行的一次性检查，未纳入自动化回归。
