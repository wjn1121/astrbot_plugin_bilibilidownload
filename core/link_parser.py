"""B 站视频链接的识别与归一化。

这里只做纯文本处理，**不联网**：b23.tv 短链的真实地址需要一次 HTTP 跳转，
因此本模块只把它标记出来，交由 ``BiliApi.resolve_short_url()`` 异步解析。
这样拆分的好处是链接识别可以被单元测试覆盖，不需要 mock 网络。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

# BV 号固定 12 位（BV + 10 位 base58 字符，大小写敏感）；av 号 1~12 位数字。
_BV_PATTERN = r"BV[0-9A-Za-z]{10}"
_AV_PATTERN = r"av\d{1,12}"

# B 站分享短链域名：b23.tv 是主域，其余几个是镜像短链（清单参考
# astrbot_plugin_biliVideo 的 SHORT_URL_DOMAINS，它按实践维护）。
_SHORT_URL_HOSTS = ("b23.tv", "bili2233.cn", "bili22.cn", "bili23.cn", "bili33.cn")
_SHORT_HOST_PATTERN = "|".join(host.replace(".", r"\.") for host in _SHORT_URL_HOSTS)

# 触发用正则：完整视频链接、短链、裸 BV/av 号三种形态都要能命中。
# 裸 ID 前后加边界断言，避免把 "xxBV1xx411c7mDyy" 这类更长的串误判成 BV 号。
TRIGGER_REGEX = (
    rf"https?://(?:www\.|m\.)?bilibili\.com/video/(?:{_BV_PATTERN}|{_AV_PATTERN})[^\s]*"
    rf"|https?://(?:{_SHORT_HOST_PATTERN})/[0-9A-Za-z]+"
    rf"|(?<![0-9A-Za-z])(?:{_BV_PATTERN}|{_AV_PATTERN})(?![0-9A-Za-z])"
)

_FIRST_REF_RE = re.compile(TRIGGER_REGEX, re.IGNORECASE)
_BV_RE = re.compile(_BV_PATTERN, re.IGNORECASE)
_AV_RE = re.compile(_AV_PATTERN, re.IGNORECASE)
_PAGE_RE = re.compile(r"[?&]p=(\d+)")
_SHORT_RE = re.compile(rf"https?://(?:{_SHORT_HOST_PATTERN})/[0-9A-Za-z]+", re.IGNORECASE)

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


# ── QQ 小程序 / JSON 卡片 ────────────────────────────────────────────
#
# QQ 把 B 站视频当「小程序卡片」分享时，链接不在文本里，而是埋在 json 消息段的
# 一个 JSON 结构里（AstrBot 的 message_str 只拼接 Plain 段，所以拿不到）。
# 下面这套提取顺序参考了两个同类插件：
#   - astrbot_plugin_parser 的 `core.utils.extract_json_url`：按 meta.<来源>.<字段>
#     的优先级取，再递归兜底，并做 URL 解码；
#   - astrbot_plugin_biliVideo 的 `parsing.url_extractor`：递归找 qqdocurl/jumpUrl，
#     以及从 `[CQ:json,data=...]` 包装里取。
# 两者共同的教训是：**别对整段 JSON 直接跑正则**——卡片里常混着推荐位等无关链接，
# 全量扫描会把第一个碰到的无关 URL 当成目标。

# 卡片里表达「真实跳转目标」的字段，按优先级排列。
# 不同来源结构不同：QQ 小程序在 meta.miniapp.*，分享卡片在 meta.detail_1.*，
# 图文 / 音乐卡片在 meta.news.* / meta.music.*。
_CARD_URL_PATHS = (
    ("miniapp", "legacyUrl"),
    ("miniapp", "pcJumpUrl"),
    ("miniapp", "jumpUrl"),
    ("miniapp", "sourceUrl"),
    ("miniapp", "url"),
    ("detail_1", "qqdocurl"),
    ("detail_1", "jumpUrl"),
    ("detail_1", "url"),
    ("detail_1", "sourceUrl"),
    ("detail_1", "shareUrl"),
    ("detail_1", "targetUrl"),
    ("news", "jumpUrl"),
    ("news", "url"),
    ("news", "sourceUrl"),
    ("news", "shareUrl"),
    ("music", "musicUrl"),
    ("music", "jumpUrl"),
    ("music", "url"),
)

# 卡片字符串里的 URL 有三种变形，必须都还原后再匹配：
# URL 编码（miniapp.legacyUrl 常见）、转义斜杠（\/）、HTML 实体（&amp;）。
_CARD_ENTITIES = (("&amp;", "&"), ("&#44;", ","), ("&#91;", "["), ("&#93;", "]"))

# 反斜杠与引号要排除：卡片里 URL 常被 " 或 \/ 包住，最后一个字符不能吃进来
_CARD_URL_RE = re.compile(r"https?://[^\s\"'<>\\]+", re.IGNORECASE)

# 聊天与卡片里粘在 URL 尾部的标点
_URL_TRAILING = "\"'`}>]),，。、）！？；：;:!?"

# 卡片链接的域名判定（bilibili.com 与上面那组短链域名）
_BILI_HOSTS = ("bilibili.com", *_SHORT_URL_HOSTS)


def normalize_card_text(value: str) -> str:
    """还原卡片字符串里的 URL 编码、转义斜杠与 HTML 实体。"""
    if not value:
        return ""
    text = value
    for entity, char in _CARD_ENTITIES:
        if entity in text:
            text = text.replace(entity, char)
    if "\\/" in text:
        text = text.replace("\\/", "/")
    if "%" in text:
        # 对不含合法 %XX 的文本是无损的，可以无条件跑
        text = unquote(text)
    return text


def _clean_card_url(url: str) -> str:
    """去掉卡片里粘在 URL 两端的尖括号与尾部标点。"""
    return (url or "").strip().strip("<>").rstrip(_URL_TRAILING)


def _is_bili_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return any(host == d or host.endswith("." + d) for d in _BILI_HOSTS)


def _iter_json_strings(value: object):
    """递归产出 JSON 结构里的所有字符串值。"""
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_json_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_json_strings(item)
    elif isinstance(value, str):
        yield value


def _load_card_json(text: str) -> object | None:
    """尽力把卡片文本解析成 JSON 对象；解析不了返回 None。"""
    if not text:
        return None
    stripped = text.strip()

    # OneBot 的 [CQ:json,data=...] 包装
    if "[CQ:json" in stripped:
        match = re.search(r"\[CQ:json,data=(.*?)\]", stripped, re.DOTALL)
        if match:
            stripped = match.group(1).strip()

    if not stripped.startswith("{") and not stripped.startswith("["):
        return None
    try:
        return json.loads(stripped)
    except (TypeError, ValueError):
        return None


def url_from_card(text: str) -> str | None:
    """从 QQ 小程序 / JSON 卡片文本里取出最可能的 B 站链接。

    三级顺序，前一级拿不到才退到下一级：

    1. 解析成 JSON，按 ``meta.<来源>.<字段>`` 的优先级取（最准）；
    2. 递归扫描所有字符串值里的 URL，优先返回 B 站域名（卡片结构变了也能兜住）；
    3. 连 JSON 都解析不了（被截断、只拿到片段）时，直接对原文跑正则。
    """
    if not text:
        return None

    payload = _load_card_json(text)
    if isinstance(payload, dict):
        meta = payload.get("meta")
        if isinstance(meta, dict):
            for source, key in _CARD_URL_PATHS:
                node = meta.get(source)
                if not isinstance(node, dict):
                    continue
                value = node.get(key)
                if not isinstance(value, str):
                    continue
                match = _CARD_URL_RE.search(normalize_card_text(value))
                if match:
                    return _clean_card_url(match.group(0))

        found: list[str] = []
        for value in _iter_json_strings(payload):
            found.extend(
                _clean_card_url(m.group(0))
                for m in _CARD_URL_RE.finditer(normalize_card_text(value))
            )
        if found:
            for url in found:
                if _is_bili_url(url):
                    return url
            return found[0]

    match = _CARD_URL_RE.search(normalize_card_text(text))
    return _clean_card_url(match.group(0)) if match else None
