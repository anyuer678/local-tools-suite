# QuickDeck 打包与部署

## 1. 运行环境

- Windows 10/11，Python 3.12
- 运行依赖：PySide6（仅 GUI 需要，建议 `>=6.5`）
- 测试依赖：pytest（开发环境）
- 核心逻辑只使用标准库，CLI 在无 PySide6 环境也可用

推荐使用项目虚拟环境：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e . --no-deps
python -m pip install PySide6
```

安装后会生成 `quickdeck.exe` 风格的命令入口（`quickdeck`），同时仍可使用 `python -m quickdeck`。

## 2. 开发/便携运行

```powershell
# 安装为可编辑包（推荐）
python -m pip install -e . --no-deps

python -m quickdeck --help
python -m quickdeck gui
python -m quickdeck gui --minimized
```

未安装时可在项目根临时设置 `PYTHONPATH=src`：

```powershell
$env:PYTHONPATH = "src"
python -m quickdeck --help
```

## 3. 数据目录与升级

数据目录优先级：

1. 命令行 `--data-dir`
2. 环境变量 `QUICKDECK_DATA_DIR`
3. Windows `%APPDATA%\QuickDeck`
4. `~/.quickdeck`

升级前建议备份数据库：

```powershell
python -m quickdeck settings export settings-backup.json
python -m quickdeck settings backup
# 新版本首次启动会自动迁移 schema；也可手动执行
python -m quickdeck settings migrate
# 需要回滚时
python -m quickdeck settings restore quickdeck-backup.db
```

恢复前请先退出正在运行的 QuickDeck，避免 SQLite 文件被占用。

## 4. 插件目录

插件目录优先级：

1. 命令行 `--plugins-dir`
2. 环境变量 `QUICKDECK_PLUGINS_DIR`
3. 项目 `plugins/`
4. 数据目录下的 `plugins/`

示例插件 `example_plugin` 随项目提供，但必须显式启用：

```powershell
python -m quickdeck plugins enable example --consent
```

## 5. 开机启动

仅在 Windows 生效，写入当前用户注册表：

```
HKCU\Software\Microsoft\Windows\CurrentVersion\Run
值名：QuickDeck
```

命令行方式：

```powershell
python -m quickdeck settings startup status
python -m quickdeck settings startup enable
python -m quickdeck settings startup disable
```

源码运行时写入 `pythonw.exe -m quickdeck --minimized`；PyInstaller 冻结运行时写入 `QuickDeck.exe --minimized`。写入失败会抛出明确错误，不会假装成功。

## 6. PyInstaller 打包（建议 onedir）

仓库已内置打包入口 `packaging/launcher.py`，可按以下步骤生成可分发目录。

### 6.1 准备入口

项目中已提供 `packaging/launcher.py`（不要放进 `src/quickdeck`）：

```python
import sys

from quickdeck.cli import main

if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--plugin-runner":
        from quickdeck import plugin_runner

        sys.argv = [sys.argv[0], sys.argv[2]]
        raise SystemExit(plugin_runner.main())
    raise SystemExit(main())
```

打包时必须加入 `--hidden-import quickdeck.plugin_runner`，否则冻结版无法把自身
当作插件运行器。

### 6.2 安装打包工具

```powershell
python -m pip install pyinstaller
```

### 6.3 生成 onedir 目录

```powershell
pyinstaller --noconfirm --clean --onedir --windowed `
  --name QuickDeck `
  --paths src `
  --collect-submodules quickdeck `
  --hidden-import quickdeck.ui.qt_app `
  --hidden-import quickdeck.plugin_runner `
  --hidden-import PySide6.QtSvg `
  packaging\launcher.py
```

