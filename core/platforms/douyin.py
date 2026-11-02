"""抖音平台。

实测结论（2026-11-01，样本 `https://v.douyin.com/OvUL1HIVkLE/`）：

- 短链跟随后落到 ``www.iesdouyin.com/share/video/{aweme_id}/``；
- **分享页已经是空壳**：``window._ROUTER_DATA`` 里只有 ua / query / abParams，
  没有 ``videoInfoRes``，页面里也没有 ``RENDER_DATA``、``og:video``、``play_addr``；
- 所有公开数据接口都已关闭或需要签名：
  - ``/web/api/v2/aweme/iteminfo`` → 200 但响应体 0 字节（已废弃）
  - ``api.amemv.com`` / ``aweme.snssdk.com`` / ``api5-normal-*.amemv.com`` → 同样 200 空体
  - ``/aweme/v1/web/aweme/detail`` → ``403 Blocked by ArgusSecurityPlugin Uifid Not Found``
  - 先访问抖音主页想拿 ttwid → 主页只回 2.4KB 的 JS 挑战页，**一个 cookie 都不下发**
- 结论：现在必须带 ``a_bogus`` 签名（JS 混淆实现）+ uifid 才能取数据，纯 HTTP 拿不到。

所以这个平台当前只做两件事：

1. **认得出抖音链接**（包括短链与分享口令），不会像以前那样毫无反应；
2. **如实降级**：告诉用户"抖音已开签名校验，暂时取不到内容，点链接可在 App 内看"。

如果将来抖音改回 SSR（或我们决定接签名/渲染方案），只要在 ``parse()`` 里补上
``_parse_from_ssr()`` 的调用即可——匹配、注册、降级链路都已经就位。
"""

from __future__ import annotations

import json
import re
import time

from astrbot.api import logger

from ..media import CAP_IMAGES, CAP_SUMMARY, CAP_VIDEO, MediaInfo, MediaItem, MediaRef
from .base import BasePlatform, ParseHint
from .errors import MediaNotFound, PlatformError

LOG_TAG = "[bilibili-download]"

UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)

_SHORT_PATTERN = r"v\.douyin\.com"
_AWEME_ID = r"\d{15,25}"

# ttwid 的缓存时长。抖音没公开有效期，6 小时是"够久又不会用到太旧的"折中；
# 真失效了也没关系——下一发冷请求会重新下发一个。
TTWID_TTL = 6 * 3600

TRIGGER_PATTERN = (
    rf"https?://(?:{_SHORT_PATTERN})/[0-9A-Za-z]+"
    rf"|https?://(?:www\.)?(?:iesdouyin|douyin)\.com/(?:share/)?video/{_AWEME_ID}"
    rf"|https?://(?:www\.)?douyin\.com/note/{_AWEME_ID}"
)

_SHORT_RE = re.compile(rf"https?://(?:{_SHORT_PATTERN})/[0-9A-Za-z]+", re.IGNORECASE)
_URL_RE = re.compile(TRIGGER_PATTERN, re.IGNORECASE)
_ID_RE = re.compile(rf"(?:video|note)/({_AWEME_ID})")
_ROUTER_MARKER = "_ROUTER_DATA"


