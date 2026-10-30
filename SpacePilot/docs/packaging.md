# SpacePilot 打包与分发说明

本文档面向需要把 SpacePilot 分发给其他 Windows 用户的开发者，说明 GUI 依赖的可选性、PyInstaller 打包方式、运行时数据目录与发布检查清单。

## 1. 依赖策略

| 组件 | 是否必需 | 说明 |
| --- | --- | --- |
| Python 3.12 运行时 | 是（源码方式） | 打包为 exe 后由 PyInstaller 自带 |
| 标准库 | 是 | 扫描、SQLite、哈希、执行器全部基于标准库 |
| PySide6 | 否 | 仅 `src/spacepilot/ui/qt_app.py` 内导入；缺失时自动回退 CLI |
| pytest | 否 | 仅开发与 CI 使用 |

`pyproject.toml` 中的可选依赖：

```toml
[project.optional-dependencies]
gui = ["PySide6>=6.5"]
test = ["pytest>=8.0"]
```

回退机制位于 `src/spacepilot/__main__.py`：无参数或 `--gui` 时尝试导入 `spacepilot.ui.qt_app`；任何导入错误（未安装 PySide6、DLL 缺失、无 Qt 平台插件）都会打印 CLI 帮助并返回 0，而不是崩溃。带子命令参数时直接走 CLI，不触碰 PySide6。

因此可以产出两种分发物：

- **完整版**：包含 PySide6，双击启动图形界面。
- **精简版**：不包含 PySide6，只有命令行功能（扫描 / 快照 / 对比 / 重复 / 建议 / 隔离 / 回滚）。

## 2. 源码安装与构建

```powershell
cd SpacePilot

# 开发安装（可编辑）
python -m pip install -e ".[gui,test]"

# 构建 wheel / sdist（需要 build 包）
python -m pip install build
python -m build

# 或直接构建 wheel
python -m pip wheel . -w dist --no-build-isolation
```

安装后提供：

- 控制台命令 `spacepilot`（映射到 `spacepilot.cli:main`，始终为 CLI）。
- 模块入口 `python -m spacepilot`（优先 GUI，失败回退 CLI）。

## 3. 使用 PyInstaller 打包

安装 PyInstaller：

```powershell
python -m pip install pyinstaller
```

### 3.1 完整版（GUI + CLI）

在项目根目录执行：

```powershell
python -m PyInstaller --noconfirm --clean --windowed --name SpacePilot `
  --paths src --hidden-import spacepilot.ui.qt_app `
  --collect-submodules spacepilot `
  packaging\entry_gui.py
