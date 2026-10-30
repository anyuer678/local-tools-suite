# EnvForge 打包与分发

## 1. Python 包

项目使用 `src/` 布局，`pyproject.toml` 基于 setuptools，包名 `envforge`，命令入口
`envforge.cli:main`。核心引擎只依赖标准库，PySide6 为可选依赖。

```powershell
cd EnvForge
python -m pip install -e .              # 开发安装
python -m pip install -e ".[gui]"       # 需要 GUI 时安装 PySide6
```

构建 wheel：

```powershell
python -m pip install build
python -m build
# dist/envforge-0.1.0-py3-none-any.whl、dist/envforge-0.1.0.tar.gz
```

安装后：

```powershell
envforge --help
python -m envforge                      # PySide6 可用时打开 GUI
```

`python -m envforge` 在 PySide6 缺失时会回退到 CLI 帮助，不会崩溃。

## 2. Windows 可执行程序（PyInstaller）

已提供 `EnvForge.spec`。入口脚本必须放在 `packaging\` 目录，不能放在项目根目录：
根目录的 `envforge.py` 启动器会遮蔽 `src\envforge` 包，导致 PyInstaller 解析到错误
模块。spec 关键配置：

```python
a = Analysis(
    ["packaging\\entry_gui.py"],
    pathex=["src"],
    ...
)
exe = EXE(..., name="EnvForge", console=False, icon="assets\\app.ico")
```

构建（建议在干净的 Python 3.12 虚拟环境中）：

```powershell
python -m venv .venv-build
.\.venv-build\Scripts\activate
python -m pip install PySide6 pyinstaller
python -m pip install -e .
python -m PyInstaller --noconfirm --clean EnvForge.spec
```

实测：`dist\EnvForge\` 约 **122.8 MB**；Windows 11 上冷启动约 **2.8 秒**，热启动约
**0.9–2.2 秒**（文件已进入系统缓存时最快 917 ms）。体积主要来自 PySide6 的 Qt 运行时与翻译文件。如果只需要 CLI，
可以不安装 PySide6，直接使用 Python 包方式分发。

图标由 `packaging\make_icon.py` 生成（Pillow，256px，4x 超采样）：

```powershell
python packaging\make_icon.py        # 输出 assets\app.png、assets\app.ico
```

## 3. 安装器（Inno Setup）

`packaging\installer.iss` 按用户安装，无需管理员权限：

- 安装目录：`%LOCALAPPDATA%\Programs\EnvForge`
- 输出：`release\installers\EnvForge-0.1.0-setup.exe`（实测 **33.6 MB**）
- 含开始菜单快捷方式；桌面快捷方式为可选任务
- 卸载入口写入当前用户的应用程序列表

编译（Inno Setup 6）：

```powershell
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" packaging\installer.iss
```

## 4. 发布验证

`release\verify-release.ps1` 对安装器做四步验证：SHA-256 比对 → 静默安装 →
启动并读取窗口标题 → 静默卸载。

```powershell
cd ..
.\release\verify-release.ps1 -Apps EnvForge
```

实测结果（2026-10-30，Windows 11）：

| App | Hash | Install | Launch | Title | Uninstall | 启动耗时 |
| --- | --- | --- | --- | --- | --- | --- |
| EnvForge | OK | OK | OK | EnvForge 环境工坊 v0.1.0 | OK | 917 ms |

安装器与便携包均已写入 `release\SHA256SUMS.txt`。安装器未做代码签名，SmartScreen
可能提示「未知发布者」，属于预期行为。

## 5. 便携包

`release\EnvForge-0.1.0-win64.zip`（48.3 MB）结构：

```text
EnvForge-0.1.0-win64\
  README.md
  assets\app.ico  assets\app.png
  dist\EnvForge\EnvForge.exe
  dist\EnvForge\_internal\...
```

解压后直接运行 `dist\EnvForge\EnvForge.exe`，不需要安装。数据仍写入
`%LOCALAPPDATA%\EnvForge`，可用 `ENVFORGE_DATA_DIR` 改为同目录，做到彻底便携。

## 6. 数据、升级与兼容

- 数据库默认 `%LOCALAPPDATA%\EnvForge\envforge.db`，升级/卸载不会自动删除。
- `ENVFORGE_DATA_DIR` 覆盖数据目录；CLI `--db`、GUI 环境变量 `ENVFORGE_DB` 可指定
  其他数据库文件。
- 数据库 schema 目前为 v1，采用「只升不降」策略：程序打开高于自身支持的版本时会
  拒绝启动并提示升级软件，不会尝试降级写入。
- DPAPI 密文与 Windows 用户绑定；把数据库拷到另一台机器/另一个账户后，敏感值会
  显示为「不可解密」而不是明文。

## 7. 发布检查清单

- [x] `python -m pytest -q` 全绿（114 项）。
- [x] `envforge --help` 与 `python -m envforge --help` 可用。
- [x] PySide6 可选：缺失时回退 CLI。
- [x] 中文/带空格路径下的项目登记、档案读写正常。
- [x] 敏感值在界面、CLI、导出中均为 `***`。
- [x] `env apply` / `restore` 缺少 `--yes` 时返回退出码 3。
- [x] `env db-check` 报健康、`env db-backup` 生成可打开的备份文件。
- [x] `audit list` 能读出写入记录，且日志中不含变量值。
- [x] 安装器哈希、安装、启动、卸载全流程通过。