class DouyinPlatform(BasePlatform):
    name = "douyin"
    display = "抖音"
    capabilities = frozenset({CAP_VIDEO, CAP_IMAGES, CAP_SUMMARY})
    trigger_pattern = TRIGGER_PATTERN

    def __init__(
        self,
        session,
        *,
        timeout: int = 20,
        verbose: bool = False,
        api_url: str = "",
    ) -> None:
        super().__init__(session, timeout=timeout, verbose=verbose)
        # 面板里填的第三方解析接口。留空 = 只识别不解析。
        self.api_url = api_url
        # 抖音分享页只在**带有效 ttwid** 的请求里才把作品数据塞进 _ROUTER_DATA。
        # 冷请求不发数据、但会在 Set-Cookie 里下发一个 ttwid，下一发带着才有内容。
        # aiohttp 的 cookie jar 常常不认这个 Domain（.douyin.com 对 iesdouyin.com），
        # 所以自己捞出来显式放进请求头——这一条决定了「首次解析成功还是失败」。
        self._ttwid = ""
        self._ttwid_at = 0.0

    def apply_credentials(self, data: dict[str, str]) -> None:
        api_url = str(data.get("api_url") or "").strip()
        self.api_url = api_url
        if api_url:
            logger.info(f"{LOG_TAG} 已配置抖音第三方解析接口：{_mask_url(api_url)}")

    # ── 匹配 ────────────────────────────────────────────────────────────

    @classmethod
    def match(cls, text: str) -> MediaRef | None:
        if not text:
            return None

        # 口令文本里 URL 后面常跟中文/标点，用专门的 URL 正则，不用整段文本
        url_match = _URL_RE.search(text)
        if not url_match:
            return None

        raw = url_match.group(0)
        ident = _ID_RE.search(raw)
        return MediaRef(
            platform=cls.name,
            raw=raw,
            media_id=ident.group(1) if ident else None,
            page=1,
            is_short=bool(_SHORT_RE.fullmatch(raw)),
        )

    # ── 解析 ────────────────────────────────────────────────────────────

    async def parse(self, ref: MediaRef, *, hint: ParseHint | None = None) -> MediaInfo:
        page_url = ref.raw
        aweme_id = ref.media_id or ""

        if ref.is_short or not aweme_id:
            page_url = await self._follow(ref.raw)
            ident = _ID_RE.search(page_url)
            if ident:
                aweme_id = ident.group(1)

        if not aweme_id:
            raise MediaNotFound("没能从抖音链接里识别出作品 ID")

        # 1) 配了第三方接口就走它（抖音自己的接口全要签名，见模块 docstring）
        media = await self._parse_via_api(ref, aweme_id, page_url)
        if media is not None:
            logger.info(f"{LOG_TAG} 抖音通过第三方接口解析成功：{aweme_id}")
            return media

        # 2) 没配接口时试 SSR：抖音哪天恢复分享页数据，这里就自动可用。
        #    失败重试是必需的，原因有两条实测依据（2026-11-01）：
        #    a) **冷启动第一次几乎必失败**：首次访问会被重定向到挑战页并下发
        #       cookie，带着这次拿到的 cookie 重发才有真页面（同一链接连测：
        #       进程内第 1 次 FAIL、第 2、3 次都 OK）；
        #    b) 短链跟随偶尔落到风控壳页，用作品 ID 直接打开 iesdouyin 分享页可以绕过。
        candidates = [page_url]
        share_kind = "note" if "/note/" in page_url else "video"
        direct = f"https://www.iesdouyin.com/share/{share_kind}/{aweme_id}/"
        if direct.split("?")[0].rstrip("/") != page_url.split("?")[0].rstrip("/"):
            candidates.append(direct)
        # 无论上面有没有换入口，都再补一次原地址：这一次请求会带上第一次握手
        # 拿到的 cookie，也就是把「冷启动失败」这一种兜掉。
        candidates.append(page_url)

        for index, url in enumerate(candidates):
            html = await self._fetch(url)
            media = self._parse_from_ssr(html, aweme_id, ref, url)
            if media is not None:
                suffix = "" if index == 0 else f"（第 {index + 1} 次尝试命中）"
                logger.info(f"{LOG_TAG} 抖音 SSR 解析成功：{aweme_id}{suffix}")
                return media

        if self.api_url:
            raise PlatformError(
                "抖音第三方接口没返回可用数据，可能是接口失效或格式不符",
                detail=f"{aweme_id} api={_mask_url(self.api_url)}",
            )
        raise PlatformError(
            "抖音现在要求签名校验，暂时取不到视频内容。点开链接可以在抖音里直接看。",
            detail=(
                f"{aweme_id} SSR 无 item_list；2026-11-01 实测接口均已关闭或需 a_bogus。"
                "可在插件面板的「平台」页配置第三方解析接口"
            ),
        )

    # ── 第三方接口 ──────────────────────────────────────────────────────

    async def _parse_via_api(
        self, ref: MediaRef, aweme_id: str, page_url: str
    ) -> MediaInfo | None:
        """调用面板里配置的第三方解析接口。

        接口格式不做强约定（各家的 JSON 结构都不一样），只要求返回 JSON，
        然后按常见的键名宽松提取；实在认不出就退回去走 SSR / 降级提示。
        支持 ``{url}`` 占位符，没有占位符时自动追加 ``?url=``。
        """
        if not self.api_url:
            return None

        from urllib.parse import quote

        target = page_url or ref.raw or f"https://www.douyin.com/video/{aweme_id}"
        endpoint = self.api_url
        if "{url}" in endpoint:
            endpoint = endpoint.replace("{url}", quote(target, safe=""))
        elif "url=" not in endpoint:
            endpoint = f"{endpoint}{'&' if '?' in endpoint else '?'}url={quote(target, safe='')}"

        try:
            async with self.session.get(
                endpoint,
                headers={"User-Agent": UA_MOBILE, "Accept": "application/json, */*"},
                timeout=_timeout(self.timeout),
            ) as resp:
                if resp.status >= 400:
                    logger.warning(f"{LOG_TAG} 抖音第三方接口返回 HTTP {resp.status}")
                    return None
                payload = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001 - 第三方挂了不影响降级链路
            logger.warning(
                f"{LOG_TAG} 抖音第三方接口请求失败：{type(exc).__name__}: {exc}"
            )
            return None

        if not isinstance(payload, (dict, list)):
            return None

        video_url = _first_url(payload, ("video_url", "play_url", "download_url", "video"))
        images = _first_url_list(payload, ("images", "pics", "image_urls", "pic"))
        if not video_url and not images:
            logger.info(f"{LOG_TAG} 抖音第三方接口返回里没有可用的媒体地址")
            return None

        title = _first_str(payload, ("title", "desc", "text", "caption", "content"))
        author = _first_str(payload, ("nickname", "author_name", "author", "name"))
        cover = _first_url(payload, ("cover", "origin_cover", "cover_url", "poster"))

        items: list[MediaItem] = []
        for image in images:
            # 第三方接口给的地址也可能带水印参数，统一清一遍
            items.append(
                MediaItem(
                    kind="image",
                    url=_strip_watermark(image),
                    referer="https://www.douyin.com/",
                )
            )
        if video_url and not _is_video_url(video_url):
            # 第三方接口偶尔也会把背景音乐塞进 video_url，一并挡掉
            logger.info(f"{LOG_TAG} 抖音第三方接口的 video_url 不像视频，已忽略")
            video_url = ""
        if video_url:
            items.insert(
                0,
                MediaItem(
                    kind="video",
                    url=video_url,
                    referer="https://www.douyin.com/",
                    headers={"User-Agent": UA_MOBILE},
                ),
            )

        return MediaInfo(
            platform=self.name,
            media_id=aweme_id,
            page=1,
            source_url=ref.raw,
            web_url=f"https://www.douyin.com/video/{aweme_id}",
            title=title or "抖音作品",
            author=author,
            cover=cover or (images[0] if images else ""),
            desc=title,
            items=items,
            extras={
                "qr_url": f"https://www.douyin.com/video/{aweme_id}",
                "images": images,
                "tags": [],
                "via": "third_party_api",
            },
        )

    async def _follow(self, short_url: str) -> str:
        try:
            async with self.session.get(
                short_url,
                headers={"User-Agent": UA_MOBILE, **self._ttwid_header()},
                timeout=_timeout(self.timeout),
                allow_redirects=True,
            ) as resp:
                self._remember_ttwid(resp)
                return str(resp.url)
        except Exception as exc:  # noqa: BLE001
            raise PlatformError(f"抖音短链解析失败：{type(exc).__name__}") from exc

    async def _fetch(self, url: str) -> str:
        try:
            async with self.session.get(
                url,
                headers={
                    "User-Agent": UA_MOBILE,
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                    **self._ttwid_header(),
                },
                timeout=_timeout(self.timeout),
                allow_redirects=True,
            ) as resp:
                self._remember_ttwid(resp)
                if resp.status >= 400:
                    raise PlatformError(f"抖音页面返回 HTTP {resp.status}")
                return await resp.text(errors="replace")
        except PlatformError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PlatformError(f"抖音页面请求失败：{type(exc).__name__}") from exc

    # ── ttwid 会话 ──────────────────────────────────────────────────────

    def _ttwid_header(self) -> dict[str, str]:
        """把缓存的 ttwid 拼成 Cookie 头；没有或已过期就返回空字典。"""
        if not self._ttwid:
            return {}
        if time.time() - self._ttwid_at > TTWID_TTL:
            self._ttwid = ""
            return {}
        return {"Cookie": f"ttwid={self._ttwid}"}

    def _remember_ttwid(self, response) -> None:
        """从响应的 Set-Cookie 里捞 ttwid。

        不依赖 aiohttp 的 cookie jar：这个 cookie 的 Domain 是 ``.douyin.com``，
        而我们要请求的是 ``www.iesdouyin.com`` 和 ``v.douyin.com``，jar 不一定
        会把它带出去；显式取出来最省事，也避免"有时能用有时不能用"的随机性。
        """
        try:
            raw_cookies = response.headers.getall("Set-Cookie", [])
        except (AttributeError, TypeError):
            return
        for raw in raw_cookies:
            name, _, rest = raw.partition("=")
            if name.strip().lower() != "ttwid":
                continue
            value = rest.split(";", 1)[0].strip()
            if value and value != self._ttwid:
                self._ttwid = value
                self._ttwid_at = time.time()
                logger.debug(f"{LOG_TAG} 已更新抖音会话凭证 ttwid（{len(value)} 字符）")
            return

    # ── SSR 解析（当前拿不到数据，保留以便将来恢复） ──────────────────────

    def _parse_from_ssr(
        self, html: str, aweme_id: str, ref: MediaRef, page_url: str
    ) -> MediaInfo | None:
        state = _extract_router_data(html)
        if not state:
            return None

        loader = state.get("loaderData") or {}
        item = None
        for key, value in loader.items():
            if not isinstance(value, dict):
                continue
            response = value.get("videoInfoRes") or {}
            items = response.get("item_list") or []
            if items and isinstance(items[0], dict):
                item = items[0]
                break
        if item is None:
            return None

        author = item.get("author") or {}
        video = item.get("video") or {}
        stats = item.get("statistics") or {}

        images = item.get("images") or []
        is_gallery = bool(images)

        items: list[MediaItem] = []
        for image in images:
            # 图集：挑无水印的那条 URL（带水印的会有「抖音」角标）
            url = _pick_image_url(image)
            if url:
                items.append(
                    MediaItem(kind="image", url=url, referer="https://www.douyin.com/")
                )

        # 只有真视频才加 video item。
        # 图文作品的 ``video.play_addr`` 里放的是**背景音乐**（实测指向
        # ``ies-music/xxx.mp3``），早先没判类型就当成视频塞进去，结果是：
        # 下载一个 mp3 → 当视频发 → QQ 拒收 → 群里冒出「视频没有发送成功」，
        # 而 34 张图一张都没发。2026-11-01 实测这条图文作品踩的就是这个坑。
        if not is_gallery:
            for addr in (video.get("play_addr") or {}).get("url_list") or []:
                if addr and _is_video_url(str(addr)):
                    items.append(
                        MediaItem(
                            kind="video",
                            url=str(addr).replace("playwm", "play"),
                            referer="https://www.douyin.com/",
                            headers={"User-Agent": UA_MOBILE},
                        )
                    )
                    break

        # 封面：图文用第一张图（``video.cover`` 在图文里往往是音轨配图）
        if is_gallery and items:
            cover = items[0].url
        else:
            cover = str(((video.get("cover") or {}).get("url_list") or [""])[0] or "")

        return MediaInfo(
            platform=self.name,
            media_id=aweme_id,
            page=1,
            source_url=ref.raw,
            # 图文作品的落地页是 /note/，用 /video/ 会跳到「作品不存在」
            web_url=f"https://www.douyin.com/{'note' if is_gallery else 'video'}/{aweme_id}",
            title=str(item.get("desc") or "").strip() or "抖音作品",
            author=str(author.get("nickname") or ""),
            author_id=str(author.get("uid") or ""),
            author_avatar=str(
                ((author.get("avatar_thumb") or {}).get("url_list") or [""])[0] or ""
            ),
            cover=cover,
            duration=int((video.get("duration") or 0) // 1000),
            desc=str(item.get("desc") or ""),
            stats={
                "like": int(stats.get("digg_count") or 0),
                "reply": int(stats.get("comment_count") or 0),
                "share": int(stats.get("share_count") or 0),
            },
            items=items,
            extras={
                "qr_url": f"https://www.douyin.com/video/{aweme_id}",
                "tags": [],
                "images": [entry.url for entry in items if entry.kind == "image"],
            },
        )

    def web_url(self, info: MediaInfo) -> str:
        if info.media_id:
            return f"https://www.douyin.com/video/{info.media_id}"
        return info.web_url or info.source_url


def _mask_url(url: str) -> str:
    """日志里只留主机名：第三方接口地址常带 token，不能整条打出来。"""
    from urllib.parse import urlparse

    try:
        return urlparse(url or "").hostname or "?"
    except ValueError:
        return "?"


def _walk(obj, depth: int = 0):
    """深度优先遍历任意嵌套结构（深度封顶，防止畸形响应把栈打爆）。"""
    if depth > 8:
        return
    yield obj
    if isinstance(obj, dict):
        for value in obj.values():
            yield from _walk(value, depth + 1)
    elif isinstance(obj, list):
        for value in obj[:20]:
            yield from _walk(value, depth + 1)


def _url_like(value) -> str:
    """从一个值里抠出 http(s) 地址：字符串直接用，列表/字典往里找。"""
    if isinstance(value, str):
        return value.strip() if value.strip().startswith("http") else ""
    if isinstance(value, list):
        for item in value:
            found = _url_like(item)
            if found:
                return found
        return ""
    if isinstance(value, dict):
        for key in ("url_list", "url", "masterUrl", "play_addr", "download_url", "src", "uri"):
            if key in value:
                found = _url_like(value[key])
                if found:
                    return found
    return ""


#: 抖音图片 URL 里的水印标记与替换目标。
#:
#: 图集的每张图会下发多个 `url_list` 变体，其中有的走 `~tplv-dy-watermark`
#: 模板（图上有「抖音」角标），有的走 `~tplv-dy-aweme-images`（无水印原图）。
#: 实测里两种都会出现，所以既要挑、也要能改。
_WATERMARK_TPL = "~tplv-dy-watermark"
_CLEAN_TPL = "~tplv-dy-aweme-images:q75.webp"
_WATERMARK_QUERY = re.compile(r"[?&]watermark=\d+")


def _strip_watermark(url: str) -> str:
    """把图片地址上的水印标记摘掉（挑不到干净变体时的兜底）。"""
    if not url:
        return ""
    cleaned = url.replace(_WATERMARK_TPL, _CLEAN_TPL)
    cleaned = _WATERMARK_QUERY.sub("", cleaned)
    return cleaned


def _pick_image_url(image) -> str:
    """从一张图的多条 URL 里挑无水印的那条。

    优先取本身不带 ``watermark`` 的；一条都没有才拿第一条去改模板。
    """
    if not isinstance(image, dict):
        return ""
    urls = [str(item) for item in (image.get("url_list") or []) if item]
    if not urls:
        return ""
    clean = [item for item in urls if "watermark" not in item.lower()]
    return _strip_watermark((clean or urls)[0])


def _is_video_url(url: str) -> bool:
    """判断 ``video.play_addr`` 给的到底是不是视频。

    图文作品的这个字段装的是**背景音乐**，形如
    ``https://aweme.snssdk.com/aweme/v1/playwm/?video_id=https://.../ies-music/<id>.mp3``。
    以前不看内容就当视频发，结果是把 mp3 传上去被 QQ 拒收。
    """
    lowered = url.lower()
    if ".mp3" in lowered or "/ies-music/" in lowered or "ies-music" in lowered:
        return False
    return True


def _first_str(payload, keys: tuple[str, ...]) -> str:
    wanted = {key.lower() for key in keys}
    for node in _walk(payload):
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).lower() in wanted and isinstance(value, str) and value.strip():
                    return value.strip()
    return ""


