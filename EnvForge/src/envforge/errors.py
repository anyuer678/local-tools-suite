"""统一异常类型：错误信息必须说明“发生了什么、影响了什么、可以怎么做”。"""

from __future__ import annotations


class EnvForgeError(RuntimeError):
    """所有可预期业务错误的基类（CLI 会转为非 0 退出码，不打印堆栈）。"""


class ValidationError(EnvForgeError):
    """输入不合法。"""


class NotFoundError(EnvForgeError):
    """目标记录不存在。"""


class ConfirmationRequired(EnvForgeError):
    """该操作会修改用户环境或暴露敏感值，必须显式确认。"""


class EnvAccessError(EnvForgeError):
    """环境变量读取或写入失败（权限、平台不支持、注册表不可用）。"""


class EnvWriteFailure(EnvAccessError):
    """写入后回读校验不通过，或写入中途异常；已尝试用写入前快照自动回滚。"""

    def __init__(
        self,
        message: str,
        *,
        scope: str = "",
        pre_snapshot_id: int | None = None,
        failed_keys: list[str] | None = None,
        rollback_ok: bool = False,
        partial: bool = False,
    ) -> None:
        super().__init__(message)
        self.scope = scope
        self.pre_snapshot_id = pre_snapshot_id
        self.failed_keys = list(failed_keys or [])
        self.rollback_ok = rollback_ok
        self.partial = partial
