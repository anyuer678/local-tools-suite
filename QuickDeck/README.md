# QuickDeck

QuickDeck 是一个常驻后台、全局快捷键呼出、以键盘为中心的可扩展快捷工具台。它不是简单的程序启动器：内置计算、文本处理、编码/JSON、单位换算、时间记录、外部命令与剪贴板历史，并支持多步骤自定义动作和隔离运行的第三方插件。

- 平台：Windows 10/11
- 语言：Python 3.12
- GUI：PySide6（惰性导入；不可用时自动回退 CLI）
- 数据：SQLite + JSON，全部保存在用户数据目录

## 功能

- **命令注册表**：SQLite 持久化，支持模糊子序列搜索、别名、前缀、关键词、分类，按使用频率与最近使用排序。
- **内置工具**：安全 AST 计算器、文本去重/排序/过滤/替换/大小写/修剪、URL/Base64/Unicode/JSON 编解码、长度/质量/时间/存储单位换算、时间记录、外部命令（必须确认）、默认关闭的剪贴板历史。
- **自定义动作**：多步骤文本流水线，支持输入/输出声明、每步超时、`fail/skip/default` 错误策略、输出到剪贴板/文件/界面、从文件读取输入。
- **插件系统**：`plugin.json` 清单 + 稳定 `plugin_api`，在独立 `python -I` 子进程中运行，带超时与异常隔离；启用必须明确同意，默认不自动启用。
- **设置中心**：快捷键、浅色主题、启动行为、数据目录、搜索规则、剪贴板规则、JSON 导入导出、数据库备份/恢复/迁移、诊断日志。
- **性能**：注册表内存预加载缓存，插件按需懒加载。

## 安装

在项目目录执行：

```powershell
python -m pip install -e . --no-deps
```

需要图形界面时安装 PySide6：

```powershell
python -m pip install PySide6
```

也可以通过依赖文件安装：

```powershell
python -m pip install -r requirements.txt
```

## 运行

```powershell
python -m quickdeck              # 有 PySide6 时启动 GUI，否则回退 CLI 帮助
python -m quickdeck gui          # 启动 GUI
python -m quickdeck gui --minimized   # 启动后隐藏到托盘
python -m quickdeck --cli        # 强制 CLI 模式
```

GUI 快捷键：

| 快捷键 | 作用 |
| --- | --- |
| `Ctrl+Alt+Space`（默认，可在设置中修改） | 全局呼出/隐藏 QuickDeck |
| `Ctrl+Enter` | 执行选中命令 |
| `Esc` | 隐藏窗口到托盘 |
| `Ctrl+Q` | 退出应用 |

全局快捷键通过 Win32 `RegisterHotKey/UnregisterHotKey` 实现；注册失败会弹窗明确提示，应用仍可通过托盘/窗口使用。系统托盘不可用或开机启动写入失败同样会明确提示。

## CLI 示例

```powershell
python -m quickdeck search calc
python -m quickdeck search base64 --category 编码
python -m quickdeck calc "2 ** 10 + 24"
python -m quickdeck unit 2 gb mb
python -m quickdeck run encoding.base64_encode -i 你好
python -m quickdeck run text.sort -f input.txt --params '{"descending": true}'
python -m quickdeck time record 阅读 --note 30分钟
python -m quickdeck time history --json

# 外部命令必须先展示动作预览，再追加 --yes 确认
python -m quickdeck exec --program powershell --arg=-NoProfile --arg=-Command --arg="Get-Date"
python -m quickdeck exec --program powershell --arg=-NoProfile --arg=-Command --arg="Get-Date" --yes

python -m quickdeck actions list
python -m quickdeck actions add --file action.json
python -m quickdeck actions run note --input "待办：写周报"

python -m quickdeck plugins list
python -m quickdeck plugins enable example --consent
python -m quickdeck plugins run example char_count -i 你好abc
python -m quickdeck plugins disable example

python -m quickdeck clipboard status      # 默认关闭
python -m quickdeck clipboard enable
python -m quickdeck clipboard history --json
python -m quickdeck clipboard clear

python -m quickdeck settings show
python -m quickdeck settings set hotkey "Ctrl+Alt+K"
python -m quickdeck settings export settings-backup.json
python -m quickdeck settings import settings-backup.json
python -m quickdeck settings backup
python -m quickdeck settings restore quickdeck-backup.db
python -m quickdeck settings migrate
python -m quickdeck settings startup status
python -m quickdeck settings startup enable
python -m quickdeck diagnose --json
```

## 内置命令

