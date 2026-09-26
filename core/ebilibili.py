"""ebilibili 下载站客户端（三路径逐级降级）。

站点形态（2026-09-13 实测）：
- A 直链接口：``GET  /api/playurl/{bvid}/{cid}`` → JSON，含 bilivideo.com 直链
- B 表单页：  ``POST /download``  字段 ``bvid``（接受完整链接/短链）→ HTML 内嵌 startDownload(...)
- C 解析页：  ``POST /downloads`` 字段 ``url`` → HTML 解析结果（支持多平台）

该站是个人小站、无 SLA，且实测对非浏览器请求偶发 ReadTimeout，
所以这里做了重试 + 指数退避，并由 ``resolve()`` 负责逐级降级。
直链带 deadline 参数（有有效期），调用方必须拿到后立刻下载。
"""

from __future__ import annotations

import asyncio
import html
import re
from collections.abc import Callable
from dataclasses import dataclass

import aiohttp

from astrbot.api import logger

from .http import BASE_HEADERS

BASE_URL = "https://www.ebilibili.com"

# 带上浏览器 UA 与站内 Referer 时实测成功率明显更高
EBILI_HEADERS = {
    **BASE_HEADERS,
    "Referer": f"{BASE_URL}/download",
}

# 表单页把直链写在 onclick="startDownload('<url>', '<filename>', '<bvid>')" 里
_START_DOWNLOAD_RE = re.compile(
    r"startDownload\(\s*'([^']+)'\s*,\s*'([^']*)'", re.IGNORECASE
)
# 解析页（/downloads）用 copyAndAlert('<url>') 提供下载链接
_COPY_ALERT_RE = re.compile(r"copyAndAlert\(\s*'([^']+)'", re.IGNORECASE)
# 兜底：直接在 HTML 里找视频直链
_RAW_MP4_RE = re.compile(r"https?://[^\"'\s<>]+?\.mp4[^\"'\s<>]*", re.IGNORECASE)
_TITLE_RE = re.compile(r"<p>\s*<strong>(.*?)</strong>\s*</p>", re.IGNORECASE | re.DOTALL)


class EbilibiliError(Exception):
    """ebilibili 站点不可用或返回内容无法解析。"""


@dataclass(slots=True)
class PlayUrl:
    """一次成功解析得到的下载信息。"""

    url: str
    filename: str
    title: str = ""
    source: str = "api"  # api | form | parser


