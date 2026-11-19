"""快手平台。

实测（2026-11-19，样本 `v.kuaishou.com/xxx`）：

- 短链 302 到 `www.kuaishou.com/short-video/<id>` 或 `/fw/photo/<id>`；
  长视频入口 `/fw/long-video/` 要改写成 `/fw/photo/` 才有完整数据；
- 页面里的 ``window.INIT_STATE`` 是 JSON，作品数据在某个含 ``photo`` 键的节点下；
- ``photo`` / ``single`` / ``ext_params`` 在新版页面里可能是 JSON **字符串**，要二次 loads；
- 视频直链在 ``mainMvUrls[].url``（只认 .mp4），封面在 ``coverUrls[].url``；
- 图集在 ``single.cdnList`` + ``single.imgRouteList``（新版）或
  ``ext_params.atlas.cdnList`` + ``atlas.list``（旧版），域名要拼 ``https://{cdn}/{route}``。

不需要 Cookie；带 iOS UA + Referer 更稳（参考实现 astrbot_plugin_rika_share 的做法）。
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

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
from .errors import MediaNotFound, PlatformError

LOG_TAG = "[bilibili-download]"

UA_IOS = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

#: 短链 / 长链 / 老域名（chenzhongtech）三种形态
TRIGGER_PATTERN = (
    r"https?://v\.kuaishou\.com/[A-Za-z\d._?%&+\-=/#]+"
    r"|https?://(?:www\.)?kuaishou\.com/[A-Za-z\d._?%&+\-=/#]+"
    r"|https?://(?:v\.m\.)?chenzhongtech\.com/fw/[A-Za-z\d._?%&+\-=/#]+"
)

_URL_RE = re.compile(
    r"https?://(?:v\.kuaishou\.com|(?:www\.)?kuaishou\.com|(?:v\.m\.)?chenzhongtech\.com)"
    r"/[A-Za-z\d._?%&+\-=/#]+",
    re.IGNORECASE,
)
_PHOTO_ID_RE = re.compile(r"/(?:short-video|photo|long-video)/([A-Za-z0-9_-]+)")
_INIT_STATE_RE = re.compile(r"window\.INIT_STATE\s*=\s*(.*?)</script>", re.DOTALL)


def _loads(value: Any) -> Any:
    """新版页面里 photo / single / ext_params 可能是 JSON 字符串。"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _urls(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    return [str(i["url"]) for i in items if isinstance(i, dict) and i.get("url")]


