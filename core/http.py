"""共享的 HTTP 请求头常量。

B 站 CDN（bilivideo.com）会校验 UA，实测结果（2026-09-13）：

| 请求方 | UA | Referer | 结果 |
|--------|----|---------|------|
| aiohttp 默认 UA | ``Python/3.x aiohttp/3.x`` | 无 | **403** |
| requests | 无 UA | 无 | **403** |
| aiohttp / requests | Chrome UA | 无 / bilibili / ebilibili | 200 |

因此**所有出网请求都必须显式带上浏览器 UA**，这里集中定义避免各模块各写一份。
"""

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

BASE_HEADERS = {
    "User-Agent": BROWSER_UA,
    "Accept-Language": "zh-CN,zh;q=0.9",
}

__all__ = ["BASE_HEADERS", "BROWSER_UA"]
