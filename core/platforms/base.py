"""平台解析器的基类与自动注册。

**这就是「新增平台」的缺口**：加一个 ``core/platforms/<平台>.py``，继承 ``BasePlatform``、
声明 ``name`` / ``display`` / ``capabilities``，实现 ``match()`` 与 ``parse()``，
然后在 ``core/platforms/registry.py`` 的 ``_BUILTIN_MODULES`` 里加一行模块名即可。
编排层、卡片层、总结层都不需要改。

参考实现取自服务器上的 ``astrbot_plugin_denia_share``（作者 xiaoxi2760）的
``BaseParser``：``__init_subclass__`` 自动收集 + ``handle()`` 装饰器声明 URL 正则。
这里简化了两点：
1. 不用装饰器声明正则，改为每个平台实现 ``match()``——多平台的匹配逻辑差异太大
   （有的要认口令、有的要跟短链），统一成函数比统一成正则更省事；
2. 不把下载与发送放进平台层：平台只负责"拿到直链"，怎么发由编排层决定，
   这样 ``file / url / auto`` 三种发送模式对全部平台一致生效。
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar

import aiohttp

from ..media import MediaInfo, MediaItem, MediaRef


@dataclass(slots=True)
class ParseHint:
    """编排层给平台的"提示"，避免平台去读全局配置。

    平台只关心自己用得上哪些字段，用不上的直接忽略——这样加平台时不需要在
    配置 schema 里加一堆只有某个平台认识的项。
    """

    reply_limit: int = 10        # 需要多少条热评（0 = 不需要）
    want_summary: bool = True    # 是否需要为 AI 总结准备额外输入
    want_subtitle: bool = False  # 是否尝试取字幕（仅 B 站类平台有意义）


class BasePlatform(ABC):
    """一个平台的解析器。子类自动注册进 ``BasePlatform.registry``。"""

    #: 平台标识（英文小写，用于配置项与去重 key，发布后不要改名）
    name: ClassVar[str] = ""
    #: 展示名（中文，面板与卡片上用）
    display: ClassVar[str] = ""
    #: 能力位，见 ``core.media`` 的 CAP_*
    capabilities: ClassVar[frozenset[str]] = frozenset()
    #: 消息过滤器用的触发正则。留空表示"不参与 @filter.regex"，只靠自己的 match()
    #: 在卡片/命令路径里被调用。写宽一点没关系——最终是否处理由 match() 决定。
    trigger_pattern: ClassVar[str] = ""

    #: name -> 平台类；由 __init_subclass__ 填充
    registry: ClassVar[dict[str, type["BasePlatform"]]] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        # 抽象基类本身（或中间层）不注册：没有 name，或是还没实现全部抽象方法的半成品
        if not cls.name or inspect.isabstract(cls):
            return
        BasePlatform.registry[cls.name] = cls

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        timeout: int = 20,
        verbose: bool = False,
    ) -> None:
        self.session = session
        self.timeout = max(5, int(timeout))
        self.verbose = verbose

    @classmethod
    def create(cls, session: aiohttp.ClientSession, *, settings: dict[str, Any]) -> "BasePlatform":
        """从配置快照构造实例。

        默认只取两个所有平台都有的键；需要平台专属配置（如 B 站的 sessdata）的
        平台覆写本方法自行读取——这样 ``main.py`` 永远不需要认识某个平台配置项。
        """
        return cls(
            session,
            timeout=int(settings.get("request_timeout") or 20),
            verbose=bool(settings.get("verbose_log")),
        )

    # ── 必须实现 ────────────────────────────────────────────────────────

    @classmethod
    @abstractmethod
    def match(cls, text: str) -> MediaRef | None:
        """从一段文本里认出属于本平台的媒体引用；认不出返回 None。

        **必须是纯函数**（不联网、不抛异常）：这样链接识别能被离线单测覆盖，
        也能让 registry 安全地依次询问所有平台。
        """

    @abstractmethod
    async def parse(self, ref: MediaRef, *, hint: ParseHint | None = None) -> MediaInfo:
        """把引用解析成 ``MediaInfo``（元数据 + extras）。

        约定：
        - 只拿元信息，**不下载媒体**；
        - 失败时抛 ``PlatformError``（见 ``core.platforms.errors``），由编排层统一兜住；
        - 平台专属的补充数据（B 站热评/标签/字幕）在 ``hint`` 允许时一并放进 ``extras``。
        """

    # ── 可选覆写（默认实现覆盖大多数平台） ───────────────────────────────

    async def resolve_media(
        self,
        info: MediaInfo,
        *,
        on_attempt: Callable[[str, bool, str], None] | None = None,
    ) -> list[MediaItem]:
        """取可下载的媒体直链。

        默认认为 ``parse()`` 已经把直链塞进 ``info.items``（抖音、小红书这类
        "解析即拿到 CDN 直链"的平台属于此类，不必覆写）。

        B 站需要额外一步（先用 ebilibili 换直链），所以覆写了这个方法。
        ``on_attempt`` 是给面板统计"哪条路径可用"的回调，用不上的平台忽略即可。
        """
        return list(info.items)

    def web_url(self, info: MediaInfo) -> str:
        """网页地址：下载失败时兜底发给用户。"""
        return info.web_url

    def summary_text(self, info: MediaInfo) -> str:
        """给 AI 总结的"这是什么东西"描述。

        默认用标题 + 正文 + 作者；有更结构化信号（分区、标签、热评）的平台覆写它。
        """
        parts = [info.title, info.desc]
        return "\n".join(part for part in parts if part).strip()

    def can(self, capability: str) -> bool:
        return capability in self.capabilities

    def apply_credentials(self, data: dict[str, str]) -> None:
        """把面板里保存的凭据注入实例。

        默认什么都不做——不需要登录态的平台不用管这个钩子。
        约定：``data`` 是已经过滤掉空值的字典，键名见 ``core/credentials.py``。
        """
        return None

    # ── 供面板与日志使用的元信息 ─────────────────────────────────────────

    @classmethod
    def describe(cls) -> dict[str, Any]:
        return {
            "name": cls.name,
            "display": cls.display or cls.name,
            "capabilities": sorted(cls.capabilities),
        }


__all__ = ["BasePlatform", "ParseHint"]
