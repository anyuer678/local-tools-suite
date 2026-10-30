# QuickDeck 插件开发指南

QuickDeck 插件是放在独立目录中的 Python 包，由 `plugin.json` 描述，主程序通过稳定 API 在隔离子进程中调用。插件不能访问主应用内存、数据库或内部对象。

## 1. 目录结构

```text
plugins/
└─ example_plugin/
   ├─ plugin.json      # 清单，必需
   └─ plugin.py        # 入口，默认由 entry 指定
```

插件目录名可以任意，但清单 `id` 必须唯一且符合 `^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`。

## 2. plugin.json

示例：

```json
{
  "id": "example",
  "name": "示例插件",
  "version": "1.0.0",
  "api_version": "1",
  "entry": "plugin.py",
  "commands": [
    {
      "id": "char_count",
      "name": "统计字符数",
      "description": "统计输入文本的字符数与行数",
      "input_type": "text",
      "output_type": "text",
      "aliases": ["字数", "字符数"],
      "keywords": ["统计", "文本", "字符"]
    }
  ]
}
```

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `id` | 是 | 插件唯一 id；字母/数字开头，可含点、下划线、短横线，最长 64 |
| `name` | 是 | 展示名称 |
| `version` | 是 | 插件自身版本，字符串 |
| `api_version` | 是 | 主版本必须为 `1`；`"1"`、`"1.0"` 均可 |
| `entry` | 是 | 入口文件，必须位于插件目录内，不能指向外部路径 |
| `commands` | 是 | 非空数组，每项描述一个命令 |

命令字段：

| 字段 | 说明 |
| --- | --- |
| `id` | 命令 id，插件内唯一 |
| `name` | 展示名称，缺省等于 id |
| `description` | 描述，参与搜索 |
| `input_type` | 当前只支持 `text` / `string` |
| `output_type` | 当前只支持 `text` |
| `aliases` | 别名数组，参与搜索 |
| `keywords` | 关键词数组，参与搜索 |

清单在启用前只做 JSON 解析和校验，不执行插件代码（懒加载）。

## 3. 插件入口

推荐使用 `register(api)`：

```python
from quickdeck.plugin_api import CommandContext, PluginAPI


def register(api: PluginAPI) -> None:
    @api.register_command("char_count")
    def char_count(ctx: CommandContext) -> str:
        text = ctx.text
        return f"字符数：{len(text)}，行数：{len(text.splitlines())}"
```

也兼容：

- `setup(api)` 函数；
- 模块中与命令 id 同名的函数；
- `cmd_<id>`、`command_<id>`、`run_<id>` 命名的函数。

处理器要求：

- 接收一个 `CommandContext`；
- 返回 `str`，否则协议层报错；
- 抛出的异常会被捕获并转为失败结果。

`CommandContext` 字段：

| 字段/方法 | 说明 |
| --- | --- |
| `command_id` | 当前命令 id |
| `text` / `input` | 用户输入文本 |
| `params` | 参数字典 |
| `input_type` | 输入类型 |
| `get(key, default)` | 读取参数 |

`PluginAPI`：

- `register_command(command_id, handler=None, ...)`：注册处理器，可作装饰器；
- `commands()`：已声明命令列表；
- `has_handler(command_id)`：是否已注册；
- `execute(command_id, text, params)`：内部执行入口（一般不需要直接调用）；
- `plugin_id`、`api_version`：只读属性。

`PluginAPI` 与 `plugin_api.py` 是稳定接口；插件只应从 `quickdeck.plugin_api` 导入这些类型。

## 4. 启用、禁用与同意

```powershell
python -m quickdeck plugins list
python -m quickdeck plugins show example
python -m quickdeck plugins enable example --consent
python -m quickdeck plugins disable example
python -m quickdeck plugins uninstall example --consent
python -m quickdeck plugins uninstall example --consent --delete-files
```

安全规则：

