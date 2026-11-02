"""平台层的异常类型。

标题里的提示语会**原样发到群里**，所以 ``str(exc)`` 必须是用户能看懂的一句话；
技术细节（状态码、堆栈、原始响应片段）走 ``detail`` 字段，只进日志与 ``/bdlstatus``。
这条规则沿用现有 B 站 412 风控的处理方式，见 ``core/bili_api.py``。
"""

from __future__ import annotations


class PlatformError(Exception):
    """一次平台解析失败。"""

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail or message


class PlatformUnavailable(PlatformError):
    """平台不可用（被配置关闭、依赖缺失、或该平台尚未实现）。"""


class MediaNotFound(PlatformError):
    """内容不存在或已删除（404 / 稿件不可见）。"""


class MediaProtected(PlatformError):
    """需要登录或风控拦截（412 / 需要 xsec_token / 试看内容）。"""


__all__ = [
    "MediaNotFound",
    "MediaProtected",
    "PlatformError",
    "PlatformUnavailable",
]
