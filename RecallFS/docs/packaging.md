# 安装、打包与离线部署

本文说明 RecallFS 的运行环境、安装方式、可选依赖、打包方案、数据库位置和离线边界。

## 1. 运行环境

- Windows 10 / 11（主要目标平台）；
- Python 3.12（`requires-python >= 3.10`，实际验证环境为 3.12.3）；
- SQLite 需带 FTS5。Python 官方 Windows 安装包自带的 SQLite 已包含 FTS5（验证环境 3.45.1）；
- 核心运行不需要任何第三方库，也不需要网络。

## 2. 安装方式

### 2.1 可编辑安装（推荐开发使用）

```powershell
cd RecallFS
python -m pip install -e .
```

这会注册 `recallfs` 控制台命令，并让 `python -m recallfs` 在任意目录可用。依赖声明为 `dependencies = []`，不会自动安装任何第三方包。

### 2.2 不安装，直接用源码运行

```powershell
cd RecallFS
$env:PYTHONPATH = "src"
python -m recallfs --help
```

测试也使用同一机制：`pyproject.toml` 中 `[tool.pytest.ini_options] pythonpath = ["src"]`。

### 2.3 可选依赖

```powershell
# 按需安装，可组合
python -m pip install -e ".[pdf,docx,pptx]"
python -m pip install -e ".[gui]"
python -m pip install -e ".[bench]"
python -m pip install -e ".[pdf,docx,pptx,gui,bench]"
```

| extra | 包 | 用途 | 缺失时 |
| --- | --- | --- | --- |
| `pdf` | `pypdf>=4.0` | PDF 文本与页码 | PDF 标记 `unsupported` |
| `docx` | `python-docx>=1.1` | DOCX 段落/表格 | DOCX 标记 `unsupported` |
| `pptx` | `python-pptx>=0.6` | PPTX 幻灯片文本 | PPTX 标记 `unsupported` |
| `gui` | `PySide6>=6.5` | 桌面界面 | `python -m recallfs` 回退 CLI |
| `bench` | `psutil>=5.9` | 更精确的内存统计 | 使用 Windows psapi / `resource` 回退 |

`requirements.txt` 列出的是全部可选增强与 pytest，**不是核心运行依赖**。

## 3. 数据库位置与备份

默认：

```
%USERPROFILE%\.recallfs\recallfs.sqlite3
```

可通过两种方式改变：

```powershell
$env:RECALLFS_HOME = "D:\recallfs-data"
python -m recallfs --db D:\recallfs-data\custom.sqlite3 status
```

数据库使用 WAL 模式，运行时可能同时存在 `recallfs.sqlite3-wal` 与 `recallfs.sqlite3-shm`。备份时建议先关闭程序，或使用 SQLite 的在线备份；直接复制三个文件也可在无写入时工作。

数据库只保存索引与配置（根目录、集合、收藏、最近搜索、语义向量），**不保存源文件的副本**。删除数据库不会影响任何源文件。

重建与清理：

```powershell
python -m recallfs rebuild --root D:\work\notes
python -m recallfs rebuild --reset          # 索引版本不兼容时重置结构
python -m recallfs index --clear-root D:\work\notes
```

以上命令都只删除数据库中的记录或重建结构，不会删除磁盘上的资料。

## 4. 打包为单文件 zipapp

`src` 目录本身可以直接打成可执行的 `.pyz`：

```powershell
python -m zipapp src -o dist\recallfs.pyz -m "recallfs.__main__:main"
python dist\recallfs.pyz --help
python dist\recallfs.pyz --db D:\idx\recallfs.sqlite3 index --root D:\work\notes
```

注意：

- zipapp 只包含 RecallFS 自身代码，不包含 PySide6、pypdf 等第三方包；
- 运行机器上仍需有 Python 3.10+ 与 FTS5；
- 若目标机器已安装 PySide6，`python recallfs.pyz` 仍可启动 GUI；否则自动回退 CLI；
- 第三方解析器同理：目标机器未安装时对应格式按 `unsupported` 处理。

该方式已在当前环境实际验证 `--help` 可运行。

## 5. 打包为独立 exe（PyInstaller）

PyInstaller 不是项目依赖，需要单独安装。GUI 打包必须在目标平台（Windows）上进行。

### 5.1 准备入口脚本