- 启用第三方代码必须显式 `--consent`，否则返回 2；
- 同意时间写入数据目录 `plugin_state.json`；
- 只有同时存在 `enabled` 与 `consented_at` 的插件才会注册命令；
- `settings.plugins.auto_enable` 固定为 `false`，设置校验会拒绝自动启用；
- 卸载默认只清理状态与命令注册，`--delete-files` 才会删除插件目录。

注册到命令注册表的命令 id 为：

```
plugin:<插件id>:<命令id>
```

例如 `plugin:example:char_count`。命令分类固定为「插件」，并附加插件 id 与 `plugin`、`插件` 关键词。

## 5. 子进程协议与隔离

主程序执行：

```text
python -I <quickdeck/plugin_runner.py> <plugin_dir>
```

冻结打包版没有外部 Python，此时主程序改为以自身可执行文件启动：

```text
QuickDeck.exe --plugin-runner <plugin_dir>
```

打包入口检测到 `--plugin-runner` 后直接分流到同一个 `plugin_runner`，因此隔离、
超时与协议完全一致。插件目录默认取 exe 同级的 `plugins\`，也可用
`QUICKDECK_PLUGINS_DIR` 覆盖。该模式已随打包产物实测通过。

- `-I` 隔离模式忽略用户 site-packages 与环境变量；
- stdin 传入 JSON：`{"command_id": "...", "text": "...", "params": {...}}`
- 插件目录作为工作目录；
- 使用最小环境变量白名单（SYSTEMROOT、PATH、TEMP、USERPROFILE 等）；
- Windows 下使用 `CREATE_NO_WINDOW`；
- 默认超时 5 秒（`settings.plugins.timeout_seconds`，单次调用还可用 `--timeout` 覆盖）；
- 超时或非零退出码均返回结构化失败；
- stderr 保留最后一段用于诊断，完整 traceback 会写入 stderr。

输出协议：

- runner 在插件执行期间把标准输出重定向到 stderr，因此插件里的 `print()` 不会污染协议；
- 最终结果以单独一行输出，前缀为 `__QUICKDECK_RESULT__`，后接 JSON；
- 成功：`{"ok": true, "output": "结果"}`
- 失败：`{"ok": false, "error": "错误信息"}`
- stdin/stdout/stderr 被显式设为 UTF-8，支持中文输入输出。

插件进程无法访问主程序的 SQLite、设置、剪贴板历史或已注册命令；需要的能力应通过参数传入或由插件自身实现。

## 6. 调试

```powershell
# 查看清单和启用状态
python -m quickdeck plugins list --json

# 直接执行（仍需先启用并同意）
python -m quickdeck plugins run example char_count -i "你好abc"
python -m quickdeck plugins run example char_count -i "你好abc" --timeout 3

# 执行失败会打印错误与 stderr 末尾
python -m quickdeck plugins run example char_count -i "你好abc"
```

常见错误：

| 现象 | 原因 |
| --- | --- |
| `缺少 plugin.json` | 目录中没有清单 |
| `插件 API 版本不兼容` | `api_version` 主版本不是 `1` |
| `插件入口不存在` | `entry` 路径错误或文件缺失 |
| `插件入口不能指向插件目录之外` | `entry` 使用了 `..` 或绝对路径 |
| `插件没有注册命令处理器` | 清单声明了命令，但入口没有对应处理器 |
| `插件命令执行异常` | 处理器抛异常，查看 stderr traceback |
| `插件执行超时` | 超过 `timeout`，检查阻塞操作 |

## 7. 限制与安全建议

- 当前只支持文本输入、文本输出；不支持二进制、图片或流式数据。
- 每次命令调用都会启动一个新进程，冷启动开销明显；插件应保持轻量。
- 插件代码拥有当前用户权限，请只启用可信来源；启用前应审阅 `plugin.py`。
- 插件不能依赖主程序内部模块；只使用 `quickdeck.plugin_api` 的稳定接口。
- 插件应在自身目录内工作，避免写入系统目录；如需持久化，请使用插件目录或用户明确指定的路径。
- 插件输出会被当作纯文本展示，不会自动执行；若需要执行外部命令，请在插件内自行实现并做好确认与超时。