def _cdns(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    return [str(i["cdn"]) for i in items if isinstance(i, dict) and i.get("cdn")]


def _extract_photo(html: str) -> dict[str, Any] | None:
    matched = _INIT_STATE_RE.search(html or "")
    if not matched:
        return None
    try:
        state = json.loads(matched.group(1).strip())
    except ValueError:
        return None
    if not isinstance(state, dict):
        return None

    for value in state.values():
        if not isinstance(value, dict) or "photo" not in value:
            continue
        photo = _loads(value.get("photo"))
        if not isinstance(photo, dict):
            continue
        single = _loads(value.get("single"))
        photo["_single"] = single if isinstance(single, dict) else {}
        return photo
    return None


def _album_urls(photo: dict[str, Any]) -> list[str]:
    """图集图片：新版 single，旧版 ext_params.atlas。"""
    ext = _loads(photo.get("ext_params")) or {}
    atlas = _loads(ext.get("atlas")) if isinstance(ext, dict) else None
    atlas = atlas if isinstance(atlas, dict) else {}

    cdns = _cdns(atlas.get("cdnList"))
    routes = [r for r in (atlas.get("list") or []) if isinstance(r, str)]
    if not cdns:
        raw = atlas.get("cdn")
        if isinstance(raw, str):
            cdns = [raw]
        elif isinstance(raw, list):
            cdns = [c for c in raw if isinstance(c, str)]

    single = photo.get("_single") or {}
    if not cdns:
        cdns = _cdns(single.get("cdnList"))
    if not routes:
        routes = [r for r in (single.get("imgRouteList") or []) if isinstance(r, str)]

    if not cdns or not routes:
        return []
    return [f"https://{cdns[0]}/{route}" for route in routes]


class KuaishouPlatform(BasePlatform):
    name = "kuaishou"
    display = "快手"
    capabilities = frozenset({CAP_VIDEO, CAP_IMAGES, CAP_SUMMARY})
    trigger_pattern = TRIGGER_PATTERN

    @classmethod
    def match(cls, text: str) -> MediaRef | None:
        if not text:
            return None
        matched = _URL_RE.search(text)
        if not matched:
            return None
        url = matched.group(0)
        pid = _PHOTO_ID_RE.search(urlparse(url).path or url)
        return MediaRef(
            platform=cls.name,
            raw=url,
            media_id=pid.group(1) if pid else None,
            is_short="/short-video/" not in url and "/photo/" not in url,
        )

    async def parse(self, ref: MediaRef, *, hint: ParseHint | None = None) -> MediaInfo:
        real_url = ref.raw
        if ref.is_short:
            real_url = await self._follow(ref.raw)
        real_url = real_url.replace("/fw/long-video/", "/fw/photo/")

        html = await self._fetch(real_url)
        photo = _extract_photo(html)
        if photo is None:
            # 作品不存在时页面会渲染「找不到该作品，为你推荐了热门作品」，
            # 此时 INIT_STATE 里是页面上下文（键名被字符混淆成 ``tusjoh...``），
            # 没有 photo。别把这种情况误报成「数据混淆、插件坏了」。
            if "找不到该作品" in html:
                raise MediaNotFound("快手提示找不到该作品（可能已删除或设为私密）")
            raise MediaNotFound("没拿到快手作品数据（页面结构可能变了）")

        caption = str(photo.get("caption") or "").strip()
        author = str(photo.get("userName") or photo.get("user_name") or "").replace("\u3164", "").strip()
        avatar = str(photo.get("headUrl") or photo.get("head_url") or "")
        created = int(photo.get("timestamp") or 0) // 1000
        duration_ms = int(photo.get("duration") or 0)

        videos = [u for u in _urls(photo.get("mainMvUrls")) if ".mp4" in u]
        covers = _urls(photo.get("coverUrls"))
        images = _album_urls(photo)

        items: list[MediaItem] = []
        for url in videos[:1]:
            items.append(
                MediaItem(
                    kind=KIND_VIDEO,
                    url=url,
                    referer="https://www.kuaishou.com/",
                    headers={"User-Agent": UA_IOS},
                )
            )
        for url in images[:9]:
            items.append(
                MediaItem(kind=KIND_IMAGE, url=url, referer="https://www.kuaishou.com/")
            )

        if not items:
            raise MediaNotFound("这个快手作品里没有可下载的视频或图集")

        pid = ref.media_id or (_PHOTO_ID_RE.search(real_url).group(1) if _PHOTO_ID_RE.search(real_url) else "")
        return MediaInfo(
            platform=self.name,
            media_id=pid,
            source_url=ref.raw,
            web_url=real_url,
            title=caption or "快手作品",
            author=author or "快手用户",
            author_avatar=avatar,
            cover=covers[0] if covers else (images[0] if images else ""),
            duration=duration_ms // 1000,
            pubdate=created,
            desc=caption,
            items=items,
            extras={
                "images": images,
                "video_url": videos[0] if videos else "",
                "qr_url": real_url,
            },
        )

    async def _follow(self, short_url: str) -> str:
        try:
            async with self.session.get(
                short_url,
                headers={"User-Agent": UA_IOS, "Referer": "https://v.kuaishou.com/"},
                timeout=self.timeout,
                allow_redirects=True,
            ) as resp:
                return str(resp.url) or short_url
        except Exception as exc:  # noqa: BLE001
            raise PlatformError(f"快手短链解析失败：{type(exc).__name__}") from exc

    async def _fetch(self, url: str) -> str:
        try:
            async with self.session.get(
                url,
                headers={
                    "User-Agent": UA_IOS,
                    "Referer": "https://v.kuaishou.com/",
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                },
                timeout=self.timeout,
                allow_redirects=True,
            ) as resp:
                if resp.status >= 400:
                    raise PlatformError(f"快手页面返回 HTTP {resp.status}")
                text = await resp.text(errors="replace")
        except PlatformError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PlatformError(f"快手页面请求失败：{type(exc).__name__}") from exc

        if "INIT_STATE" not in text:
            logger.info(f"{LOG_TAG} 快手页面里没有 INIT_STATE（长度 {len(text)}）")
        return text