```

说明：

- 必须使用 `packaging\entry_gui.py`，不要直接打包 `src\spacepilot\__main__.py`。
  PyInstaller 会把包内 `__main__.py` 当成顶层脚本执行，导致其中的相对导入
  （`from .cli import ...`）失败，冻结版会直接退出。该问题已在实机构建中复现并修复。
- `--paths src` 让 PyInstaller 能找到 `spacepilot` 包（项目使用 `src` 布局）。
- `--windowed` 隐藏控制台窗口，适合 GUI；如需同时保留 CLI 输出，可改用 `--console`，此时双击会弹出控制台。
- PyInstaller 自带 PySide6 hook，通常无需手动 `--collect-all`；若打包后提示缺少 Qt 插件，可追加：
  ```powershell
  pyinstaller -n SpacePilot --windowed --paths src --collect-all PySide6 packaging\entry_gui.py
  ```
  这会显著增大体积。
- 产物位于 `dist\SpacePilot\`，主程序为 `dist\SpacePilot\SpacePilot.exe`。已实测：
  约 122.8 MB，冷启动约 0.71 秒，`--gui` 参数可正常打开主窗口。

### 3.2 精简版（仅 CLI）

不安装 PySide6 的环境中打包：

```powershell
pyinstaller -n SpacePilotCLI --console --paths src packaging\entry_gui.py
```

`packaging\entry_gui.py` 在导入 GUI 失败时会输出 CLI 帮助；建议分发时附带启动脚本或说明，让用户以 `SpacePilotCLI.exe scan ...` 方式调用。

### 3.3 单文件模式（可选）

```powershell
pyinstaller -n SpacePilot --onefile --windowed --paths src packaging\entry_gui.py
```

单文件启动时需要解压到临时目录，首次启动较慢，且部分安全软件会加大误报概率；分发场景优先使用目录模式。

### 3.4 版本信息与图标（可选）

- 图标：`--icon assets\spacepilot.ico`
- 版本信息：准备 `version_info.txt` 后使用 `--version-file version_info.txt`
- 公司 / 产品名建议填写 `SpacePilot`，版本与 `pyproject.toml` 的 `0.1.0` 保持一致。

## 4. 运行时数据目录

打包不会写入安装目录；所有状态保存在用户目录：

| 内容 | 默认路径 | 覆盖方式 |
| --- | --- | --- |
| 快照数据库 | `%LOCALAPPDATA%\SpacePilot\spacepilot.db` | 全局参数 `--db` |
| 隔离目录 | `%LOCALAPPDATA%\SpacePilot\quarantine` | `quarantine --dest` |
| 审计日志 | `%LOCALAPPDATA%\SpacePilot\executions.jsonl` | `--log` |

- 首次运行会自动创建目录与数据库；升级 schema 时会自动迁移（`PRAGMA user_version`）。
- 卸载程序时不会自动删除上述数据，需由用户手动清理。
- 如果 `%LOCALAPPDATA%` 不可用，回退到 `~/.spacepilot`。
- 数据库使用 WAL 模式，目录中会出现 `spacepilot.db-wal`、`spacepilot.db-shm`，属正常现象。

## 5. 安装器（Inno Setup）

已提供 `packaging\installer.iss`，编译命令：

```powershell
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" packaging\installer.iss
```

产物：`release\installers\SpacePilot-0.1.0-setup.exe`（33.6 MB）。按用户安装到
`%LOCALAPPDATA%\Programs\SpacePilot`，无需管理员权限；快捷方式带 `--gui` 参数。

实测：静默安装 → `--gui` 启动窗口正常（约 0.76 秒）→ 静默卸载后目录与卸载注册项清除。
安装器未签名；卸载不会删除扫描数据库与用户数据。

exe 构建使用 `--icon assets\app.ico`，安装器通过 `SetupIconFile=assets\app.ico` 使用同一图标。

## 6. 发布检查清单

- [ ] `python -m pytest -q` 全绿（当前 32 passed）。
- [ ] `python -m spacepilot --help` 退出码 0，子命令完整。
- [ ] `python -m spacepilot scan <测试目录>` 可生成快照并打印统计。
- [ ] GUI 启动、6 个标签页可切换、扫描进度与取消可用。
- [ ] 在未安装 PySide6 的环境验证 `python -m spacepilot --help` 走 CLI 回退、不崩溃。
- [ ] 隔离预览 → 令牌 → 确认 → 执行 → 回滚完整走通，原文件始终未被直接删除。
- [ ] 确认分发版本不暴露删除入口（CLI / GUI 均只提供隔离与用户指定移动）。
- [ ] `README.md`、`docs/test-report.md`、`docs/packaging.md` 随包提供。
- [ ] 如需对外分发，补充 LICENSE、隐私说明（本工具不联网、不上传任何文件信息）。
- [ ] 代码签名（可选）：对 exe 进行 Authenticode 签名，降低 SmartScreen 拦截。
- [ ] 在 Windows 10 与 Windows 11 各做一次冷启动验证。
- [ ] 记录最终产物哈希（SHA-256）用于完整性校验。

## 6. 常见问题

**Q：打包后启动直接显示命令行帮助？**
A：说明 PySide6 未被正确收集或 Qt 平台插件缺失。检查 PyInstaller 构建日志中的 PySide6 hook，必要时使用 `--collect-all PySide6`，或在目标机安装 Microsoft Visual C++ 运行库。

**Q：能否把数据库放在程序目录做成便携版？**
A：可以。启动时传入 `--db .\spacepilot.db`，隔离时传入 `--dest .\quarantine`，日志传入 `--log .\executions.jsonl`。注意便携目录需可写。

**Q：为什么没有删除按钮？**
A：这是安全设计。删除必须由代码显式构造 `Executor(allow_delete=True)` 并经过预览、令牌、确认与关键路径校验；默认分发的 CLI 与 GUI 均不提供删除入口，避免误操作造成不可撤销的数据丢失。

**Q：回滚能恢复删除的文件吗？**
A：不能。回滚只对隔离副本有效；删除不可撤销，移动也不承诺回滚。
