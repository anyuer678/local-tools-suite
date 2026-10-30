"""QuickDeck 示例插件：统计字符数 / 反转文本。"""

from quickdeck.plugin_api import CommandContext, PluginAPI


def register(api: PluginAPI) -> None:
    @api.register_command("char_count")
    def char_count(ctx: CommandContext) -> str:
        text = ctx.text
        lines = len(text.splitlines())
        return f"字符数：{len(text)}，行数：{lines}"

    @api.register_command("reverse")
    def reverse(ctx: CommandContext) -> str:
        return ctx.text[::-1]