class EbilibiliClient:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        timeout: int = 20,
        retries: int = 2,
        verbose: bool = False,
        on_attempt: Callable[[str, bool, str], None] | None = None,
    ) -> None:
        self._session = session
        self._timeout = max(5, timeout)
        self._retries = max(0, retries)
        self._verbose = verbose
        # 每条路径尝试后的上报回调，供 /bdlstatus 统计「哪条路径可用」
        self._on_attempt = on_attempt

    # ---------------------------------------------------------------- 请求层

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        data: dict | None = None,
    ) -> tuple[int, str]:
        """发起一次带重试的请求，返回 (状态码, 响应文本)。"""
        url = path if path.startswith("http") else BASE_URL + path
        last_error: Exception | None = None

        for attempt in range(self._retries + 1):
            started = asyncio.get_running_loop().time()
            try:
                async with self._session.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    headers=EBILI_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=self._timeout),
                ) as resp:
                    text = await resp.text(errors="replace")
                    if self._verbose:
                        cost = asyncio.get_running_loop().time() - started
                        logger.info(
                            f"[ebilibili] {method} {url} -> {resp.status} "
                            f"({len(text)} 字节, {cost:.1f}s)"
                        )
                    if resp.status >= 500:
                        raise EbilibiliError(f"HTTP {resp.status}")
                    return resp.status, text
            except (aiohttp.ClientError, TimeoutError, EbilibiliError) as exc:
                last_error = exc
                if attempt < self._retries:
                    # 指数退避：站点偶发把读取卡住，立刻重试通常还是失败
                    delay = 1.5 * (attempt + 1)
                    logger.info(
                        f"[ebilibili] {method} {url} 失败（{type(exc).__name__}: {exc}），"
                        f"{delay:.1f}s 后重试"
                    )
                    await asyncio.sleep(delay)

        raise EbilibiliError(f"请求失败：{type(last_error).__name__}: {last_error}")

    # ---------------------------------------------------------------- 三条路径

    async def get_by_api(self, bvid: str, cid: int) -> PlayUrl:
        """路径 A：直链接口（最干净，优先使用）。"""
        status, text = await self._request("GET", f"/api/playurl/{bvid}/{cid}")
        if status >= 400:
            raise EbilibiliError(f"直链接口返回 HTTP {status}")

        try:
            import json

            payload = json.loads(text)
        except ValueError as exc:
            raise EbilibiliError(f"直链接口返回非 JSON：{text[:120]}") from exc

        if payload.get("error"):
            raise EbilibiliError(str(payload["error"]))

        direct = ((payload.get("url") or {}) if isinstance(payload.get("url"), dict) else {})
        direct_url = direct.get("url") or ""
        if not direct_url:
            raise EbilibiliError("直链接口未返回 url 字段")

        return PlayUrl(
            url=direct_url,
            filename=str(payload.get("filename") or "").strip(),
            title=str(payload.get("title") or "").strip(),
            source="api",
        )

    async def get_by_form(self, link_or_id: str) -> PlayUrl:
        """路径 B：POST /download 表单（字段名是 bvid，但实际接受链接或 ID）。"""
        status, text = await self._request("POST", "/download", data={"bvid": link_or_id})
        if status >= 400:
            raise EbilibiliError(f"表单页返回 HTTP {status}")

        parsed = self._extract(text)
        if parsed is None:
            raise EbilibiliError("表单页未找到下载链接（视频可能不可下载）")

        url, filename = parsed
        title_match = _TITLE_RE.search(text)
        title = html.unescape(title_match.group(1)).strip() if title_match else ""
        return PlayUrl(url=url, filename=filename, title=title, source="form")

    async def get_by_parser(self, page_url: str) -> PlayUrl:
        """路径 C：POST /downloads 解析页。"""
        status, text = await self._request("POST", "/downloads", data={"url": page_url})
        if status >= 400:
            raise EbilibiliError(f"解析页返回 HTTP {status}")

        parsed = self._extract(text)
        if parsed is None:
            raise EbilibiliError("解析页未找到下载链接")

        url, filename = parsed
        title_match = _TITLE_RE.search(text)
        title = html.unescape(title_match.group(1)).strip() if title_match else ""
        return PlayUrl(url=url, filename=filename, title=title, source="parser")

    async def resolve(
        self,
        *,
        bvid: str,
        cid: int,
        original_link: str,
        need_title: bool = False,
    ) -> PlayUrl | None:
        """按 A → B → C 顺序尝试，全部失败返回 None。

        不做成"抛最后一次异常"是因为调用方一律会降级为发送网页链接，
        返回 None 更便于表达"这条路走不通"。

        构造时传入的 ``on_attempt(name, ok, detail)`` 会在每条路径尝试后回调，
        供调用方统计路径可用性；回调本身不抛异常。
        """
        attempts = (
            ("直链接口", lambda: self.get_by_api(bvid, cid)),
            ("表单页", lambda: self.get_by_form(original_link or bvid)),
            ("解析页", lambda: self.get_by_parser(original_link or f"{BASE_URL}/video/{bvid}")),
        )
        report = self._on_attempt or (lambda *_: None)

        for name, coro_factory in attempts:
            try:
                play = await coro_factory()
            except EbilibiliError as exc:
                logger.info(f"[ebilibili] {name} 解析失败：{exc}")
                report(name, False, str(exc))
                continue
            except Exception as exc:  # noqa: BLE001 - 兜底，任何异常都只降级不崩溃
                logger.warning(f"[ebilibili] {name} 出现未预期异常：{type(exc).__name__}: {exc}")
                report(name, False, f"{type(exc).__name__}: {exc}")
                continue

            if not play.filename:
                play.filename = f"{bvid}.mp4"
            logger.info(f"[ebilibili] {name} 解析成功（{play.filename}）")
            report(name, True, play.source)
            return play

        logger.warning("[ebilibili] 三条路径全部失败")
        return None

    # ---------------------------------------------------------------- 工具

    @staticmethod
    def _extract(text: str) -> tuple[str, str] | None:
        """从 HTML 中提取 (直链, 文件名)。

        直链里的 ``&amp;`` 必须还原成 ``&``，否则 CDN 会因为参数被截断返回 403。
        """
        match = _START_DOWNLOAD_RE.search(text)
        if match:
            return html.unescape(match.group(1)), html.unescape(match.group(2))

        match = _COPY_ALERT_RE.search(text)
        if match:
            return html.unescape(match.group(1)), ""

        match = _RAW_MP4_RE.search(text)
        if match:
            return html.unescape(match.group(0)), ""

        return None

    @staticmethod
    def web_url(bvid: str) -> str:
        """域名替换式的网页下载地址，作为最终兜底发给用户。"""
        return f"{BASE_URL}/video/{bvid}"


__all__ = ["BASE_URL", "EbilibiliClient", "EbilibiliError", "PlayUrl"]
