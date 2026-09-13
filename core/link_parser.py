"""B 站视频链接的识别与归一化。

这里只做纯文本处理，**不联网**：b23.tv 短链的真实地址需要一次 HTTP 跳转，
因此本模块只把它标记出来，交由 ``BiliApi.resolve_short_url()`` 异步解析。
这样拆分的好处是链接识别可以被单元测试覆盖，不需要 mock 网络。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# BV 号固定 12 位（BV + 10 位 base58 字符，大小写敏感）；av 号 1~12 位数字。
_BV_PATTERN = r"BV[0-9A-Za-z]{10}"
_AV_PATTERN = r"av\d{1,12}"

# 触发用正则：完整视频链接、b23.tv 短链、裸 BV/av 号三种形态都要能命中。
# 裸 ID 前后加边界断言，避免把 "xxBV1xx411c7mDyy" 这类更长的串误判成 BV 号。
TRIGGER_REGEX = (
    rf"https?://(?:www\.|m\.)?bilibili\.com/video/(?:{_BV_PATTERN}|{_AV_PATTERN})[^\s]*"
    rf"|https?://b23\.tv/[0-9A-Za-z]+"
    rf"|(?<![0-9A-Za-z])(?:{_BV_PATTERN}|{_AV_PATTERN})(?![0-9A-Za-z])"
)

_FIRST_REF_RE = re.compile(TRIGGER_REGEX, re.IGNORECASE)
_BV_RE = re.compile(_BV_PATTERN, re.IGNORECASE)
_AV_RE = re.compile(_AV_PATTERN, re.IGNORECASE)
_PAGE_RE = re.compile(r"[?&]p=(\d+)")
_SHORT_RE = re.compile(r"https?://b23\.tv/[0-9A-Za-z]+", re.IGNORECASE)

# 回复「下载」这类自然语言触发词（整条消息就是一个下载指令时才生效）
DOWNLOAD_WORD_REGEX = r"^[\s\u200b]*[!！/／]?(?:下载|下载视频|获取视频|要视频|发视频)[\s\u200b]*$"


@dataclass(slots=True)
class BiliRef:
    """消息中发现的一个 B 站视频引用。"""

    raw: str  # 命中的原始文本，用于日志与给用户回显
    bvid: str | None  # 已归一化的 BV 号（前缀统一大写）
    aid: int | None  # av 号（数字部分）
    page: int  # 1-based 分P序号
    short_url: str | None  # 命中 b23.tv 短链时保存原始短链

    @property
    def is_short_link(self) -> bool:
        return self.short_url is not None

    @property
    def is_bare_id(self) -> bool:
        """是否只是裸 BV/av 号（没有完整链接）。"""
        return not self.raw.lower().startswith("http")


def find_ref(text: str) -> BiliRef | None:
    """从一段消息文本中提取第一个 B 站视频引用。

    只取第一个：一条消息里塞多个链接时，逐个处理会让机器人刷屏，
    也会成倍放大带宽与风控成本。
    """
    if not text:
        return None

    match = _FIRST_REF_RE.search(text)
    if not match:
        return None

    raw = match.group(0)

    if _SHORT_RE.fullmatch(raw):
        # 短链里看不出 BV 号，留给 BiliApi 跳转解析
        return BiliRef(raw=raw, bvid=None, aid=None, page=1, short_url=raw)

    page = 1
    page_match = _PAGE_RE.search(raw)
    if page_match:
        page = max(1, int(page_match.group(1)))

    bv_match = _BV_RE.search(raw)
    if bv_match:
        # 用户常把小写 bv 前缀打出来，B 站接口只认大写前缀（后 10 位仍大小写敏感）
        bvid = "BV" + bv_match.group(0)[2:]
        return BiliRef(raw=raw, bvid=bvid, aid=None, page=page, short_url=None)

    av_match = _AV_RE.search(raw)
    if av_match:
        return BiliRef(
            raw=raw, bvid=None, aid=int(av_match.group(0)[2:]), page=page, short_url=None
        )

    return None


def bvid_from_url(url: str) -> str | None:
    """从（已跳转完成的）URL 中提取 BV 号，用于短链解析结果。"""
    if not url:
        return None
    match = _BV_RE.search(url)
    return "BV" + match.group(0)[2:] if match else None


def aid_from_url(url: str) -> int | None:
    """从 URL 中提取 av 号数字部分。"""
    if not url:
        return None
    match = _AV_RE.search(url)
    return int(match.group(0)[2:]) if match else None
