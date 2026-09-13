"""B 站公开接口封装：视频信息、短链跳转、热门评论。

全部使用无需登录态即可访问的接口（已实测 2026-09-13）：
- ``/x/web-interface/view``   视频信息（bvid 或 aid 均可）
- ``/x/v2/reply/main``        热门评论（mode=3 为按热度排序，无需 wbi 签名）
字幕接口在未携带 SESSDATA 时返回空列表，属于二期能力，此处不实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import aiohttp

from astrbot.api import logger

from .http import BASE_HEADERS

VIEW_API = "https://api.bilibili.com/x/web-interface/view"
REPLY_API = "https://api.bilibili.com/x/v2/reply/main"

BILI_HEADERS = {
    **BASE_HEADERS,
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
}

# 单条热评用于总结时的最大长度，避免某条长评把 prompt 顶爆
REPLY_MAX_CHARS = 100


class BiliApiError(Exception):
    """B 站接口返回业务错误或网络异常。"""


@dataclass(slots=True)
class VideoPage:
    """分P信息。"""

    cid: int
    part: str
    duration: int


@dataclass(slots=True)
class VideoInfo:
    """一个视频的元数据快照。"""

    bvid: str
    aid: int
    cid: int
    title: str
    desc: str
    duration: int
    owner: str
    tname: str
    cover: str
    view: int = 0
    like: int = 0
    danmaku: int = 0
    reply: int = 0
    favorite: int = 0
    coin: int = 0
    pages: list[VideoPage] = field(default_factory=list)

    def cid_for_page(self, page: int) -> int:
        """取指定分P的 cid，越界时回退到主 cid。

        用户可能手改 ``?p=99``，此时按首个分P处理比直接报错更友好。
        """
        if 1 <= page <= len(self.pages):
            return self.pages[page - 1].cid
        return self.cid

    def part_name(self, page: int) -> str:
        if 1 <= page <= len(self.pages):
            return self.pages[page - 1].part
        return self.title


class BiliApi:
    """B 站接口客户端（复用外部传入的 aiohttp session）。"""

    def __init__(self, session: aiohttp.ClientSession, timeout: int = 20) -> None:
        self._session = session
        self._timeout = timeout

    async def _get_json(self, url: str, params: dict | None = None) -> dict:
        try:
            async with self._session.get(
                url,
                params=params,
                headers=BILI_HEADERS,
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            ) as resp:
                if resp.status >= 400:
                    raise BiliApiError(f"HTTP {resp.status}")
                payload = await resp.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise BiliApiError(f"网络错误：{exc}") from exc
        except TimeoutError as exc:
            raise BiliApiError("请求超时") from exc

        code = payload.get("code")
        if code != 0:
            raise BiliApiError(f"code={code} message={payload.get('message')}")
        return payload.get("data") or {}

    async def resolve_short_url(self, url: str) -> str:
        """跟随 b23.tv 短链拿到最终地址。

        优先读 302 的 Location（省一次请求），拿不到再跟随重定向取最终 URL。
        """
        try:
            async with self._session.get(
                url,
                headers=BILI_HEADERS,
                timeout=aiohttp.ClientTimeout(total=self._timeout),
                allow_redirects=False,
            ) as resp:
                location = resp.headers.get("Location")
                if location:
                    return location
        except (aiohttp.ClientError, TimeoutError) as exc:
            logger.debug(f"短链 Location 读取失败，改为跟随重定向：{exc}")

        try:
            async with self._session.get(
                url,
                headers=BILI_HEADERS,
                timeout=aiohttp.ClientTimeout(total=self._timeout),
                allow_redirects=True,
            ) as resp:
                return str(resp.url)
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise BiliApiError(f"短链解析失败：{exc}") from exc

    async def get_view(self, *, bvid: str | None = None, aid: int | None = None) -> VideoInfo:
        """获取视频信息，bvid 与 aid 至少提供一个。"""
        if not bvid and not aid:
            raise BiliApiError("缺少 bvid/aid")

        params = {"bvid": bvid} if bvid else {"aid": aid}
        data = await self._get_json(VIEW_API, params)

        stat = data.get("stat") or {}
        owner = data.get("owner") or {}
        pages = [
            VideoPage(
                cid=int(page.get("cid") or 0),
                part=str(page.get("part") or ""),
                duration=int(page.get("duration") or 0),
            )
            for page in (data.get("pages") or [])
            if page.get("cid")
        ]

        return VideoInfo(
            bvid=str(data.get("bvid") or bvid or ""),
            aid=int(data.get("aid") or aid or 0),
            cid=int(data.get("cid") or 0),
            title=str(data.get("title") or "").strip(),
            desc=str(data.get("desc") or "").strip(),
            duration=int(data.get("duration") or 0),
            owner=str(owner.get("name") or ""),
            tname=str(data.get("tname") or ""),
            cover=str(data.get("pic") or ""),
            view=int(stat.get("view") or 0),
            like=int(stat.get("like") or 0),
            danmaku=int(stat.get("danmaku") or 0),
            reply=int(stat.get("reply") or 0),
            favorite=int(stat.get("favorite") or 0),
            coin=int(stat.get("coin") or 0),
            pages=pages,
        )

    async def get_hot_replies(self, aid: int, limit: int = 10) -> list[str]:
        """取热门评论正文，用于给 AI 总结补充"观众在聊什么"。

        接口不可用（风控、关闭评论区等）时返回空列表，不抛异常——
        热评只是锦上添花，不该阻断主流程。
        """
        if not aid or limit <= 0:
            return []
        try:
            data = await self._get_json(
                REPLY_API, {"type": 1, "oid": aid, "mode": 3, "next": 0}
            )
        except BiliApiError as exc:
            logger.debug(f"热门评论获取失败（忽略）：{exc}")
            return []

        replies: list[str] = []
        seen: set[str] = set()
        for item in data.get("replies") or []:
            content = (item.get("content") or {}).get("message") or ""
            text = " ".join(str(content).split())
            if not text or text in seen:
                continue
            seen.add(text)
            replies.append(text[:REPLY_MAX_CHARS])
            if len(replies) >= limit:
                break
        return replies


__all__ = ["BiliApi", "BiliApiError", "VideoInfo", "VideoPage"]
