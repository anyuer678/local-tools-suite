# QuickDeck 测试报告

- 日期：2026-10-29
- 环境：Windows 11、Python 3.12、pytest 9.0.3、PySide6 已安装
- 执行目录：`QuickDeck`（仓库内相对路径）
- 执行命令：`python -m pytest -q`
- 结果：**181 passed，0 failed，0 skipped**（约 8.8 秒）

## 分文件结果

| 测试文件 | 用例数 | 覆盖内容 |
| --- | ---: | --- |
| `tests/test_calculator.py` | 31 | AST 白名单计算、优先级、整除/取模/幂、非法语法、拒绝属性访问/字符串/超大整数/超限指数、中文错误 |
| `tests/test_text_tools.py` | 6 | 去重、排序、过滤、替换、大小写、修剪 |
| `tests/test_encoding_tools.py` | 14 | URL、Base64、Unicode 转义、JSON 格式化/校验（含拒绝 NaN/Infinity） |
| `tests/test_units.py` | 24 | 长度、质量、时间、存储换算（1024 进制）、温度/非法单位错误 |
| `tests/test_command_registry.py` | 8 | SQLite 持久化、别名/分类/关键词搜索、模糊排序、频率与最近使用、缓存与注销 |
| `tests/test_workflow_engine.py` | 9 | 多步骤流水线、输入来源、超时、`fail/skip/default`、输出到剪贴板/文件/显示、从文件读输入 |
| `tests/test_settings.py` | 15 | 默认设置、校验、JSON 导入导出、数据库备份/恢复/迁移、设置持久化 |
| `tests/test_hotkey.py` | 22 | 快捷键解析/规范化/非法输入、虚拟键码、非 Windows 明确报错、Windows 真实注册/注销 |
| `tests/test_clipboard_activity.py` | 8 | 默认关闭、容量上限、排除规则、清空、持久化、时间记录 |
| `tests/test_external_commands.py` | 13 | 预览、未确认拒绝、确认后执行、超时、非零退出、参数校验 |
| `tests/test_plugin_system.py` | 8 | 清单校验、同意启用、真实子进程执行、中文编码、懒加载、超时、异常隔离、禁用、卸载清理 |
| `tests/test_cli.py` | 11 | `--help`、search/run/calc/unit/time、外部命令确认、动作增删执行、插件同意、settings/diagnose、`gui --minimized` 参数转发 |
| `tests/test_builtin_commands.py` | 3 | 内置命令注册、通过注册表调度执行 |
| `tests/test_launch_behavior.py` | 4 | 手动 `gui` 默认可见、`--minimized` 隐藏、`background` 隐藏、`window` 可见（真机回归） |
| `tests/test_plugin_frozen.py` | 2 | 冻结版以自身 exe + `--plugin-runner` 执行；源码版使用 `python -I` |

## 关键验证点

1. **计算器安全**：不使用 `eval`，只遍历 AST 白名单节点；属性访问、字符串、推导式、超大整数、过深/超大指数均被拒绝。
2. **插件真实隔离**：`PluginHost` 通过 `sys.executable -I plugin_runner.py <plugin_dir>` 启动独立进程；stdout 只识别 `__QUICKDECK_RESULT__` 标记，插件打印噪声不会污染协议；超时与异常返回结构化失败。
3. **同意机制**：`plugins enable` 缺少 `--consent` 返回 2；成功启用会写入 `consented_at`，只有同时具备 `enabled` 与同意记录才注册命令。
4. **外部命令**：必须先预览，`confirm=True` 才执行；CLI 未带 `--yes` 返回 2，不会静默运行。
5. **Windows 热键**：解析测试覆盖修饰键顺序、A-Z/0-9/F1-F24/常用特殊键；`test_real_registration_or_skip` 在本机实际注册 `Ctrl+Alt+F11` 成功，未触发 skip，注销后状态正确。
6. **GUI 冒烟**：在 `QT_QPA_PLATFORM=offscreen` 下构造完整窗口、托盘分支、热键注册与清理，`run_gui` 返回 0；未进行真实桌面交互自动化。

## GUI 执行卡死修复（2026-10-30）

- 现象：GUI 选择“计算器”执行 `1+1` 后，执行按钮持续禁用，状态停在“执行中…”，结果不返回。
- 原因：`run_selected` 把 `ExecutionWorker` 移入 `QThread` 后未保留 Python 引用；worker 可能在 `started` 信号发出前被回收，线程因此永远不会 `quit`。
- 修复：窗口新增 `_workers` 集合持有 worker，`finished` 时移除；线程模型与对外行为不变。
- 验证：源码 GUI、PyInstaller 冻结版、重新编译并静默安装的安装版均实测通过（搜索 `calc` → 输入 `1+1` → 结果 `2`、状态“执行完成”）。`pytest` 仍为 **181 passed**。

## 确认框本地化（2026-10-30）

- 现象：选择“外部命令”执行时，Qt 标准按钮显示英文 `Yes` / `No`，与其余中文界面不一致。
- 修复：改为实例化 `QMessageBox` 并显式设置按钮文案为“执行” / “取消”，默认按钮仍为“取消”。
- 验证：`pytest` 保持 **181 passed**；冻结版与安装版重建后界面一致。

## 已知限制

- 剪贴板真实后端依赖 Windows API；自动化测试主要使用内存后端，CLI/Windows 后端仅做冒烟。
- 全局快捷键注册受系统已占用组合影响；失败时会明确提示并降级到托盘/窗口。
- GUI 视觉布局与托盘交互未做像素级/人工回归测试。
- 插件只支持文本输入输出；每次调用启动新进程，冷启动耗时高于内置命令。
- 冻结打包（PyInstaller）下插件由程序自身以 `--plugin-runner` 模式执行；已实测无需外部 Python。插件目录需放在 exe 同级，详见 `docs/packaging.md`。
