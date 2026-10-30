# RunTrace 测试报告

- 日期：2026-10-29
- 平台：Windows 10/11，Python 3.12.3
- pytest：9.0.3
- 项目目录：`RunTrace`（仓库内相对路径）
- 测试数据库：每个测试使用独立 `tmp_path`，彼此不共享状态。

## 执行命令与结果

```powershell
cd RunTrace
python -m pytest -q
```

结果：

```text
.........................................                                [100%]
41 passed in 9.79s
```

另外在项目根目录执行：

```powershell
python -m runtrace --help
```

CLI 帮助正常输出了 run/list/show/diff/export/replay/project/模板等命令，退出码 0。
在 `QT_QPA_PLATFORM=offscreen` 下实例化 PySide6 主窗口并处理事件循环，输出
`GUI_SMOKE_OK`（不参与自动化测试，避免测试依赖 PySide6）。

## 覆盖矩阵

| 需求 | 测试文件 | 关键断言 |
|---|---|---|
| 成功退出 0、stdout/stderr、中文输出、环境脱敏 | `test_process_runner.py` | 状态 success、exit 0、日志入库、secret 不在 run 记录中 |
| 失败退出码 3、stderr | `test_process_runner.py` | 状态 failed、exit 3、stderr 可读取 |
| 中文/空格 cwd 与参数 | `test_process_runner.py`、`test_command_builder.py` | 参数数组原样传递、cwd 解析正确、输出含中文/空格 |
| 超时 sleep 2 / timeout 0.5 | `test_process_runner.py` | 状态 timeout、耗时 < 2s、进程树被终止 |
| 取消长任务（sleep 30） | `test_process_runner.py` | 运行中日志已实时可读、`runner.stop()` 后状态 cancelled、线程退出 |
| 孙进程整棵树终止 | `test_process_runner.py` | 父进程再启动 sleep(30) 孙进程，`taskkill /T` 后孙进程 PID 消失 |
| 大输出流式（3,000,000 字符） | `test_process_runner.py` | 完整日志可还原、落库多个 chunk |
| 混合编码输出（GBK + UTF-8） | `test_process_runner.py` | 逐行回退本地编码，两行中文均正确还原 |
| 输出回调双流 | `test_process_runner.py` | stdout/stderr 回调均收到内容 |
| 启动失败 | `test_process_runner.py` | 状态 failed、错误写入 stderr 记录 |
| 持久化、唯一 ID、事务日志 | `test_run_store.py` | run id 唯一、分块日志顺序正确 |
| 搜索/项目/状态/时间筛选 | `test_run_store.py` | 日志全文搜索、状态/项目/日期边界正确 |
| 导出 JSON/文本/ZIP、敏感检查 | `test_run_store.py` | 结构、日志、校验和、secret 不泄露 |
| running → interrupted 恢复 | `test_run_store.py` | 遗留 running 记录被恢复、已完成记录不重复处理 |
| 模板保存/复制/脱敏 | `test_run_store.py` | 敏感覆盖不持久化、复制/删除正常 |
| 行级 diff / 退出码变化 / 事实-推测 | `test_diff_engine.py` | changed/added/removed、facts/speculations 分离 |
| 项目上下文非 Git 不报错 | `test_project_context.py` | 返回 `is_git=False` 与说明 |
| Git branch/commit/dirty 只读读取 | `test_project_context.py` | 临时仓库检测正确（有 git 时执行） |
| 复现包结构/校验/缺口 | `test_replay_manager.py` | 必需文件、SHA-256、脱敏缺口、外部依赖 |
| 复现执行必须确认 | `test_replay_manager.py` | 未确认抛异常且不新增 run；确认后生成新 run |
| 篡改/损坏包拒绝 | `test_replay_manager.py` | 校验失败、缺失文件、非 zip 均被拒绝 |
| CLI --help 与运行/列表/查看/导出 | `test_cli.py` | 子进程真实调用 `python -m runtrace` |
| PySide6 缺失自动回退 CLI | `test_cli.py` | 注入伪造缺失模块后输出回退提示并显示帮助 |
| GUI 源码无语法错误且测试不导入 Qt | `test_cli.py` | `compile()` 成功；pytest 不依赖 PySide6 |

## 测试设计说明

- 运行类测试统一使用 `sys.executable -c`，避免依赖外部命令和网络。
- 成功场景同时验证“子进程收到真实 env 覆盖”和“数据库中只保存 `***`”。
- 大输出测试通过 50 次 60,000 字符写入，实际检查 3 MB 级日志的入库与还原，而不是
  只检查进程退出。
- 取消测试在线程中启动阻塞任务，先断言运行中的首行日志已实时可读，再调用
  `runner.stop(id)`，避免只测试事件标志而没有真实子进程。
- 进程树测试由父 Python 进程再启动一个 sleep(30) 孙进程，读取孙进程 PID 后取消，
  用 `tasklist` 确认 `taskkill /T /F` 已终止整棵树。
- 异常恢复通过直接插入 `running` 记录再调用 `recover_interrupted_runs()`，符合
  “模拟崩溃遗留”要求。
- CLI 回退测试用临时 `PySide6` 包抛出 `ImportError`，不依赖本机是否安装 PySide6。
- GUI 只做源码编译和可选的 offscreen 冒烟，不纳入 pytest，以免破坏“测试不依赖
  PySide6”的约束。

## 已知测试边界

- 未覆盖真实鼠标点击 PySide6 界面（自动化环境窗口服务不稳定）；界面逻辑与执行
  核心分别通过源码编译、offscreen 构造和 CLI/进程测试验证。
- 未做磁盘写满、SQLite 损坏等故障注入。
- 未测试不同 Windows 代码页下的非 UTF-8 子进程输出；该限制已写入 README。
