# RunTrace 打包与分发

## 1. Python 包（推荐）

项目采用 `src/` 布局，`pyproject.toml` 使用 setuptools 构建；包名为 `runtrace`，
入口点为 `runtrace.cli:main`。

```powershell
cd RunTrace
python -m pip install -e .              # 开发安装
python -m pip install -e ".[gui]"       # 需要 GUI 时安装 PySide6
```

构建 wheel：

```powershell
python -m pip install build            # 如果尚未安装构建工具
python -m build
# 产物：dist/runtrace-0.1.0-py3-none-any.whl、dist/runtrace-0.1.0.tar.gz
```

离线/内网环境可让构建机准备 `build`、`setuptools`、`wheel` 后使用
`--no-build-isolation`，或直接把源码目录与 `PYTHONPATH=src` 一起分发。应用本身
运行时不存在第三方核心依赖；PySide6 是可选依赖。

安装后启动：

```powershell
python -m runtrace
python -m runtrace --help
runtrace --help        # console script
```

`python -m runtrace` 在 PySide6 可用时打开 GUI；不可用时回退 CLI 帮助，不会因缺少
GUI 依赖崩溃。

## 2. Windows 桌面可执行程序（可选）

PySide6 应用可用 PyInstaller/Nuitka 打包，但 PySide6 和打包工具不是本项目强依赖。
建议使用干净的 Python 3.12 虚拟环境：

```powershell
python -m venv .venv-build
.\.venv-build\Scripts\activate
python -m pip install PySide6 pyinstaller
python -m pip install -e .
```

PyInstaller 需要一个入口脚本，原因是 `runtrace.ui.qt_app` 通过惰性导入加载。项目已提供
`packaging/entry_gui.py`：

```python
from runtrace.ui.qt_app import launch

raise SystemExit(launch())
```

入口脚本必须放在 `packaging\` 目录，不能放在项目根目录：根目录的 `runtrace.py`
启动器会遮蔽 `src\runtrace` 包，导致 PyInstaller 解析到错误模块。

打包命令（在项目根目录执行）：

```powershell
python -m PyInstaller --noconfirm --clean --windowed --name RunTrace `
  --paths src --hidden-import runtrace.ui.qt_app `
  --collect-submodules runtrace `
  packaging\entry_gui.py
```

注意：

- 入口脚本只用于打包，不放入运行时包内。
- 已实测：`dist\RunTrace\` 约 122.8 MB，Windows 11 上冷启动约 0.75 秒。
- `--windowed` 适合 GUI；如需要 CLI 控制台，改用 `--console`，或分别构建 GUI/CLI
  两个入口。
- PyInstaller 会把 PySide6 运行时一并打包，体积较大；如只需 CLI，不必安装/打包
  PySide6。
- 分发给他人前建议对 exe 做代码签名，并按组织策略检查 SmartScreen/杀毒软件误报。
- 正式发布应在 Windows 10 和 Windows 11 上分别冒烟：`python -m runtrace`、运行
  成功/失败命令、停止长任务、生成/校验复现包。

## 3. 数据与升级

- 数据库默认在 `%LOCALAPPDATA%\RunTrace\runtrace.db`，升级包不会删除它。
- `RUNTRACE_DATA_DIR` 可覆盖数据目录；CLI `--db` 可指定其他数据库。
- 当前 schema 为 v1，没有自动迁移框架；跨版本升级前应备份数据库。删除数据库会
  删除全部历史与日志（应用默认不自动清理）。
- 导出文件与复现包含脱敏环境值和运行日志，应按敏感数据管理。

## 4. 安装器（Inno Setup）

已提供 `packaging\installer.iss`。安装 Inno Setup 6 后编译：

```powershell
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" packaging\installer.iss
```

产物：`release\installers\RunTrace-0.1.0-setup.exe`（33.6 MB）。按用户安装到
`%LOCALAPPDATA%\Programs\RunTrace`，无需管理员权限，含开始菜单快捷方式，卸载入口
写入当前用户的应用列表。

实测：`/VERYSILENT` 静默安装成功 → 启动窗口正常（约 0.91 秒）→ 静默卸载后安装目录
与 `HKCU` 卸载项均已清除。安装器未做代码签名，SmartScreen 可能提示。

exe 构建使用 `--icon assets\app.ico`，安装器通过 `SetupIconFile=assets\app.ico`
使用同一图标；`dist` 与安装器均已实机确认图标生效。

## 5. 发布检查清单

- [ ] `python -m pytest -q` 全绿。
- [ ] `python -m runtrace --help` 与 `runtrace --help` 均可用。
- [ ] PySide6 可选：卸载后可回退 CLI；安装后可启动 GUI。
- [ ] 用带空格/中文的路径和参数跑一次成功与失败任务。
- [ ] 验证停止长任务后无孤儿进程。
- [ ] 导出 JSON/ZIP 并检查环境变量中没有 `***` 以外的明文密钥。
- [ ] 生成复现包并检查外部依赖与脱敏缺口；执行必须二次确认。
- [ ] 在目标 Windows 版本上检查数据目录写入权限。
