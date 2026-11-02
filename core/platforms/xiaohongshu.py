"""小红书平台。

实测结论（2026-11-01，样本 `https://xhslink.cn/o/5V9X9rBJbKg`）：

- 短链域名有两个：``xhslink.cn`` 与 ``xhslink.com``，跳转后落到
  ``www.xiaohongshu.com/discovery/item/{note_id}?xsec_token=...``；
- 页面里 ``window.__INITIAL_STATE__`` 带了完整数据，路径是
  ``noteData.data.noteData``（不是耳熟能详的 ``note.noteDetailMap``，那个是旧版结构）；
- 视频直链在 ``video.media.stream.h264[0].masterUrl``，图片在 ``imageList[].url``，
  两者都是 ``http://``，必须升级成 https 才能在群里显示/下载；
- 昵称字段是 ``user.nickName``（**不是** ``nickname``，写错就拿到 None）。

不需要登录、不需要 cookie。``xsec_token`` 已经包含在跳转后的 URL 里，直接用即可；
没有 token 时页面会变成风控页，此时如实报错而不是假装"笔记不存在"。
"""

from __future__ import annotations

import json
import re
from typing import Any

from astrbot.api import logger

from ..media import (
    CAP_IMAGES,
    CAP_SUMMARY,
    CAP_VIDEO,
    MediaInfo,
    MediaItem,
    MediaRef,
)
from .base import BasePlatform, ParseHint
from .errors import MediaNotFound, MediaProtected, PlatformError

LOG_TAG = "[bilibili-download]"

# 移动端 UA：PC UA 拿到的是需要登录的壳页，移动端分享页才是完整 SSR
UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)

#: 图片原图 CDN。用 `fileId` 直连这里是**无水印原图**（2026-11-01 实测）：
#:   - 页面给的 `!h5_1080jpg` 签名地址 → 正中压着一个「小红书」水印，393KB→165KB 被压过
#:   - `{IMAGE_CDN}/{fileId}` → 同图 393KB、尺寸更大、无任何水印
#: 同一份 fileId 在 `sns-img-qc` / `sns-img-bd` / `ci.xiaohongshu.com` 都能取到，
#: 这里用主 CDN，失败时由调用方退回签名地址。
IMAGE_CDN = "https://sns-img-qc.xhscdn.com"

# 短链域名 + 内容页形态。短链后面是一串大小写字母数字，长度不固定。
_SHORT_HOSTS = ("xhslink.cn", "xhslink.com")
_SHORT_PATTERN = "|".join(host.replace(".", r"\.") for host in _SHORT_HOSTS)
_NOTE_ID = r"[0-9a-f]{16,32}"

TRIGGER_PATTERN = (
    # 短链是两段路径：xhslink.cn/o/xxxx、xhslink.com/a/xxxx
    rf"https?://(?:{_SHORT_PATTERN})/[0-9A-Za-z/]+"
    rf"|https?://(?:www\.)?xiaohongshu\.com/(?:explore|discovery/item)/{_NOTE_ID}[^\s]*"
)

_SHORT_RE = re.compile(
    rf"https?://(?:{_SHORT_PATTERN})/[0-9A-Za-z/]+", re.IGNORECASE
)
_NOTE_RE = re.compile(
    rf"https?://(?:www\.)?xiaohongshu\.com/(?:explore|discovery/item)/({_NOTE_ID})[^\s]*",
    re.IGNORECASE,
)
# 页面里的 window.__INITIAL_STATE__ = {...}
_STATE_MARKER = "__INITIAL_STATE__"


def _https(url: str) -> str:
    """小红书的 CDN 链接给的是 http，群里显示/下载要 https。"""
    url = (url or "").strip()
    if url.startswith("http://"):
        return "https://" + url[len("http://") :]
    return url


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _extract_state(text: str) -> dict | None:
    """把 ``window.__INITIAL_STATE__`` 的对象抠出来。

    两个坑：
    1. 它不是严格 JSON —— 里面混着 ``undefined``，直接 ``json.loads`` 会炸；
    2. 不能用正则截到行尾，对象里有换行和嵌套，只能按大括号配平（并跳过字符串里的括号）。
    """
    index = text.find(_STATE_MARKER)
    if index < 0:
        return None
    start = text.find("{", index)
    if start < 0:
        return None

    depth = 0
    in_string = False
    escaped = False
    for position in range(start, len(text)):
        char = text[position]
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
                raw = text[start : position + 1].replace("undefined", "null")
                try:
                    return json.loads(raw)
                except ValueError as exc:
                    logger.debug(f"{LOG_TAG} 小红书页面 JSON 解析失败：{exc}")
                    return None
    return None