生成结果位于 `dist\QuickDeck\`。`--windowed` 不显示控制台窗口；如需保留 CLI 控制台，改用 `--console` 或额外生成一个控制台版本。已实测：目录模式约 124.6 MB，冷启动约 0.80 秒，`QuickDeck.exe gui` 可正常显示窗口。冻结版插件已实测：无需外部 Python，复制 `plugins\` 后可直接启用并执行。

### 6.4 放置插件与资源

PyInstaller 将 Python 包放入 `dist\QuickDeck\_internal\`，而程序默认在可执行文件同级查找 `plugins\`。部署时把插件复制到：

```powershell
Copy-Item -Recurse plugins dist\QuickDeck\plugins
```

或通过环境变量/快捷方式参数指定：

```powershell
$env:QUICKDECK_PLUGINS_DIR = "D:\QuickDeckPlugins"
```

### 6.5 安装器（Inno Setup）

已提供 `packaging\installer.iss`，编译命令：

```powershell
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" packaging\installer.iss
```

产物：`release\installers\QuickDeck-0.1.0-setup.exe`（66.3 MB）。安装到
`%LOCALAPPDATA%\Programs\QuickDeck`，包含主程序、`cli\QuickDeckCLI.exe` 与 `plugins\`；
开始菜单提供 GUI 与 CLI 两个入口，无需管理员权限。

实测：静默安装 → 安装版 CLI 启用并执行示例插件成功（“字符数 16，行数 1”）→ GUI 启动
（约 0.86 秒）→ 静默卸载后目录与卸载注册项清除。安装器未签名。

exe 构建使用 `--icon assets\app.ico`，安装器通过 `SetupIconFile=assets\app.ico` 使用同一图标。

卸载时会询问是否同时删除 `%APPDATA%\QuickDeck` 中的配置、历史与插件数据，默认选择“保留”；
静默卸载不弹窗，始终保留数据。已实测静默卸载后安装目录移除、数据目录仍在。

### 6.6 可选：图标与版本信息

```powershell
pyinstaller --noconfirm --clean --onedir --windowed `
  --name QuickDeck --icon assets\quickdeck.ico `
  --version-file packaging\version_info.txt `
  --paths src --collect-submodules quickdeck `
  --hidden-import quickdeck.ui.qt_app `
  packaging\launcher.py
```

### 6.6 单文件模式（可选）

```powershell
pyinstaller --noconfirm --clean --onefile --windowed `
  --name QuickDeck --paths src `
  --collect-submodules quickdeck `
  --hidden-import quickdeck.ui.qt_app `
  packaging\launcher.py
```

单文件模式启动时会解压到临时目录，启动更慢；插件目录需通过 `QUICKDECK_PLUGINS_DIR` 显式指定。

## 7. 冻结模式的插件限制

插件执行使用：

```
python -I <plugin_runner.py> <plugin_dir>
```

因此冻结后的 QuickDeck 需要满足：

- 存在可用的 Python 3.12 解释器（`sys.executable` 不能是打包后的 GUI 程序）；
- `plugin_runner.py` 需随程序一起可访问。

当前版本未内置冻结环境的解释器发现逻辑。若需要分发带插件的单文件程序，建议：

1. 随包携带独立 Python 运行时与 `plugin_runner.py`，并通过定制启动器设置进程解释器；或
2. 以源码 + 虚拟环境方式部署，保留 `python -m quickdeck` 运行方式；或
3. 仅在冻结版本中使用内置工具与自定义动作，插件功能在源码部署中启用。

## 8. 发布检查清单

- [ ] `python -m pytest -q` 全绿
- [ ] `python -m quickdeck --help` 正常输出
- [ ] `python -m quickdeck gui --minimized` 可启动并隐藏到托盘
- [ ] 全局快捷键可呼出/隐藏，注销后不残留
- [ ] `settings backup/restore/migrate` 在副本数据目录验证通过
- [ ] 插件启用需 `--consent`，超时与异常不会导致主进程崩溃
- [ ] 数据目录、日志目录可写，卸载时明确是否保留用户数据
- [ ] 发布包内包含 `plugins\`（如需要）与第三方许可说明
