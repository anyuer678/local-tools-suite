"""QuickDeck 插件稳定 API。

插件运行在独立子进程，只通过本模块提供的 API 与上下文交互；
不得读取主应用内存、数据库或内部对象。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

API_VERSION = "1"


class PluginAPIError(RuntimeError):
    pass


@dataclass
class CommandContext:
    """传给插件命令的只读上下文。"""

    command_id: str
    text: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    input_type: str = "text"

    def get(self, key: str, default: Any = None) -> Any:
        return self.params.get(key, default)

    @property
    def input(self) -> str:
        return self.text


PluginHandler = Callable[[CommandContext], str]


@dataclass
class PluginCommand:
    id: str
    name: str = ""
    description: str = ""
    input_type: str = "text"
    output_type: str = "text"
    aliases: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not str(self.id).strip():
            raise PluginAPIError("插件命令 id 不能为空")
        self.id = str(self.id).strip()
        self.name = str(self.name or self.id).strip()
        self.input_type = str(self.input_type or "text")
        self.output_type = str(self.output_type or "text")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PluginCommand":
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            description=str(data.get("description", "")),
            input_type=str(data.get("input_type", "text")),
            output_type=str(data.get("output_type", "text")),
            aliases=[str(x) for x in data.get("aliases", [])],
            keywords=[str(x) for x in data.get("keywords", [])],
        )


class PluginAPI:
    """由 runner 实例化后交给插件。"""

    def __init__(
        self,
        plugin_id: str,
        declared_commands: Sequence[PluginCommand | Mapping[str, Any]] | None = None,
    ) -> None:
        self.plugin_id = str(plugin_id)
        self.api_version = API_VERSION
        self._declared: dict[str, PluginCommand] = {}
        for item in declared_commands or []:
            command = item if isinstance(item, PluginCommand) else PluginCommand.from_dict(item)
            if command.output_type != "text":
                raise PluginAPIError(f"当前 API 仅支持 text 输出：{command.id}")
            self._declared[command.id] = command
        self._handlers: dict[str, PluginHandler] = {}

    def register_command(
        self,
        command_id: str,
        handler: PluginHandler | None = None,
        *,
        name: str | None = None,
        description: str | None = None,
        aliases: Sequence[str] | None = None,
        keywords: Sequence[str] | None = None,
    ):
        """注册命令处理器，可作装饰器使用。

        ``@api.register_command("reverse")``
        ``def reverse(ctx): ...``
        """

        def decorator(func: PluginHandler) -> PluginHandler:
            if not callable(func):
                raise PluginAPIError("插件处理器必须可调用")
            command = self._declared.get(command_id)
            if command is None:
                command = PluginCommand(
                    id=command_id,
                    name=name or command_id,
                    description=description or "",
                    aliases=list(aliases or []),
                    keywords=list(keywords or []),
                )
                self._declared[command_id] = command
            if command_id in self._handlers:
                raise PluginAPIError(f"插件命令重复注册：{command_id}")
            self._handlers[command_id] = func
            return func

        if handler is not None:
            return decorator(handler)
        return decorator

    def commands(self) -> list[PluginCommand]:
        return [self._declared[key] for key in sorted(self._declared)]

    def has_handler(self, command_id: str) -> bool:
        return command_id in self._handlers

    def execute(self, command_id: str, text: str = "", params: Mapping[str, Any] | None = None) -> str:
        handler = self._handlers.get(command_id)
        if handler is None:
            raise PluginAPIError(f"插件没有注册命令处理器：{command_id}")
        command = self._declared.get(command_id)
        context = CommandContext(
            command_id=command_id,
            text=str(text),
            params=dict(params or {}),
            input_type=command.input_type if command else "text",
        )
        try:
            result = handler(context)
        except Exception as exc:
            raise PluginAPIError(f"插件命令执行异常：{exc}") from exc
        if not isinstance(result, str):
            raise PluginAPIError("插件命令必须返回字符串")
        return result
