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
TAG_API = "https://api.bilibili.com/x/tag/archive/tags"

BILI_HEADERS = {
    **BASE_HEADERS,
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
}

# 单条热评喂给 AI 时的最大长度：避免某条长评把 prompt 顶爆，也控制 token 成本
# （见 OPTIMIZATION §9.5）。
REPLY_MAX_CHARS = 100

# 单条热评在**卡片上展示**的最大长度。必须与上面分开：展示追求完整（超长评论在
# 图片上被硬截断会显得莫名其妙），而喂 AI 追求省钱。这个值只影响图片，不影响 token。
REPLY_DISPLAY_MAX_CHARS = 400


def _to_int(value: object, default: int = 0) -> int:
    """把接口返回值稳妥地转成 int（B 站偶尔给字符串或 None）。"""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


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
    pubdate: int = 0        # 发布时间（Unix 秒）：卡片时间行用
    width: int = 0          # 分辨率宽（view 接口 dimension，热评图信息区用）
    height: int = 0         # 分辨率高
    owner_face: str = ""    # UP 主头像 URL：卡片圆形头像用
    owner_mid: int = 0      # UP 主 UID：用于在热评里标出「UP 主本人」
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


@dataclass(slots=True)
class ReplyInfo:
    """一条热门评论（结构化，供卡片展示与 AI 参考共用）。"""

    author: str
    avatar: str
    level: int
    content: str
    like: int
    mid: int = 0          # 评论者 UID：用于标出「UP 主本人」
    ctime: int = 0        # 发布时间（Unix 秒）
    reply_count: int = 0  # 该条评论下的回复数
    picture: str = ""     # 评论附图（B 站支持多图，这里只取第一张）
    medal_name: str = ""  # 粉丝勋章名（无勋章时为空串）
    medal_level: int = 0  # 粉丝勋章等级
    dress_no: str = ""    # 装扮编号（已格式化为 6 位，如 "000293"；无装扮时为空串）


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
            pubdate=int(data.get("pubdate") or 0),
            width=int((data.get("dimension") or {}).get("width") or 0),
            height=int((data.get("dimension") or {}).get("height") or 0),
            owner_face=str(owner.get("face") or ""),
            owner_mid=int(owner.get("mid") or 0),
            view=int(stat.get("view") or 0),
            like=int(stat.get("like") or 0),
            danmaku=int(stat.get("danmaku") or 0),
            reply=int(stat.get("reply") or 0),
            favorite=int(stat.get("favorite") or 0),
            coin=int(stat.get("coin") or 0),
            pages=pages,
        )

    async def get_hot_replies(self, aid: int, limit: int = 10) -> list[ReplyInfo]:
        """取热门评论（含作者、头像、等级、点赞数）。

        返回结构化数据，一处取两处用：AI 总结只取 ``content``，卡片展示会用到
        全部字段。接口不可用（风控、关闭评论区等）时返回空列表、不抛异常——
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

        replies: list[ReplyInfo] = []
        seen: set[str] = set()
        for item in data.get("replies") or []:
            if not isinstance(item, dict):
                continue
            content_obj = item.get("content") or {}
            content = content_obj.get("message") or ""
            text = " ".join(str(content).split())
            if not text or text in seen:
                continue
            seen.add(text)

            member = item.get("member") or {}
            level_info = member.get("level_info") or {}

            # 评论附图：B 站支持多图，只取第一张（多图会让卡片过长、渲染更慢）
            picture = ""
            pictures = content_obj.get("pictures")
            if isinstance(pictures, list) and pictures:
                first = pictures[0]
                if isinstance(first, dict):
                    picture = str(first.get("img_src") or "").strip()

            # 粉丝勋章：无勋章的用户该字段为 null / {} / level=0，统一收敛成空串，
            # 模板据此决定是否渲染，避免出现「空勋章」占位。
            fans = item.get("fans_detail")
            if not isinstance(fans, dict):
                fans = {}
            medal_name = str(fans.get("medal_name") or "").strip()
            medal_level = _to_int(fans.get("level"))

            # 装扮编号：用户佩戴「装扮」时 user_sailing.cardbg 才有值。
            # 编号优先取 fan.num_desc——B 站已经把它格式化成 6 位字符串（如 "070746"），
            # 与客户端显示的 NO.070746 完全一致；没有时再用 number / cardbg.id 补零。
            dress_no = ""
            sailing = member.get("user_sailing")
            if isinstance(sailing, dict):
                for key in ("cardbg", "cardbg_with_focus"):
                    cardbg = sailing.get(key)
                    if not isinstance(cardbg, dict):
                        continue
                    fan = cardbg.get("fan")
                    if isinstance(fan, dict):
                        dress_no = str(fan.get("num_desc") or "").strip()
                        if not dress_no:
                            number = _to_int(fan.get("number"))
                            if number > 0:
                                dress_no = f"{number:06d}"
                    if not dress_no:
                        card_id = _to_int(cardbg.get("id"))
                        if card_id > 0:
                            dress_no = f"{card_id:06d}"
                    if dress_no:
                        break

            replies.append(
                ReplyInfo(
                    author=str(member.get("uname") or "匿名用户").strip(),
                    avatar=str(member.get("avatar") or "").strip(),
                    level=_to_int(level_info.get("current_level")),
                    content=text[:REPLY_DISPLAY_MAX_CHARS],
                    like=_to_int(item.get("like")),
                    mid=_to_int(member.get("mid")),
                    ctime=_to_int(item.get("ctime")),
                    reply_count=_to_int(item.get("rcount")),
                    picture=picture,
                    medal_name=medal_name,
                    medal_level=medal_level if medal_name else 0,
                    dress_no=dress_no,
                )
            )
            if len(replies) >= limit:
                break
        return replies

    async def get_tags(self, bvid: str, limit: int = 8) -> list[str]:
        """取视频标签，用于补强「这是什么类型的视频」这一信号。

        标签是公开信息（无需登录），而且往往比简介更能说明内容方向
        （例如「英雄联盟」「电竞」「实况」）。接口异常时返回空列表——
        标签只是锦上添花，不该阻断总结。
        """
        if not bvid or limit <= 0:
            return []

        try:
            data = await self._get_json(TAG_API, {"bvid": bvid})
        except BiliApiError as exc:
            logger.debug(f"视频标签获取失败（忽略）：{exc}")
            return []

        # 该接口的 data 直接是数组；空数组会被 _get_json 归一成 {}，所以这里要判类型
        tags: list[str] = []
        for item in (data if isinstance(data, list) else []):
            name = str((item or {}).get("tag_name") or "").strip()
            if name and name not in tags:
                tags.append(name)
            if len(tags) >= limit:
                break
        return tags


__all__ = ["BiliApi", "BiliApiError", "ReplyInfo", "VideoInfo", "VideoPage"]