| 命令 id | 说明 | 分类 |
| --- | --- | --- |
| `calc.eval` | 安全计算表达式 | 数学 |
| `text.dedupe` / `text.sort` / `text.filter` / `text.replace` | 文本去重/排序/过滤/替换 | 文本 |
| `text.case` / `text.trim` | 大小写转换/修剪 | 文本 |
| `encoding.url_encode` / `encoding.url_decode` | URL 编解码 | 编码 |
| `encoding.base64_encode` / `encoding.base64_decode` | Base64 编解码 | 编码 |
| `encoding.unicode_escape` / `encoding.unicode_unescape` | Unicode 转义/反转义 | 编码 |
| `encoding.json_format` / `encoding.json_validate` | JSON 格式化/校验（拒绝 NaN/Infinity） | 编码 |
| `unit.convert` | 长度/质量/时间/存储换算（存储按 1024） | 换算 |
| `time.record` / `time.history` | 记录/查看时间 | 时间 |
| `command.run` | 执行外部命令（需确认） | 系统 |
| `clipboard.history` / `clipboard.clear` | 剪贴板历史（默认关闭） | 剪贴板 |

计算器只接受数字、括号、`+ - * / // % **` 与少量白名单数学函数，不使用 `eval`；对指数、超大整数和非法语法有明确中文错误。

## 自定义动作

动作定义示例（保存为 `action.json`）：

```json
{
  "id": "note",
  "name": "整理待办",
  "aliases": ["todo"],
  "description": "去重、排序并加标题",
  "category": "动作",
  "output_method": "display",
  "steps": [
    {"operation": "text.dedupe", "name": "去重"},
    {"operation": "text.sort", "name": "排序", "on_error": "skip"},
    {"operation": "text.replace", "params": {"old": "-", "new": "•"}}
  ]
}
```

步骤字段：

- `operation`：内置纯文本操作，如 `text.dedupe`、`text.sort`、`text.filter`、`text.replace`、`text.upper`、`encoding.base64_encode`、`unit.convert`、`calc.eval` 等。
- `input_source`：`previous`（上一步输出，默认）或 `original`（用户原始输入）。
- `timeout`：单步超时秒数（默认 5，必须大于 0）。
- `on_error`：`fail`（默认）/`skip`/`default`；`default` 使用同名字段的默认文本。
- `input_type` / `output_type`：当前仅支持 `text`。
- 动作级 `output_method`：`display` / `clipboard` / `file`；文件输出必须提供 `output_path`。
- `input_params`：声明输入参数名，可通过 `--params` JSON 传入。

## 插件

插件目录默认为项目 `plugins/`（可用 `--plugins-dir` 或 `QUICKDECK_PLUGINS_DIR` 覆盖）。示例插件位于 `plugins/example_plugin`，启用后可使用 `plugin:example:char_count` 与 `plugin:example:reverse`。

```powershell
python -m quickdeck plugins enable example --consent
python -m quickdeck run plugin:example:char_count -i 你好abc
```

启用会执行第三方代码，因此必须显式 `--consent`；插件进程超时、异常或协议污染都会被隔离为失败结果，不会拖垮主程序。详见 `docs/plugin-guide.md`。

## 数据目录

优先级：`--data-dir` > 环境变量 `QUICKDECK_DATA_DIR` > Windows `%APPDATA%\QuickDeck` > `~/.quickdeck`。

目录内容：

- `settings.json`：用户设置
- `quickdeck.db`：命令注册表、时间记录、剪贴板历史、自定义动作、诊断事件、schema 版本
- `plugin_state.json`：插件启用状态与同意时间
- `logs/`：轮转诊断日志

## 诊断与测试

```powershell
python -m pytest -q
python -m quickdeck diagnose --json
python -m quickdeck diagnose --event "手动诊断"
```

测试覆盖计算器安全、文本/编码/单位/JSON、命令注册表、workflow、插件真实子进程、设置/数据库、热键解析与 Windows 真实注册、外部命令确认等。最近一次结果见 `docs/test-report.md`。

## 已知限制

- GUI 依赖 PySide6；未安装时自动回退 CLI。
- 仅支持 Windows 全局快捷键、系统托盘与开机启动；非 Windows 会明确报错或降级。
- 插件只支持文本输入/输出，且每次执行启动独立子进程，冷启动有额外开销；打包版会把程序自身作为插件运行器（`--plugin-runner`），插件目录放在 exe 同级的 `plugins\` 即可。
- 当前只提供浅色主题；单位换算覆盖常见类别，不包含货币等动态汇率。
- 打包后的程序需要把 `plugins\` 目录放在 exe 同级，PyInstaller 方案见 `docs/packaging.md`。
