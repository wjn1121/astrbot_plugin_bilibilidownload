"""微博平台。

实测（2026-11-19）：

- 数据走 ``m.weibo.cn/statuses/show?id=<wid>``，返回 JSON；
- **必须带访客 Cookie**，完全匿名会被 403/418 拦——访客身份从
  ``visitor.passport.weibo.cn/visitor/genvisitor2`` POST 一次拿到（带 ``cb=visitor_gray_callback``），
  进程内缓存，过期后清掉重取；
- 链接形态很多：``m.weibo.cn/status/<id>``、``m.weibo.cn/statuses/show?id=<id>``、
  ``weibo.com/<uid>/<base62>``、``video.weibo.com/show?fid=<fid>``、``weibo.com/tv/show/...``；
  后三种要先把 base62 的 mid 还原成数字 id；
- 正文 ``text`` 是 HTML，图片在 ``pics[].large.url``，视频在 ``page_info.media_info``，
  长文（``isLongText``）只给截断版，这里不额外拉全文。

访客身份与 base62 还原的做法参考 ``astrbot_plugin_denia_share``（上游 iris1598/astrbot_plugin_rika_share）。
"""

from __future__ import annotations

import asyncio
import re
import time
from email.utils import parsedate_to_datetime
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

UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

TRIGGER_PATTERN = (
    r"https?://m\.weibo\.cn/(?:status|detail)/[0-9A-Za-z]+"
    r"|https?://m\.weibo\.cn/status(?:es)?/show\?id=[0-9A-Za-z]+"
    r"|https?://(?:www\.)?weibo\.com/\d+/[0-9A-Za-z]+"
    r"|https?://weibo\.com/tv/show/\d{4}:\d+\?mid=\d+"
    r"|https?://video\.weibo\.com/show\?fid=\d+:\d+"
)

_PATTERNS = (
    re.compile(r"m\.weibo\.cn/(?:status|detail)/([0-9A-Za-z]+)", re.IGNORECASE),
    re.compile(r"m\.weibo\.cn/status(?:es)?/show\?id=([0-9A-Za-z]+)", re.IGNORECASE),
    re.compile(r"weibo\.com/tv/show/\d{4}:\d+\?mid=(\d+)", re.IGNORECASE),
    re.compile(r"video\.weibo\.com/show\?fid=\d+:(\d+)", re.IGNORECASE),
    re.compile(r"weibo\.com/\d+/([0-9A-Za-z]+)", re.IGNORECASE),
)

#: 微博的 mid → 数字 id（base62）
_B62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"

#: 访客 cookie 缓存（进程内共享）
_visitor: dict[str, str] | None = None
_visitor_lock = asyncio.Lock()

_TAG_RE = re.compile(r"<[^>]+>")


def mid2id(mid: str) -> str:
    """base62 的 mid 还原成数字 id；本来就是纯数字时原样返回。"""
    if not mid or mid.isdigit():
        return mid or ""
    number = 0
    for ch in mid:
        index = _B62.find(ch)
        if index < 0:
            return mid
        number = number * 62 + index
    return str(number)


def _clean_html(text: str) -> str:
    return _TAG_RE.sub("", text or "").replace("&nbsp;", " ").strip()


def _pubdate(value: str) -> int:
    try:
        return int(parsedate_to_datetime(value).timestamp())
    except Exception:  # noqa: BLE001 - 格式变了就当未知
        return 0


async def _get_visitor(session, timeout: int) -> dict[str, str] | None:
    """取（并缓存）微博访客 Cookie；取不到返回 None（退化成匿名请求）。"""
    global _visitor
    if _visitor:
        return _visitor
    async with _visitor_lock:
        if _visitor:
            return _visitor
        try:
            async with session.post(
                "https://visitor.passport.weibo.cn/visitor/genvisitor2",
                data="cb=visitor_gray_callback",
                headers={
                    "User-Agent": UA,
                    "content-type": "application/x-www-form-urlencoded",
                    "referer": "https://visitor.passport.weibo.cn/",
                },
                timeout=timeout,
            ) as resp:
                if resp.status != 200:
                    logger.warning(f"{LOG_TAG} 微博访客身份获取失败（HTTP {resp.status}）")
                    return None
                cookies = {key: morsel.value for key, morsel in resp.cookies.items()}
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{LOG_TAG} 微博访客身份获取失败：{type(exc).__name__}: {exc}")
            return None

        if not cookies:
            logger.warning(f"{LOG_TAG} 微博访客响应里没有 Cookie，改用匿名请求")
            return None
        _visitor = cookies
        return _visitor


def _drop_visitor() -> None:
    global _visitor
    _visitor = None


