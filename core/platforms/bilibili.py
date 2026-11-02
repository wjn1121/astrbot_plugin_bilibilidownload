"""哔哩哔哩平台。

这是第一个平台实现，同时也是**新增平台的样板**：照着它写一个文件，实现
``match()`` / ``parse()`` / ``resolve_media()``，就够了。

这里把原先散在 ``main.py`` 里的 B 站专属逻辑（短链归一化、视频信息/热评/标签
缓存、WBI 签名、字幕获取、ebilibili 换直链）全部收进来。编排层从此只认
``MediaInfo``，不再认识 bvid / cid / sessdata 这些概念。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from astrbot.api import logger

from ..bili_api import REPLY_MAX_CHARS, BiliApi, BiliApiError, ReplyInfo, VideoInfo
from ..cache import TtlCache
from ..ebilibili import EbilibiliClient
from ..link_parser import TRIGGER_REGEX, aid_from_url, bvid_from_url, find_ref
from ..media import (
    CAP_REPLIES,
    CAP_SUBTITLE,
    CAP_SUMMARY,
    CAP_VIDEO,
    MediaInfo,
    MediaItem,
    MediaRef,
)
from ..subtitle import fetch_subtitle, fetch_wbi_keys
from .base import BasePlatform, ParseHint
from .errors import MediaNotFound, MediaProtected, PlatformError

LOG_TAG = "[bilibili-download]"

# 视频信息 / 热评 / 标签 / 字幕的进程内缓存 TTL（秒）。
# 播放量这类统计会变，TTL 不宜过长；同一个视频常被多个群先后发出，缓存能省重复请求。
# 直链有时效，**绝不缓存**。
INFO_CACHE_TTL = 300
REPLY_CACHE_TTL = 300


class BilibiliPlatform(BasePlatform):
    name = "bilibili"
    display = "哔哩哔哩"
    capabilities = frozenset({CAP_VIDEO, CAP_SUMMARY, CAP_SUBTITLE, CAP_REPLIES})
    # 精确的触发正则（FV/av/完整链接/短链），比通用 URL 正则省掉大量无关消息
    trigger_pattern = TRIGGER_REGEX

    def __init__(
        self,
        session,  # noqa: ANN001 - aiohttp.ClientSession，避免顶层导入开销
        *,
        timeout: int = 20,
        verbose: bool = False,
        sessdata: str = "",
        prefer_subtitle: bool = True,
        subtitle_max_chars: int = 6000,
    ) -> None:
        super().__init__(session, timeout=timeout, verbose=verbose)
        self.sessdata = sessdata
        self.prefer_subtitle = prefer_subtitle
        self.subtitle_max_chars = subtitle_max_chars

        # 跨会话共享：同一个视频被多个群先后发出时不重复请求 B 站
        self._info_cache = TtlCache(max_items=256, ttl=INFO_CACHE_TTL, name="视频信息")
        self._reply_cache = TtlCache(max_items=256, ttl=REPLY_CACHE_TTL, name="热评")

    # ── 构造 ────────────────────────────────────────────────────────────

    @classmethod
    def create(cls, session, *, settings: dict) -> "BilibiliPlatform":  # noqa: ANN001
        """从配置快照构造实例。

        新增平台时若需要新的配置项，在这里读取即可——``main.py`` 只负责把
        ``dict(config)`` 递过来，不认识任何平台专属的键。
        """
        return cls(
            session,
            timeout=int(settings.get("request_timeout") or 20),
            verbose=bool(settings.get("verbose_log")),
            sessdata=str(settings.get("sessdata") or "").strip(),
            prefer_subtitle=bool(settings.get("prefer_subtitle", True)),
            subtitle_max_chars=int(settings.get("subtitle_max_chars") or 6000),
        )

    @property
    def api(self) -> BiliApi:
        return BiliApi(self.session, self.timeout)

    def apply_credentials(self, data: dict[str, str]) -> None:
        """面板里填的 SESSDATA 优先于插件配置（面板是"临时覆盖"，改配置仍是主路径）。"""
        sessdata = str(data.get("sessdata") or "").strip()
        if sessdata:
            self.sessdata = sessdata
            logger.info(f"{LOG_TAG} 已应用面板里保存的 SESSDATA（字幕增强可用）")

    # ── 匹配 ────────────────────────────────────────────────────────────

    @classmethod
    def match(cls, text: str) -> MediaRef | None:
        ref = find_ref(text)
        if ref is None:
            return None

        # 裸 BV/av 号直接给出 media_id；短链要等一次跳转才知道
        media_id = ref.bvid or (f"av{ref.aid}" if ref.aid else None)
        return MediaRef(
            platform=cls.name,
            raw=ref.raw,
            media_id=media_id,
            page=ref.page,
            is_short=ref.is_short_link,
            extra={"bili_ref": ref},
        )

    # ── 解析 ────────────────────────────────────────────────────────────

    async def parse(self, ref: MediaRef, *, hint: ParseHint | None = None) -> MediaInfo:
        hint = hint or ParseHint()
        api = self.api
        bili_ref = ref.extra.get("bili_ref")

        bvid, aid = await self._normalize(api, ref, bili_ref)
        info = await self._get_view_cached(api, bvid=bvid, aid=aid)

        media = self._to_media_info(info, ref)

        if not hint.want_summary:
            return media

        # 热评与标签互不依赖，并行取可省一次往返；字幕要先用 WBI keys 签名，单独走
        replies, tags = await asyncio.gather(
            self._get_replies_cached(api, info.aid, hint.reply_limit),
            self._get_tags_cached(api, info.bvid),
        )
        subtitle_text = await self._get_subtitle_text(info, ref.page) if hint.want_subtitle else None

        media.extras["replies"] = replies
        media.extras["tags"] = tags
        media.extras["subtitle_text"] = subtitle_text
        media.extras["subtitle_basis"] = "基于字幕" if subtitle_text else ""
        return media

    async def _normalize(
        self, api: BiliApi, ref: MediaRef, bili_ref
    ) -> tuple[str, int | None]:  # noqa: ANN001
        """把引用归一化成 (bvid, aid)，短链需要一次跳转。"""
        bvid = bili_ref.bvid if bili_ref else None
        aid = bili_ref.aid if bili_ref else None

        if ref.is_short and bili_ref is not None and bili_ref.short_url:
            final_url = await api.resolve_short_url(bili_ref.short_url)
            bvid = bvid_from_url(final_url) or bvid
            aid = aid_from_url(final_url) or aid
            if bvid:
                ref.media_id = bvid
            logger.info(f"{LOG_TAG} 短链已解析：{bili_ref.short_url} -> {final_url}")

        if not bvid and aid is None:
            raise PlatformError("无法从链接中识别视频编号")
        return bvid, aid

    def _to_media_info(self, info: VideoInfo, ref: MediaRef) -> MediaInfo:
        """``VideoInfo``（B 站内部模型）→ ``MediaInfo``（跨平台模型）。

        原样的 ``VideoInfo`` 也塞进 ``extras["video_info"]``：卡片渲染、热评图、
        字幕总结都还要用到 cid / 分P名 / 分辨率这些 B 站字段，转换时不必把它们
        压扁成通用字段。
        """
        return MediaInfo(
            platform=self.name,
            media_id=info.bvid or (f"av{info.aid}" if info.aid else ref.media_id or ""),
            page=ref.page,
            source_url=ref.raw,
            web_url=EbilibiliClient.web_url(info.bvid) if info.bvid else "",
            title=info.title,
            author=info.owner,
            author_id=str(info.owner_mid or ""),
            author_avatar=info.owner_face,
            cover=info.cover,
            duration=info.duration,
            desc=info.desc,
            pubdate=info.pubdate,
            stats={
                "view": info.view,
                "like": info.like,
                "danmaku": info.danmaku,
                "reply": info.reply,
                "favorite": info.favorite,
                "coin": info.coin,
            },
            extras={
                "video_info": info,
                "pages": [page.part for page in info.pages],
                "width": info.width,
                "height": info.height,
                "tname": info.tname,
                # 二维码要指向 bilibili.com（而不是下载站），否则扫出来是别人的页面
                "qr_url": f"https://www.bilibili.com/video/{info.bvid}" if info.bvid else "",
                "replies": [],
                "tags": [],
                "subtitle_text": None,
                "subtitle_basis": "",
            },
        )

    # ── 直链 ────────────────────────────────────────────────────────────

    async def resolve_media(
        self,
        info: MediaInfo,
        *,
        on_attempt: Callable[[str, bool, str], None] | None = None,
    ) -> list[MediaItem]:
        """用 ebilibili 换 B 站 CDN 直链（第三方站点，三条路径逐级降级）。"""
        video = info.extra("video_info")
        if not isinstance(video, VideoInfo):
            raise PlatformError("缺少视频信息，无法获取直链")

        client = EbilibiliClient(
            self.session,
            timeout=self.timeout,
            retries=2,
            verbose=self.verbose,
            on_attempt=on_attempt,
        )
        play = await client.resolve(
            bvid=video.bvid,
            cid=video.cid_for_page(info.page),
            original_link=info.source_url or video.bvid,
        )
        if play is None:
            raise PlatformError("下载站三条路径都没取到直链")

        return [
            MediaItem(
                kind="video",
                url=play.url,
                filename=play.filename or f"{video.bvid}.mp4",
                referer="https://www.bilibili.com/",
            )
        ]

    def web_url(self, info: MediaInfo) -> str:
        video = info.extra("video_info")
        bvid = getattr(video, "bvid", "") or info.media_id
        return EbilibiliClient.web_url(bvid) if bvid else info.web_url

    # ── 缓存层 ──────────────────────────────────────────────────────────

    async def _get_view_cached(self, api: BiliApi, *, bvid: str, aid: int | None) -> VideoInfo:
        key = f"view:{bvid or aid}"
        cached = self._info_cache.get(key)
        if isinstance(cached, VideoInfo):
            if self.verbose:
                logger.info(f"{LOG_TAG} 视频信息命中缓存：{key}")
            return cached

        try:
            info = await api.get_view(bvid=bvid, aid=aid)
        except BiliApiError as exc:
            raise self._to_platform_error(exc) from exc
        self._info_cache.set(key, info)
        return info

    async def _get_replies_cached(self, api: BiliApi, aid: int, limit: int) -> list[ReplyInfo]:
        """空结果不缓存，避免一次接口抖动把「没有热评」固化 5 分钟。"""
        if limit <= 0:
            return []
        key = f"reply:{aid}:{limit}"
        cached = self._reply_cache.get(key)
        if isinstance(cached, list):
            if self.verbose:
                logger.info(f"{LOG_TAG} 热评命中缓存：{key}")
            return cached

        replies = await api.get_hot_replies(aid, limit)
        if replies:
            self._reply_cache.set(key, replies)
        return replies

    async def _get_tags_cached(self, api: BiliApi, bvid: str) -> list[str]:
        if not bvid:
            return []
        key = f"tags:{bvid}"
        cached = self._info_cache.get(key)
        if isinstance(cached, list):
            if self.verbose:
                logger.info(f"{LOG_TAG} 视频标签命中缓存：{key}")
            return cached

        tags = await api.get_tags(bvid)
        if tags:
            self._info_cache.set(key, tags)
        return tags

    async def _get_wbi_keys(self) -> tuple[str, str] | None:
        if not self.sessdata:
            return None
        cached = self._info_cache.get("wbi")
        if isinstance(cached, tuple) and len(cached) == 2:
            return cached

        keys = await fetch_wbi_keys(self.session, sessdata=self.sessdata, timeout=self.timeout)
        if keys:
            self._info_cache.set("wbi", keys)
        return keys

    async def _get_subtitle_text(self, info: VideoInfo, page: int) -> str | None:
        """尝试取字幕文本（L3 总结的输入）。任何一步失败都返回 None，调用方回退 L1/L2。"""
        if not (self.sessdata and self.prefer_subtitle):
            return None

        key = f"subtitle:{info.bvid}:{page}"
        cached = self._info_cache.get(key)
        if isinstance(cached, str):
            if self.verbose:
                logger.info(f"{LOG_TAG} 字幕命中缓存：{key}")
            return cached

        try:
            keys = await self._get_wbi_keys()
            if keys is None:
                return None
            result = await fetch_subtitle(
                self.session,
                bvid=info.bvid,
                cid=info.cid_for_page(page),
                sessdata=self.sessdata,
                img_key=keys[0],
                sub_key=keys[1],
                timeout=self.timeout,
                max_chars=self.subtitle_max_chars,
            )
        except Exception as exc:  # noqa: BLE001 - 字幕是增强项，失败一律降级
            logger.warning(
                f"{LOG_TAG} 字幕获取失败，改用元数据总结：{type(exc).__name__}: {exc}"
            )
            return None

        if result is None:
            logger.info(f"{LOG_TAG} {info.bvid} 无可用字幕（或登录态失效），改用元数据总结")
            return None

        logger.info(
            f"{LOG_TAG} 字幕获取成功：{result.lan_doc or result.lan}，"
            f"{len(result.cues)} 段，{len(result.text)} 字"
        )
        self._info_cache.set(key, result.text)
        return result.text

    # ── 总结输入 ────────────────────────────────────────────────────────

    def summary_text(self, info: MediaInfo) -> str:
        """B 站的总结信号比通用平台丰富：分区、标签、分P名都能说明内容方向。"""
        video = info.extra("video_info")
        parts = [info.title, info.desc]
        if isinstance(video, VideoInfo) and video.tname:
            parts.append(f"分区：{video.tname}")
        tags = info.extra("tags") or []
        if tags:
            parts.append("标签：" + "、".join(tags))
        return "\n".join(part for part in parts if part).strip()

    # ── 工具 ────────────────────────────────────────────────────────────

    @staticmethod
    def _to_platform_error(exc: BiliApiError) -> PlatformError:
        """把 B 站接口异常翻成平台异常，保住"错误提示能直接发群里"这条线。"""
        text = str(exc)
        if "412" in text:
            return MediaProtected("B 站风控拦截（HTTP 412），请稍后再试", detail=text)
        if "code=-404" in text or "-404" in text:
            return MediaNotFound("视频不存在或已被删除", detail=text)
        return PlatformError(text, detail=text)

    def cache_snapshot(self) -> dict:
        """供面板「缓存」页签展示。"""
        return {
            "info": self._info_cache.stats(),
            "replies": self._reply_cache.stats(),
        }

    def clear_cache(self) -> int:
        return self._info_cache.clear() + self._reply_cache.clear()


__all__ = ["BilibiliPlatform", "REPLY_MAX_CHARS"]
