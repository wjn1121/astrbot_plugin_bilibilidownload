"""平台层入口。

编排层（``main.py`` / ``core/dispatch.py``）只从这里取东西：

    from .core import platforms
    platforms.load_builtin()
    hit = platforms.match(text, enabled=self.enabled_platforms)

**新增一个平台**：
1. 在 ``core/platforms/`` 下新建 ``<平台>.py``，继承 ``BasePlatform``；
2. 声明 ``name`` / ``display`` / ``capabilities`` / ``trigger_pattern``；
3. 实现 ``match()`` 与 ``parse()``；需要额外换直链时覆写 ``resolve_media()``；
4. 在 ``registry._BUILTIN_MODULES`` 里加一行模块名。

主流程不需要任何改动。
"""

from __future__ import annotations

from .base import BasePlatform, ParseHint
from .errors import MediaNotFound, MediaProtected, PlatformError, PlatformUnavailable
from .registry import (
    combined_trigger_regex,
    describe_all,
    get,
    list_platform_names,
    load_builtin,
    load_errors,
    match,
    ordered,
)

__all__ = [
    "BasePlatform",
    "MediaNotFound",
    "MediaProtected",
    "ParseHint",
    "PlatformError",
    "PlatformUnavailable",
    "combined_trigger_regex",
    "describe_all",
    "get",
    "list_platform_names",
    "load_builtin",
    "load_errors",
    "match",
    "ordered",
]