class WeiboPlatform(BasePlatform):
    name = "weibo"
    display = "微博"
    capabilities = frozenset({CAP_VIDEO, CAP_IMAGES, CAP_SUMMARY})
    trigger_pattern = TRIGGER_PATTERN

    @classmethod
    def match(cls, text: str) -> MediaRef | None:
        if not text:
            return None
        for pattern in _PATTERNS:
            matched = pattern.search(text)
            if matched:
                return MediaRef(
                    platform=cls.name,
                    raw=matched.group(0),
                    media_id=mid2id(matched.group(1)),
                )
        return None

    async def parse(self, ref: MediaRef, *, hint: ParseHint | None = None) -> MediaInfo:
        wid = str(ref.media_id or "").strip()
        if not wid.isdigit():
            raise MediaNotFound("没能从链接里识别出微博 id")

        data = await self._fetch(wid)
        user = data.get("user") or {}
        text = _clean_html(str(data.get("text") or ""))

        pics = data.get("pics") or []
        images = [
            str((pic.get("large") or {}).get("url") or pic.get("url") or "")
            for pic in pics
            if isinstance(pic, dict)
        ]
        images = [url for url in images if url.startswith("http")]

        video_url = self._video_url(data)
        cover = video_url and self._video_cover(data) or (images[0] if images else "")

        items: list[MediaItem] = []
        if video_url:
            items.append(
                MediaItem(
                    kind=KIND_VIDEO,
                    url=video_url,
                    referer="https://weibo.com/",
                    headers={"User-Agent": UA},
                )
            )
        for url in images[:9]:
            items.append(
                MediaItem(kind=KIND_IMAGE, url=url, referer="https://weibo.com/")
            )

        if not items:
            raise MediaNotFound("这条微博没有可下载的图片或视频（纯文字微博）")

        stats = {
            key: int(data.get(src) or 0)
            for key, src in (
                ("like", "attitudes_count"),
                ("reply", "comments_count"),
                ("share", "reposts_count"),
            )
        }

        return MediaInfo(
            platform=self.name,
            media_id=wid,
            source_url=ref.raw,
            web_url=f"https://m.weibo.cn/detail/{wid}",
            title=text[:60] or "微博",
            author=str(user.get("screen_name") or ""),
            author_id=str(user.get("id") or ""),
            author_avatar=str(user.get("avatar_hd") or user.get("profile_image_url") or ""),
            cover=cover,
            pubdate=_pubdate(str(data.get("created_at") or "")),
            desc=text,
            items=items,
            extras={"images": images, "video_url": video_url},
            stats=stats,
        )

    @staticmethod
    def _video_url(data: dict[str, Any]) -> str:
        """从 page_info 里挑一条视频直链（优先高清）。"""
        info = (data.get("page_info") or {}).get("media_info") or {}
        for key in ("stream_url_hd", "stream_url"):
            value = info.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value

        urls = (data.get("page_info") or {}).get("urls") or {}
        for key in ("mp4_720p_mp4", "mp4_hd_mp4", "mp4_ld_mp4", "mp4_1080p"):
            value = urls.get(key) if isinstance(urls, dict) else None
            if isinstance(value, str) and value.startswith("http"):
                return value
        return ""

    @staticmethod
    def _video_cover(data: dict[str, Any]) -> str:
        info = (data.get("page_info") or {}).get("page_pic") or {}
        url = info.get("url") or info.get("large", {}).get("url")
        return str(url) if isinstance(url, str) else ""

    async def _fetch(self, wid: str) -> dict[str, Any]:
        headers = {
            "User-Agent": UA,
            "accept": "application/json, text/plain, */*",
            "referer": f"https://m.weibo.cn/detail/{wid}",
            "origin": "https://m.weibo.cn",
            "x-requested-with": "XMLHttpRequest",
            "mweibo-pwa": "1",
        }
        url = f"https://m.weibo.cn/statuses/show?id={wid}&_={int(time.time() * 1000)}"

        for attempt in range(2):
            cookies = await _get_visitor(self.session, self.timeout)
            try:
                async with self.session.get(
                    url, headers=headers, cookies=cookies, timeout=self.timeout
                ) as resp:
                    if resp.status in (403, 418):
                        _drop_visitor()
                        if attempt == 0:
                            continue
                        raise MediaProtected("微博把这次请求当成异常流量拦下了，稍后再试")
                    if resp.status != 200:
                        raise PlatformError(f"微博接口返回 HTTP {resp.status}")
                    data = await resp.json(content_type=None)
            except (MediaProtected, PlatformError):
                raise
            except Exception as exc:  # noqa: BLE001
                raise PlatformError(f"微博接口请求失败：{type(exc).__name__}: {exc}") from exc

            if not isinstance(data, dict) or data.get("ok") != 1:
                message = (data or {}).get("msg") if isinstance(data, dict) else ""
                raise MediaNotFound(f"没拿到微博内容（{message or '可能已删除或仅粉丝可见'}）")
            # statuses/show 的外层是 {ok, data}，真正的微博对象在 data.data 里
            inner = data.get("data")
            return inner if isinstance(inner, dict) else data

        raise MediaProtected("微博要求验证，稍后再试")