def _find_note(state: dict | None) -> dict:
    """在多处可能的位置里找笔记主体。

    新版在 ``noteData.data.noteData``，旧版/预加载在 ``normalNotePreloadData``，
    两边都试一次，避免小红书改结构就把解析打死。
    """
    if not isinstance(state, dict):
        return {}

    note_data = state.get("noteData")
    if isinstance(note_data, dict):
        inner = note_data.get("data")
        if isinstance(inner, dict):
            note = inner.get("noteData")
            if isinstance(note, dict) and note:
                return note
        preload = note_data.get("normalNotePreloadData")
        if isinstance(preload, dict) and preload:
            return preload

    # 兜底：老结构 note.noteDetailMap[note_id].note
    note = state.get("note")
    if isinstance(note, dict):
        detail = note.get("noteDetailMap")
        if isinstance(detail, dict):
            for value in detail.values():
                if isinstance(value, dict) and isinstance(value.get("note"), dict):
                    return value["note"]
    return {}


class XiaohongshuPlatform(BasePlatform):
    name = "xiaohongshu"
    display = "小红书"
    capabilities = frozenset({CAP_VIDEO, CAP_IMAGES, CAP_SUMMARY})
    trigger_pattern = TRIGGER_PATTERN

    def __init__(
        self,
        session,
        *,
        timeout: int = 20,
        verbose: bool = False,
        cookie: str = "",
    ) -> None:
        super().__init__(session, timeout=timeout, verbose=verbose)
        # 面板里保存的 Cookie（可选）。填了能提高"需要登录才能看"的笔记的成功率；
        # 不填也能解析大部分公开笔记。
        self.cookie = cookie

    def apply_credentials(self, data: dict[str, str]) -> None:
        cookie = str(data.get("cookie") or "").strip()
        if cookie:
            self.cookie = cookie
            logger.info(f"{LOG_TAG} 已应用面板里保存的小红书 Cookie（长度 {len(cookie)}）")

    def _headers(self, *, html: bool = False) -> dict[str, str]:
        headers = {
            "User-Agent": UA_MOBILE,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                if html
                else "application/json, text/plain, */*"
            ),
            "Referer": "https://www.xiaohongshu.com/",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    # ── 匹配 ────────────────────────────────────────────────────────────

    @classmethod
    def match(cls, text: str) -> MediaRef | None:
        if not text:
            return None

        # 先看内容页（直接带 note_id），再看短链（要跳转才知道 id）
        note = _NOTE_RE.search(text)
        if note:
            return MediaRef(
                platform=cls.name,
                raw=note.group(0),
                media_id=note.group(1),
                page=1,
            )

        short = _SHORT_RE.search(text)
        if short:
            return MediaRef(
                platform=cls.name,
                raw=short.group(0),
                media_id=None,
                page=1,
                is_short=True,
            )
        return None

    # ── 解析 ────────────────────────────────────────────────────────────

    async def parse(self, ref: MediaRef, *, hint: ParseHint | None = None) -> MediaInfo:
        page_url = ref.raw
        if ref.is_short:
            page_url = await self._follow(ref.raw)
            match = _NOTE_RE.search(page_url)
            if match:
                ref.media_id = match.group(1)

        html = await self._fetch_page(page_url)
        state = _extract_state(html)
        note = _find_note(state)

        if not note:
            # 风控页 / 需要登录 / 笔记已删，三种情况在这里收敛
            if "登录" in html or "verify" in html.lower():
                raise MediaProtected(
                    "小红书返回了验证页（可能触发了风控），稍后再试或换一条链接",
                    detail=f"len={len(html)}",
                )
            raise MediaNotFound("没拿到笔记内容（可能已删除，或需要登录才能看）")

        note_id = str(note.get("noteId") or ref.media_id or "")
        user = note.get("user") or {}
        interact = note.get("interactInfo") or {}
        title = str(note.get("title") or "").strip()
        desc = str(note.get("desc") or "").strip()

        images, image_fallbacks = self._collect_images(note)
        videos = self._collect_videos(note)
        duration_ms = self._duration_ms(note)

        media = MediaInfo(
            platform=self.name,
            media_id=note_id or (ref.media_id or ""),
            page=1,
            source_url=ref.raw,
            web_url=page_url,
            title=title or desc[:40] or "小红书笔记",
            author=str(user.get("nickName") or user.get("nickname") or "").strip(),
            author_id=str(user.get("userId") or ""),
            author_avatar=_https(str(user.get("avatar") or "")),
            cover=images[0].url if images else "",
            duration=duration_ms // 1000,
            pubdate=_to_int(note.get("time")) // 1000,  # 页面给的是毫秒
            desc=desc,
            stats={
                "like": _to_int(interact.get("likedCount")),
                "reply": _to_int(interact.get("commentCount")),
                "favorite": _to_int(interact.get("collectedCount")),
                "share": _to_int(interact.get("shareCount")),
            },
            items=images + videos,
            extras={
                "note_type": str(note.get("type") or ""),
                "images": [item.url for item in images],
                # 与 images 同序的签名地址：无水印 CDN 取不到时下载侧退回它
                "image_fallbacks": image_fallbacks,
                "duration_ms": duration_ms,
                "qr_url": page_url,
                "tags": _extract_tags(desc),
            },
        )

        logger.info(
            f"{LOG_TAG} 小红书解析成功：{media.media_id} "
            f"《{media.title}》 图 {len(images)} 张 / 视频 {len(videos)} 个"
        )
        return media

    async def _follow(self, short_url: str) -> str:
        """短链跟随到内容页（保留 xsec_token 等查询参数，缺了会变成风控页）。"""
        try:
            async with self.session.get(
                short_url,
                headers=self._headers(html=True),
                timeout=aiohttp_timeout(self.timeout),
                allow_redirects=True,
            ) as resp:
                return str(resp.url)
        except Exception as exc:  # noqa: BLE001 - 统一转成平台异常
            raise PlatformError(f"小红书短链解析失败：{type(exc).__name__}") from exc

    async def _fetch_page(self, url: str) -> str:
        try:
            async with self.session.get(
                url,
                headers=self._headers(html=True),
                timeout=aiohttp_timeout(self.timeout),
                allow_redirects=True,
            ) as resp:
                if resp.status == 404:
                    raise MediaNotFound("小红书笔记不存在或已删除")
                if resp.status >= 400:
                    raise PlatformError(f"小红书返回 HTTP {resp.status}")
                return await resp.text(errors="replace")
        except PlatformError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PlatformError(f"小红书页面请求失败：{type(exc).__name__}") from exc

    # ── 字段提取 ────────────────────────────────────────────────────────

    @staticmethod
    def _collect_images(note: dict) -> tuple[list[MediaItem], list[str]]:
        """取图集。返回 (无水印原图项, 对应的签名地址兜底)。

        无水印原图靠 ``fileId`` 直连 ``IMAGE_CDN``：页面里给的
        ``...!h5_1080jpg`` 是**带水印的压缩版**（正中压「小红书」三个字），
        换成 ``{CDN}/{fileId}`` 就是原始大图、干净无水印。

        兜底列表与返回的 items 一一对应：CDN 万一取不到（个别老图 / 新图未同步），
        下载侧会退回签名地址，至少能出图——带水印总比裂图好。
        """
        items: list[MediaItem] = []
        fallbacks: list[str] = []
        for row in note.get("imageList") or []:
            if not isinstance(row, dict):
                continue

            # 先找签名地址（兜底用）
            signed = ""
            for info in row.get("infoList") or []:
                if isinstance(info, dict) and info.get("imageScene") == "H5_DTL":
                    signed = str(info.get("url") or "")
                    break
            signed = signed or str(row.get("url") or "")

            file_id = str(row.get("fileId") or "").strip().lstrip("/")
            url = f"{IMAGE_CDN}/{file_id}" if file_id else ""
            if not url:
                url = signed
            if not url:
                continue

            items.append(
                MediaItem(
                    kind="image",
                    url=_https(url),
                    referer="https://www.xiaohongshu.com/",
                    size_hint=0,
                )
            )
            fallbacks.append(_https(signed))
        return items, fallbacks

    @staticmethod
    def _collect_videos(note: dict) -> list[MediaItem]:
        """取视频直链。优先 h264（兼容性最好），没有再看 h265。"""
        video = note.get("video") or {}
        media = video.get("media") or {}
        streams = media.get("stream") or {}
        for codec in ("h264", "h265", "av1"):
            rows = streams.get(codec) or []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                url = _https(str(row.get("masterUrl") or ""))
                if not url:
                    continue
                return [
                    MediaItem(
                        kind="video",
                        url=url,
                        filename="",
                        referer="https://www.xiaohongshu.com/",
                        size_hint=_to_int(row.get("size")),
                        headers={
                            "User-Agent": UA_MOBILE,
                            "Referer": "https://www.xiaohongshu.com/",
                        },
                    )
                ]
        return []

    @staticmethod
    def _duration_ms(note: dict) -> int:
        video = note.get("video") or {}
        media = video.get("media") or {}
        streams = media.get("stream") or {}
        for codec in ("h264", "h265"):
            for row in streams.get(codec) or []:
                if isinstance(row, dict):
                    value = _to_int(row.get("duration"))
                    if value:
                        return value
        return 0

    def web_url(self, info: MediaInfo) -> str:
        return info.web_url or info.source_url


def _extract_tags(desc: str) -> list[str]:
    """笔记正文里的话题标签 ``#爱弥斯[话题]#`` → ``爱弥斯``。"""
    return [tag for tag in re.findall(r"#([^#\[\]\s]{1,24})(?:\[话题\])?#", desc or "") if tag]


def aiohttp_timeout(seconds: int):
    """延迟导入 aiohttp：这个模块在纯函数测试里也可能被导入。"""
    import aiohttp

    return aiohttp.ClientTimeout(total=max(5, int(seconds)))


__all__ = ["TRIGGER_PATTERN", "XiaohongshuPlatform"]
