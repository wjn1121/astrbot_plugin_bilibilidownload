"""平台注册表：匹配、启用过滤、能力查询。

编排层只跟这里打交道，不认识任何具体平台——这是「新增平台不动主流程」的关键。

加载策略（照搬 denia 踩过的坑）：平台模块**逐个 import、单个失败只影响自己**。
denia 早期把 B 站解析器裸 import 在包顶层，B 站依赖装不上时会把另外 10 个
与 B 站毫无关系的平台一起带死。这里用 ``importlib`` 逐个加载并记录失败原因，
面板上能看到"哪个平台没起来、为什么"。
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Iterable, Sequence
from typing import Any

from astrbot.api import logger

from ..media import MediaRef
from .base import BasePlatform

LOG_TAG = "[bilibili-download]"

# 内置平台模块（相对 core.platforms）。新增平台 = 加一个文件 + 这里加一行。
# 顺序即"同一段文本先问谁"的默认顺序；用户可以用 enabled_platforms 改。
_BUILTIN_MODULES: tuple[str, ...] = (
    "bilibili",
    "douyin",
    "xiaohongshu",
)

# 加载失败的平台：{模块名: 错误描述}；供面板展示，不影响其它平台
_load_errors: dict[str, str] = {}

_loaded = False

# 匹配不到任何平台时的兜底触发正则：只用来过滤"带链接的消息"，
# 真正是否处理由各平台的 match() 决定，所以宽一点没有副作用。
FALLBACK_TRIGGER_PATTERN = r"https?://[^\s\"'<>]+"


def load_builtin(*, force: bool = False) -> tuple[list[str], dict[str, str]]:
    """导入全部内置平台模块，返回 (成功的平台名, 失败原因)。

    可重复调用（幂等）；``force=True`` 时重新尝试加载上次失败的模块，
    用于用户在面板上"重试"（例如刚补装了某个可选依赖）。
    """
    global _loaded
    if _loaded and not force:
        return list_platform_names(), dict(_load_errors)

    for module in _BUILTIN_MODULES:
        if not force and module in _load_errors:
            continue
        try:
            importlib.import_module(f"{__package__}.{module}")
            _load_errors.pop(module, None)
        except Exception as exc:  # noqa: BLE001 - 单平台失败不能拖垮整个插件
            _load_errors[module] = f"{type(exc).__name__}: {exc}"
            logger.warning(
                f"{LOG_TAG} 平台模块 {module} 加载失败，该平台不参与解析："
                f"{type(exc).__name__}: {exc}"
            )

    _loaded = True
    if _load_errors:
        logger.warning(
            f"{LOG_TAG} 有 {len(_load_errors)} 个平台加载失败："
            f"{'、'.join(_load_errors)}（其余平台不受影响）"
        )
    return list_platform_names(), dict(_load_errors)


def list_platform_names() -> list[str]:
    """已注册的平台标识，按注册顺序。"""
    return list(BasePlatform.registry)


def get(name: str) -> type[BasePlatform] | None:
    return BasePlatform.registry.get((name or "").strip().lower())


def _is_enabled(name: str, enabled: Sequence[str] | None) -> bool:
    """``enabled`` 为空视为「全部启用」。

    与现有的 ``group_whitelist``（留空 = 所有群生效）保持同一种直觉，
    避免用户在配置里清空列表后整个插件突然什么都不响应。
    """
    if not enabled:
        return True
    wanted = {str(item).strip().lower() for item in enabled if str(item).strip()}
    return not wanted or name in wanted


def ordered(enabled: Sequence[str] | None = None) -> list[type[BasePlatform]]:
    """按启用状态过滤后的平台类列表。

    顺序优先级：配置里 ``enabled`` 的书写顺序 → 未列出的按注册顺序。
    这样用户可以用配置顺序决定"同一段文本先问哪个平台"。
    """
    classes = list(BasePlatform.registry.values())
    if not enabled:
        return classes

    rank: dict[str, int] = {}
    for index, item in enumerate(enabled):
        key = str(item).strip().lower()
        if key and key not in rank:
            rank[key] = index
    fallback = len(rank)
    return sorted(
        (cls for cls in classes if _is_enabled(cls.name, enabled)),
        key=lambda cls: rank.get(cls.name, fallback),
    )


def match(
    text: str,
    *,
    enabled: Sequence[str] | None = None,
) -> tuple[type[BasePlatform], MediaRef] | None:
    """依次询问各平台；第一个认出来的胜出。

    平台 ``match()`` 约定不抛异常，但真抛了也不能让整条消息挂掉——这里逐个兜住，
    只记 debug 日志。
    """
    if not text:
        return None

    for cls in ordered(enabled):
        try:
            ref = cls.match(text)
        except Exception as exc:  # noqa: BLE001 - 第三方/新平台的匹配 bug 不该拖垮主流程
            logger.debug(f"{LOG_TAG} 平台 {cls.name} 匹配异常（忽略）：{type(exc).__name__}: {exc}")
            continue
        if ref is not None:
            if not ref.platform:
                ref.platform = cls.name
            return cls, ref
    return None


def combined_trigger_regex() -> str:
    """所有平台触发正则的并集，供 ``@filter.regex`` 使用。

    没有声明 ``trigger_pattern`` 的平台不参与（它们通常靠自己的 ``match()``
    在卡片路径里被调用，不依赖消息过滤器）。
    """
    patterns: list[str] = []
    for cls in BasePlatform.registry.values():
        pattern = (getattr(cls, "trigger_pattern", "") or "").strip()
        if pattern:
            patterns.append(f"(?:{pattern})")
    if not patterns:
        return FALLBACK_TRIGGER_PATTERN
    joined = "|".join(patterns)
    # 提前编译一次：正则写错时在加载期就暴露，而不是等第一条消息进来才报错
    try:
        re.compile(joined)
    except re.error as exc:
        logger.error(f"{LOG_TAG} 合并后的触发正则无效（{exc}），退回通用 URL 匹配")
        return FALLBACK_TRIGGER_PATTERN
    return joined


def describe_all(enabled: Sequence[str] | None = None) -> list[dict[str, Any]]:
    """面板「平台」页签的数据：每个平台的能力位与启停状态。"""
    enabled_set = {str(item).strip().lower() for item in (enabled or []) if str(item).strip()}
    rows: list[dict[str, Any]] = []
    for cls in BasePlatform.registry.values():
        row = cls.describe()
        row["enabled"] = _is_enabled(cls.name, enabled)
        row["in_config"] = (not enabled_set) or cls.name in enabled_set
        rows.append(row)

    # 加载失败的模块也列出来，否则用户只会看到"少了几个平台"却不知道原因
    for module, error in _load_errors.items():
        rows.append(
            {
                "name": module,
                "display": f"{module}（加载失败）",
                "capabilities": [],
                "enabled": False,
                "in_config": False,
                "error": error,
            }
        )
    return rows


def load_errors() -> dict[str, str]:
    return dict(_load_errors)


def reset_for_tests() -> None:
    """清空注册表（仅测试用）。"""
    global _loaded
    BasePlatform.registry.clear()
    _load_errors.clear()
    _loaded = False


def iter_names(names: Iterable[str]) -> str:
    """把平台名列表拼成一句人话，给日志与提示语用。"""
    items = [name for name in names if name]
    return "、".join(items) if items else "（无）"


__all__ = [
    "FALLBACK_TRIGGER_PATTERN",
    "combined_trigger_regex",
    "describe_all",
    "get",
    "iter_names",
    "list_platform_names",
    "load_builtin",
    "load_errors",
    "match",
    "ordered",
    "reset_for_tests",
]
