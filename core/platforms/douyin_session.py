"""抖音分享页会话：cookie 保持 + 重试 + WAF 挑战处理。

为什么需要单独一个模块：

- 抖音的 ``www.iesdouyin.com/share/<type>/<id>/`` 只在请求**带有效 ttwid** 时才把
  作品数据放进 ``window._ROUTER_DATA``；不带 cookie 的冷请求只给页面上下文
  （ua / itemId 之类），没有 ``videoInfoRes`` / ``slidesInfoRes``；
- 冷请求会在 ``Set-Cookie`` 里下发 ttwid，带着它再请求才有数据；
- 缓存的 ttwid 失效时，**带着失效 cookie 服务端不会重新下发**，必须清空重来；
- 请求过密会返回 ``waf_js`` 挑战页，页面里的 ``cs`` 是个 base64 JSON，
  需要做一次 sha256 PoW 才能拿到 ``_wafchallengeid``；
- 连发太多次会把出口 IP 打进 ``__ac_nonce`` 限流页，之后怎么重试都没用，
  所以每条作品的请求预算要卡死。

这套判断来自 ``astrbot_plugin_denia_share`` 的实测（2026-09）：
全新 client 不带 cookie 单发 6/6 失败；冷请求拿 ttwid 后再带上去 4/4 成功。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
from http.cookies import SimpleCookie
from typing import Any

import aiohttp
from astrbot.api import logger

LOG_TAG = "[bilibili-download]"

_ROUTER_RE = re.compile(r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", re.DOTALL)
_CHALLENGE_RE = re.compile(r'cs="([A-Za-z0-9+/=]+)"')

#: 承载作品数据的字段名：视频页是 videoInfoRes，图文页是 slidesInfoRes / noteDetailRes
WORK_KEYS = ("videoInfoRes", "slidesInfoRes", "noteDetailRes")

#: 单条作品的总请求预算（跨 URL 变体共享）。连发太多会被 IP 级限流。
MAX_REQUESTS_PER_WORK = 12

#: 单次 fetch 的最大尝试次数
MAX_ATTEMPTS = 6

#: PoW 最多试多少个数字
MAX_POW_ITER = 1_000_000

_WAF_MARK = "waf_js"


def _b64d(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4))


def _b64e(value: bytes) -> str:
    return base64.b64encode(value).decode()


def router_data(text: str) -> dict[str, Any] | None:
    matched = _ROUTER_RE.search(text or "")
    if not matched:
        return None
    try:
        data = json.loads(matched.group(1).strip())
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def has_work_data(text: str) -> bool:
    """``_ROUTER_DATA`` 里是否真的带了作品数据（而不是只有页面上下文）。"""
    data = router_data(text)
    if data is None:
        return False
    for value in (data.get("loaderData") or {}).values():
        if not isinstance(value, dict):
            continue
        for key in WORK_KEYS:
            info = value.get(key)
            if isinstance(info, dict) and info.get("item_list"):
                return True
    return False


def looks_like_challenge(text: str) -> bool:
    return _WAF_MARK in (text or "")


def solve_challenge(text: str) -> str | None:
    """解开 ``waf_js`` 的 PoW，返回 ``_wafchallengeid``；解不开返回 None。

    挑战页里的 ``cs`` 是 base64 JSON：``{"v": {"a": <prefix 原始字节>, "c": <目标 sha256>}}``，
    找最小的 i 使 ``sha256(prefix + str(i)) == target``（实测 i 通常是个位数）。
    """
    matched = _CHALLENGE_RE.search(text or "")
    if not matched:
        return None
    try:
        payload = json.loads(_b64d(matched.group(1)))
        prefix = _b64d(payload["v"]["a"])
        target = _b64d(payload["v"]["c"]).hex()
    except Exception:  # noqa: BLE001 - 结构变了就当解不开
        return None

    for index in range(MAX_POW_ITER):
        if hashlib.sha256(prefix + str(index).encode()).hexdigest() == target:
            payload["d"] = _b64e(str(index).encode())
            return _b64e(json.dumps(payload, separators=(",", ":")).encode())
    return None


class DouyinShareSession:
    """跨请求保持 cookie 的分享页会话。"""

    def __init__(self, timeout: int = 20) -> None:
        self.timeout = max(5, int(timeout))
        self._cookies: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._requests_left = MAX_REQUESTS_PER_WORK

    def start_work(self) -> None:
        """开始解析一条新作品：重置请求预算。"""
        self._requests_left = MAX_REQUESTS_PER_WORK

    def cookie_header(self) -> dict[str, str]:
        if not self._cookies:
            return {}
        joined = "; ".join(f"{name}={value}" for name, value in self._cookies.items())
        return {"Cookie": joined}

    def _spend_request(self) -> bool:
        if self._requests_left <= 0:
            return False
        self._requests_left -= 1
        return True

    def _remember(self, response: aiohttp.ClientResponse) -> None:
        for header in response.headers.getall("Set-Cookie", []):
            jar = SimpleCookie()
            try:
                jar.load(header)
            except Exception:  # noqa: BLE001
                continue
            for name, morsel in jar.items():
                if morsel.value:
                    self._cookies[name] = morsel.value

    async def fetch(
        self, http: aiohttp.ClientSession, url: str, headers: dict[str, str]
    ) -> str:
        """抓分享页，保持 cookie，并重试到真的拿到作品数据。"""
        async with self._lock:
            text = await self._fetch_with_retries(http, url, headers)
            if has_work_data(text) or not self._cookies:
                return text

            # 带着 cookie 还是没数据：多半是缓存的 ttwid 失效。
            # 带着失效 cookie 服务端不会重新下发，必须清空后冷请求一次。
            self._cookies.clear()
            logger.debug(f"{LOG_TAG} 抖音带 cookie 仍无数据，清空会话冷请求一次")
            return await self._fetch_with_retries(http, url, headers)

    async def _fetch_with_retries(
        self, http: aiohttp.ClientSession, url: str, headers: dict[str, str]
    ) -> str:
        text = ""
        solved = False
        for attempt in range(MAX_ATTEMPTS):
            if not self._spend_request():
                logger.warning(
                    f"{LOG_TAG} 抖音本条作品的请求预算已用尽"
                    f"（{MAX_REQUESTS_PER_WORK} 次），停止重试"
                )
                return text
            try:
                async with http.get(
                    url,
                    headers={**headers, **self.cookie_header()},
                    timeout=aiohttp.ClientTimeout(total=self.timeout),
                    allow_redirects=True,
                ) as resp:
                    self._remember(resp)
                    text = await resp.text(errors="replace")
                    if resp.status >= 400 and not looks_like_challenge(text):
                        logger.warning(f"{LOG_TAG} 抖音分享页返回 HTTP {resp.status}")
            except Exception as exc:  # noqa: BLE001 - 网络异常继续重试
                logger.debug(f"{LOG_TAG} 抖音分享页请求异常：{type(exc).__name__}: {exc}")
                await asyncio.sleep(self._backoff(attempt))
                continue

            if has_work_data(text):
                return text

            # 挑战只解一次：解完还拿不到数据说明不是挑战的问题（多半是限流）
            if not solved and looks_like_challenge(text):
                solved = True
                challenge = await asyncio.to_thread(solve_challenge, text)
                if challenge:
                    self._cookies["_wafchallengeid"] = challenge
                else:
                    return text

            await asyncio.sleep(self._backoff(attempt))
        return text

    @staticmethod
    def _backoff(attempt: int) -> float:
        return 0.3 + 0.2 * attempt


#: 按超时配置缓存会话，让 cookie 跨消息存活
_SESSIONS: dict[int, DouyinShareSession] = {}


def get_share_session(timeout: int = 20) -> DouyinShareSession:
    session = _SESSIONS.get(timeout)
    if session is None:
        session = DouyinShareSession(timeout)
        _SESSIONS[timeout] = session
    return session


__all__ = [
    "DouyinShareSession",
    "get_share_session",
    "has_work_data",
    "looks_like_challenge",
    "router_data",
    "solve_challenge",
]
