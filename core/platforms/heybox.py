"""小黑盒平台。

实测结论（2026-11-17，样本 `https://www.xiaoheihe.cn/app/bbs/link/175148302`）：

- 分享链接有两种写法：``/app/bbs/link/<id>`` 与 App 的
  ``api.xiaoheihe.cn/bbs/app/link/tree?link_id=<id>``；
- 网页是纯前端渲染，直接 GET 只有 3KB 空壳、没有任何 og 数据（换微信 UA 也一样），
  所以**必须走接口** ``api.xiaoheihe.cn/bbs/app/link/tree``；
- 该接口要求 hkey 签名。算法随前端变动，这里按 2026-11 的版本实现，
  失效时的表现是响应里 ``status=show_captcha`` 或「非法请求」；
- 无 Cookie 的机房 IP 大概率拿到 ``show_captcha``（人机验证），此时如实报错，
  让用户在面板填一份浏览器 Cookie。这是平台风控，不是代码问题。

签名算法移植自 ``heybox-video``（GitHub: PaidaxingTuT）与 npm ``heybox-url``，
两套里只有前者与服务端当前版本对得上；``heybox-url`` 的 web 算法已失效
（实测返回「非法请求」）。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from astrbot.api import logger

from ..media import (
    CAP_IMAGES,
    CAP_SUMMARY,
    CAP_VIDEO,
    KIND_IMAGE,
    KIND_VIDEO,
    MediaInfo,
    MediaItem,
    MediaRef,
)
from .base import BasePlatform, ParseHint
from .errors import MediaNotFound, MediaProtected, PlatformError

LOG_TAG = "[bilibili-download]"

API_BASE = "https://api.xiaoheihe.cn"
LINK_PATH = "/bbs/app/link/tree"
WEB_TEMPLATE = "https://www.xiaoheihe.cn/app/bbs/link/{link_id}"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

#: 内容页 / 接口两种链接形态，统一抽出 link_id
_LINK_RE = re.compile(
    r"https?://(?:www\.)?xiaoheihe\.cn/(?:app/)?bbs/link/([0-9]+)[^\s]*",
    re.IGNORECASE,
)
_API_RE = re.compile(
    r"https?://api\.xiaoheihe\.cn/(?:v3/)?bbs/app/link/tree\?[^\s]*?link_id=([0-9]+)",
    re.IGNORECASE,
)

TRIGGER_PATTERN = (
    r"https?://(?:www\.)?xiaoheihe\.cn/(?:app/)?bbs/link/[0-9]+[^\s]*"
    r"|https?://api\.xiaoheihe\.cn/(?:v3/)?bbs/app/link/tree\?[^\s]*?link_id=[0-9]+"
)

# ── hkey 签名（2026-11 版本） ──────────────────────────────────────────

_ALPHABET = "JKMNPQRTX1234OABCDFG56789H"


def _convert(value: int) -> int:
    """GF(2^8) 上乘 2（多项式 0x1B）。"""
    return ((value << 1) ^ 0x1B) & 0xFF if value & 0x80 else value << 1


def _c3(value: int) -> int:
    return _convert(value) ^ value


def _c2(value: int) -> int:
    return _c3(_convert(value))


def _c1(value: int) -> int:
    return _c2(_c3(_convert(value)))


def _c0(value: int) -> int:
    return _c1(value) ^ _c2(value) ^ _c3(value)


def _checksum(data: list[int]) -> int:
    """4 字节校验和，结果 0~99。"""
    values = [
        _c0(data[0]) ^ _c1(data[1]) ^ _c2(data[2]) ^ _c3(data[3]),
        _c3(data[0]) ^ _c0(data[1]) ^ _c1(data[2]) ^ _c2(data[3]),
        _c2(data[0]) ^ _c3(data[1]) ^ _c0(data[2]) ^ _c1(data[3]),
        _c1(data[0]) ^ _c2(data[1]) ^ _c3(data[2]) ^ _c0(data[3]),
    ]
    return sum(values) % 100


def _digits_only(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def hkey(path: str, timestamp: int, nonce: str) -> str:
    """计算 mobile 通道的 hkey（5 位前缀 + 2 位校验和）。

    注意与 web 通道**不是同一套**：字母表、校验和都不同。web 那套在机房 IP 上
    一律被判 `show_captcha`，mobile 这套实测能拿到数据（2026-11-18）。
    """
    normalized = "/" + "/".join(part for part in path.split("/") if part) + "/"
    stamp = int(timestamp) + 1
    nonce_hash = (
        hashlib.md5(_digits_only(nonce + _ALPHABET).encode()).hexdigest().lower()
    )
    digits = _digits_only(hashlib.md5(f"{stamp}{normalized}{nonce_hash}".encode()).hexdigest())[:9]
    digits = digits.ljust(9, "0")

    key = ""
    current = int(digits)
    for _ in range(5):
        key += _ALPHABET[current % len(_ALPHABET)]
        current //= len(_ALPHABET)

    return key + str(_checksum([ord(ch) for ch in key[-4:]])).zfill(2)


# ── 响应字段提取（结构不稳定，全部按候选键降级） ──────────────────────


def _pick_str(node: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _pick_dict(node: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    for key in keys:
        value = node.get(key)
        if isinstance(value, dict) and value:
            return value
    return {}


def _collect_urls(node: Any) -> list[str]:
    """把 str / list / dict 三种形态统一成 URL 列表。"""
    urls: list[str] = []
    if isinstance(node, str):
        if node.startswith("http"):
            urls.append(node)
    elif isinstance(node, list):
        for item in node:
            urls.extend(_collect_urls(item))
    elif isinstance(node, dict):
        for key in ("url", "src", "link", "video_url", "origin_url"):
            value = node.get(key)
            if isinstance(value, str) and value.startswith("http"):
                urls.append(value)
        for key in ("images", "list", "items"):
            value = node.get(key)
            if isinstance(value, (list, dict)):
                urls.extend(_collect_urls(value))
    seen: set[str] = set()
    return [u for u in urls if not (u in seen or seen.add(u))]


def _collect_block_urls(raw: Any) -> list[str]:
    """从 item_list / content 里挖图片地址。

    小黑盒的正文是块结构：``[{"type": "img", "url": "..."}, ...]``，
    也可能是 JSON 字符串或嵌套 list。视频块单独忽略（视频走 ``video_url``）。
    """
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            raw = json.loads(text)
        except ValueError:
            return []
    if not isinstance(raw, list):
        return []

    urls: list[str] = []
    stack: list[Any] = list(raw)
    while stack:
        block = stack.pop(0)
        if isinstance(block, list):
            stack.extend(block)
            continue
        if not isinstance(block, dict):
            continue
        block_type = str(block.get("type") or "").strip().lower()
        if block_type in {"img", "image", "photo"}:
            url = block.get("url") or block.get("src") or block.get("image")
            if isinstance(url, str) and url.startswith("http"):
                urls.append(url)
        for key in ("children", "blocks", "items", "list"):
            if isinstance(block.get(key), list):
                stack.extend(block[key])

    seen: set[str] = set()
    return [u for u in urls if not (u in seen or seen.add(u))]


def _to_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class HeyboxPlatform(BasePlatform):
    name = "heybox"
    display = "小黑盒"
    capabilities = frozenset({CAP_VIDEO, CAP_IMAGES, CAP_SUMMARY})
    trigger_pattern = TRIGGER_PATTERN

    def __init__(self, session, *, timeout: int = 20, verbose: bool = False) -> None:
        super().__init__(session, timeout=timeout, verbose=verbose)
        self.cookie = ""

    def apply_credentials(self, data: dict[str, str]) -> None:
        self.cookie = str((data or {}).get("cookie") or "").strip()

    # ── 匹配 ────────────────────────────────────────────────────────────

    @classmethod
    def match(cls, text: str) -> MediaRef | None:
        if not text:
            return None
        matched = _LINK_RE.search(text) or _API_RE.search(text)
        if not matched:
            return None
        return MediaRef(
            platform=cls.name,
            raw=matched.group(0),
            media_id=matched.group(1),
        )

    # ── 解析 ────────────────────────────────────────────────────────────

    async def parse(self, ref: MediaRef, *, hint: ParseHint | None = None) -> MediaInfo:
        link_id = str(ref.media_id or "").strip()
        if not link_id:
            raise MediaNotFound("没认出小黑盒帖子编号")

        data = await self._fetch(link_id)
        result = data.get("result") or {}
        link = _pick_dict(result, ("link", "post", "link_info", "topic"))
        if not link:
            raise MediaNotFound("没拿到帖子内容（可能已删除，或需要登录）")

        title = _pick_str(link, ("title", "share_title", "link_title"))
        # text 是完整正文，description 是截断版；两者都没有时才会是纯链接帖
        body = _pick_str(link, ("text", "description", "desc"))
        user = link.get("user") or {}
        topics = [
            str(item.get("name"))
            for item in (link.get("topics") or [])
            if isinstance(item, dict) and item.get("name")
        ]

        # 图片在 item_list/content 的块结构里（type=img → url），不是 images 字段
        images = _collect_block_urls(link.get("item_list")) or _collect_block_urls(
            link.get("content")
        )
        video_url = _pick_str(link, ("video_url",))
        video_cover = _pick_str(link, ("video_cover", "cover", "thumb", "video_thumb"))

        items: list[MediaItem] = []
        if video_url:
            items.append(
                MediaItem(kind=KIND_VIDEO, url=video_url, referer="https://www.xiaoheihe.cn/")
            )
        for url in images[:9]:
            items.append(
                MediaItem(kind=KIND_IMAGE, url=url, referer="https://www.xiaoheihe.cn/")
            )

        if not items:
            raise MediaNotFound("这条帖子没有可下载的图片或视频（纯文字帖）")

        return MediaInfo(
            platform=self.name,
            media_id=link_id,
            source_url=ref.raw,
            web_url=WEB_TEMPLATE.format(link_id=link_id),
            title=title or body[:40] or "小黑盒帖子",
            author=_pick_str(user, ("username", "nickname", "name")),
            author_id=str(user.get("userid") or user.get("user_id") or ""),
            author_avatar=_pick_str(user, ("avatar", "avartar", "avatar_url")),
            cover=video_cover or (images[0] if images else ""),
            pubdate=self._pubdate(link),
            desc=body,
            items=items,
            extras={"images": images, "video_url": video_url, "topics": topics},
            stats={
                key: _to_int(link.get(src))
                for key, src in (
                    ("like", "up"),
                    ("reply", "comment_num"),
                    ("favorite", "favour_count"),
                )
                if _to_int(link.get(src))
            },
        )

    @staticmethod
    def _pubdate(link: dict[str, Any]) -> int:
        raw = _to_int(link.get("create_at") or link.get("time") or link.get("publish_time"))
        if raw > 10**11:  # 毫秒
            raw //= 1000
        return raw

    async def _fetch(self, link_id: str) -> dict[str, Any]:
        ts = int(time.time())
        nonce = hashlib.md5(f"{ts}{time.time_ns()}".encode()).hexdigest().upper()
        params = {
            "os_type": "web",
            "app": "heybox",
            "client_type": "mobile",
            "version": "999.0.3",
            "x_client_type": "web",
            "x_os_type": "Mac",
            "x_app": "heybox",
            "heybox_id": "-1",
            "link_id": link_id,
            "owner_only": "1",
            "hkey": hkey(LINK_PATH, ts, nonce),
            "_time": str(ts),
            "nonce": nonce,
        }
        headers = {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Referer": WEB_TEMPLATE.format(link_id=link_id),
            "Origin": "https://www.xiaoheihe.cn",
            "X-Requested-With": "XMLHttpRequest",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie

        try:
            async with self.session.get(
                API_BASE + LINK_PATH,
                params=params,
                headers=headers,
                timeout=self.timeout,
            ) as resp:
                data = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001 - 网络/JSON 都收敛成平台错误
            raise PlatformError(f"小黑盒接口请求失败：{type(exc).__name__}: {exc}") from exc

        status = str(data.get("status") or "")
        if status == "show_captcha":
            logger.info(f"{LOG_TAG} 小黑盒触发人机验证（链接 {link_id}）")
            raise MediaProtected(
                "小黑盒这次返回了人机验证，稍后重试；连续失败可在面板填一份浏览器 Cookie"
            )
        if status != "ok":
            raise PlatformError(f"小黑盒接口返回异常：{data.get('msg') or status or '未知'}")
        return data