PyInstaller 需要一个普通入口文件，避免直接打包包内 `__main__.py` 时的相对导入问题。项目根目录已提供 `build_entry.py`：

```python
from recallfs.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
```

### 5.2 GUI 版

```powershell
python -m pip install pyinstaller
pyinstaller --noconfirm --clean --windowed --name RecallFS `
  --paths src `
  --collect-submodules recallfs `
  --hidden-import PySide6.QtCore `
  --hidden-import PySide6.QtGui `
  --hidden-import PySide6.QtWidgets `
  --hidden-import pypdf `
  build_entry.py
```

产物在 `dist\RecallFS\RecallFS.exe`。`--windowed` 会隐藏控制台，因此建议确保 GUI 依赖已安装。已实测：目录模式约 189.8 MB，冷启动约 0.66 秒；构建时显式加入 `--hidden-import pypdf` 后，PDF 文本提取在冻结版中可用（取决于构建环境是否安装 pypdf）。

### 5.3 CLI 版

```powershell
pyinstaller --noconfirm --clean --onefile --console --name recallfs-cli `
  --paths src `
  --collect-submodules recallfs `
  build_entry.py
```

产物为 `dist\recallfs-cli.exe`，可直接执行 `recallfs-cli.exe --help`。

### 5.4 可选解析器

若希望 exe 自带 PDF/DOCX/PPTX 能力，在打包环境中先安装对应包，PyInstaller 会自动收集；否则运行时按缺失处理。也可以显式添加：

```powershell
--hidden-import pypdf --hidden-import docx --hidden-import pptx
```

本次交付已实际构建并启动验证四个项目的 onedir 产物；上述命令即为实测命令。干净虚拟机上的安装向导与签名验证仍需在发布阶段补做。

## 6. 离线与隐私边界

- 核心模块（`filesystem_watcher`、`document_parser`、`content_chunker`、`fulltext_index`、`index_scheduler`、`search_engine`、`evidence_locator`、`project_collections`、`semantic_index`）不导入 `requests`、`socket`、`urllib`、`http.client`；
- `tests/test_cli.py` 通过静态导入检查与运行时阻断（patch `socket`）验证这一点；
- 语义索引是本地哈希向量：`network_required=False`、`auto_download=False`，不上传文本，不下载模型；
- GUI 仅使用本机文件系统与 SQLite，不发起网络请求；
- 打开原文件调用系统默认程序（Windows `os.startfile`），不复制、不上传。

## 7. 安装器（Inno Setup）

已提供 `packaging\installer.iss`，编译命令：

```powershell
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" packaging\installer.iss
```

产物：`release\installers\RecallFS-0.1.0-setup.exe`（54.2 MB）。按用户安装到
`%LOCALAPPDATA%\Programs\RecallFS`，无需管理员权限；构建时已打入 `pypdf`，PDF 提取可用。

实测：静默安装 → 启动窗口正常（约 0.91 秒）→ 静默卸载后目录与卸载注册项清除。
安装器未签名；卸载不会删除索引数据库或被索引的源文件。

exe 构建使用 `--icon assets\app.ico`，安装器通过 `SetupIconFile=assets\app.ico` 使用同一图标。

## 8. 卸载与清理

```powershell
python -m pip uninstall recallfs
```

可选删除数据库目录 `%USERPROFILE%\.recallfs`。卸载与删除数据库都不会删除被索引的源文件。

## 8. 常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `No module named recallfs` | 未安装且未设置 `PYTHONPATH` | `pip install -e .` 或 `$env:PYTHONPATH="src"` |
| 启动后只有 CLI | PySide6 未安装或导入失败 | 安装 `.[gui]`，或继续使用 CLI |
| PDF/DOCX/PPTX 显示 `unsupported` | 可选解析器缺失 | 安装对应 extra 后执行 `index --retry-failed` |
| 索引版本不一致 | 数据库由旧版本程序创建 | `rebuild --reset`（会清除索引与集合/收藏配置） |
| 搜索结果全部失效 | 文件在索引后被修改或删除 | 重新执行 `index` |
| `database is locked` | 另一个进程长期持有写事务 | 关闭其他实例后重试；连接已使用 `BEGIN IMMEDIATE` 串行化 |
| 内存统计为 `psapi`/`resource` | 未安装 psutil | 安装 `.[bench]` 可获得更精确数据 |