def _first_url(payload, keys: tuple[str, ...]) -> str:
    wanted = {key.lower() for key in keys}
    for node in _walk(payload):
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).lower() in wanted:
                    found = _url_like(value)
                    if found:
                        return found
    # 兜底：任何以 .mp4 结尾的字符串
    for node in _walk(payload):
        if isinstance(node, str) and node.startswith("http") and ".mp4" in node:
            return node.split("?")[0]
    return ""


def _collect_urls(value) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        if value.startswith("http"):
            found.append(value)
    elif isinstance(value, list):
        for item in value:
            found.extend(_collect_urls(item))
    elif isinstance(value, dict):
        for key in ("url_default", "url", "url_list", "masterUrl", "src"):
            if key in value:
                found.extend(_collect_urls(value[key]))
    # 去重但保序
    seen: set[str] = set()
    unique: list[str] = []
    for url in found:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def _first_url_list(payload, keys: tuple[str, ...]) -> list[str]:
    wanted = {key.lower() for key in keys}
    for node in _walk(payload):
        if isinstance(node, dict):
            for key, value in node.items():
                if str(key).lower() in wanted:
                    urls = _collect_urls(value)
                    if urls:
                        return urls
    return []


def _extract_router_data(html: str) -> dict | None:
    """``window._ROUTER_DATA = {...}`` → dict（按大括号配平，跳过字符串里的括号）。"""
    index = html.find(_ROUTER_MARKER)
    if index < 0:
        return None
    start = html.find("{", index)
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False
    for position in range(start, len(html)):
        char = html[position]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start : position + 1].replace("undefined", "null"))
                except ValueError:
                    return None
    return None


def _timeout(seconds: int):
    import aiohttp

    return aiohttp.ClientTimeout(total=max(5, int(seconds)))


__all__ = ["TRIGGER_PATTERN", "DouyinPlatform"]
