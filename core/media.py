"""跨平台媒体模型。

设计原则：这里只定义**所有平台都长得一样**的字段；平台独有的信息（B 站热评与分P、
抖音图集、小红书笔记正文、字幕依据……）一律进 ``extras``，由渲染层与总结层按平台取用。

这样做的直接好处是**新增平台不需要改这个文件**：加一个 parser，把独有字段塞进
``extras``，编排层与卡片层一行都不用动。反过来，如果把 ``VideoInfo`` 那套字段直接
当成通用模型，第二个平台进来时就会被迫往模型里加一堆只有 B 站有意义的字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 媒体条目类型。video 与 audio 走"下载后发送/发直链"，image 走图集发送。
KIND_VIDEO = "video"
KIND_IMAGE = "image"
KIND_AUDIO = "audio"

# 能力位：平台用它们声明"我能做什么"，编排层据此决定走哪条链路，
# 而不是到处写 ``if platform == "bilibili"``。
CAP_VIDEO = "video"        # 能取到视频直链
CAP_IMAGES = "images"      # 有图集（或图文笔记）
CAP_AUDIO = "audio"        # 有音频（音乐类平台）
CAP_SUMMARY = "summary"    # 有足够的文本信号可交给 AI 总结
CAP_SUBTITLE = "subtitle"  # 能取到字幕（B 站配 sessdata 后）
CAP_REPLIES = "replies"    # 有热评（B 站独有，卡片会单独成图）


@dataclass(slots=True)
class MediaRef:
    """一条消息里识别出的媒体引用。

    ``match()`` 的产出物，只描述"指向哪个平台的哪个内容"，**不含任何请求结果**。
    这样链接识别可以被单测覆盖，也不需要 mock 网络。
    """

    platform: str
    raw: str                                  # 命中的原始文本，用于日志与回显
    media_id: str | None = None               # bvid / aweme_id / note_id …
    page: int = 1                             # 1-based 分P（无分P概念的平台恒为 1）
    is_short: bool = False                    # 是否短链，需要额外跳转才能拿到 ID
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_bare_id(self) -> bool:
        """是否只是裸 ID（没有完整链接）。"""
        return not self.raw.lower().startswith("http")

    @property
    def dedup_key(self) -> str:
        """解析前的去重 key。

        短链此时还不知道真实 ID，只能用原串占位；``parse()`` 拿到 ID 后，
        编排层会再用 ``MediaInfo.dedup_key`` 补登记一次，这样第二次有人发长链
        时能被正确判重（与 v0.2.x 的行为一致）。
        """
        ident = self.media_id or self.raw
        return f"{self.platform}:{ident}#p{self.page}"


@dataclass(slots=True)
class MediaItem:
    """一条可下载的媒体（视频 / 图片 / 音频）。"""

    kind: str = KIND_VIDEO
    url: str = ""
    filename: str = ""
    referer: str = ""                                  # 站方防盗链校验用
    size_hint: int = 0                                 # 已知体积（字节），未知为 0
    headers: dict[str, str] = field(default_factory=dict)  # 平台额外的下载头

    @property
    def is_video(self) -> bool:
        return self.kind == KIND_VIDEO


@dataclass(slots=True)
class MediaInfo:
    """一个媒体内容的元数据快照（跨平台通用部分）。"""

    platform: str
    media_id: str
    page: int = 1

    source_url: str = ""      # 用户发的原始链接
    web_url: str = ""         # 网页地址（下载失败时兜底给用户）

    title: str = ""
    author: str = ""
    author_id: str = ""
    author_avatar: str = ""
    cover: str = ""
    duration: int = 0
    desc: str = ""
    pubdate: int = 0
    stats: dict[str, int] = field(default_factory=dict)

    # parse() 阶段通常为空（此时还不知道直链），由 resolve_media() 填充
    items: list[MediaItem] = field(default_factory=list)

    # 平台独有信息。约定 key（新增平台可自由扩展，渲染层不认识就忽略）：
    #   bilibili: replies / tags / pages / width / height / subtitle_basis / video_info
    #   douyin:   images / music
    #   xiaohongshu: note_text / images
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def dedup_key(self) -> str:
        """归一化的去重 key：同一内容的三种链接写法必须落到同一个值。"""
        return f"{self.platform}:{self.media_id}#p{self.page}"

    @property
    def page_suffix(self) -> str:
        """卡片标题后缀，如「（P2）」；单P 或平台无分P概念时为空串。"""
        return f"（P{self.page}）" if self.page > 1 else ""

    def extra(self, key: str, default: Any = None) -> Any:
        return self.extras.get(key, default)

    def stat(self, key: str) -> int:
        try:
            return int(self.stats.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    def video_items(self) -> list[MediaItem]:
        return [item for item in self.items if item.kind == KIND_VIDEO]

    def image_items(self) -> list[MediaItem]:
        return [item for item in self.items if item.kind == KIND_IMAGE]


__all__ = [
    "CAP_AUDIO",
    "CAP_IMAGES",
    "CAP_REPLIES",
    "CAP_SUBTITLE",
    "CAP_SUMMARY",
    "CAP_VIDEO",
    "KIND_AUDIO",
    "KIND_IMAGE",
    "KIND_VIDEO",
    "MediaInfo",
    "MediaItem",
    "MediaRef",
]
