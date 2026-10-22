"""B 站设备指纹 cookie（buvid3）的获取与缓存。

为什么需要它：B 站的接口风控（HTTP 412）对「一个 cookie 都不带的裸请求」最敏感。
真实浏览器访问 B 站时一定带着 ``buvid3``（B 站自己下发的设备指纹），而服务端 IP
（尤其云主机）本来就更容易被判成脚本。补上这个 cookie 让请求形态更接近浏览器，
是缓解 412 成本最低的一步，且**不需要用户做任何配置**。

获取方式（公开接口，**无需登录**）：

    GET https://api.bilibili.com/x/frontend/finger/spi  →  data.b_3

失败（网络异常、风控、结构变化）时返回空串，调用方按「没有 cookie」继续请求——
绝不能因为拿不到指纹就中断解析。
"""

from __future__ import annotations

import asyncio

import aiohttp

from astrbot.api import logger

from .http import BASE_HEADERS

FINGER_API = "https://api.bilibili.com/x/frontend/finger/spi"

_FINGER_HEADERS = {
    **BASE_HEADERS,
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
}

_cached: str = ""
_lock = asyncio.Lock()


async def get_buvid3(session: aiohttp.ClientSession, *, timeout: int = 10) -> str:
    """取 buvid3，进程内缓存；取不到时返回空串。

    加锁是为了避免并发解析时多个协程同时去打这个接口（既浪费一次请求，
    也更容易触发风控）。
    """
    global _cached
    if _cached:
        return _cached

    async with _lock:
        if _cached:  # 等锁期间可能已被别的协程填好
            return _cached
        _cached = await _fetch(session, timeout=timeout)
        return _cached


async def _fetch(session: aiohttp.ClientSession, *, timeout: int) -> str:
    try:
        async with session.get(
            FINGER_API,
            headers=_FINGER_HEADERS,
            timeout=aiohttp.ClientTimeout(total=max(3, timeout)),
        ) as resp:
            if resp.status >= 400:
                logger.debug(f"buvid3 获取失败：HTTP {resp.status}")
                return ""
            payload = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001 - 指纹拿不到不影响主流程
        logger.debug(f"buvid3 获取异常（忽略）：{type(exc).__name__}: {exc}")
        return ""

    data = payload.get("data") or {}
    buvid = str(data.get("b_3") or "").strip()
    if buvid:
        logger.debug("已获取 buvid3，后续 B 站接口请求会带上该 cookie")
    return buvid


def reset() -> None:
    """清空缓存（测试用）。buvid3 是设备级指纹，重新获取通常仍是同一个值。"""
    global _cached
    _cached = ""


__all__ = ["FINGER_API", "get_buvid3", "reset"]
