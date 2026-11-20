"""微信公众号文章平台。

实测（2026-11-20）：

- 文章页 ``mp.weixin.qq.com/s/<短码>`` 是**服务端渲染**的，直接 GET 就能拿到全文，
  不需要 Cookie、不需要签名；页面约 3~4MB（含大量内联样式与脚本）；
- 字段位置：
  - 标题 ``<h1 id="activity-name"><span class="js_title_inner">…</span></h1>``，
    兜底可读 ``var msg_title = "…"``；
  - 公众号名 ``id="js_name"``；
  - 发布时间只有 ``var ct = "1601027332"`` 这个时间戳，页面上显示的时间是 JS 渲染的；
  - 正文 ``<div id="js_content">…</div>``，里面是富文本，需要剥标签；
  - 图片**懒加载**：真地址在 ``data-src``，``src`` 是占位符，两者都要兼容；
  - 封面 ``<meta property="og:image">``。
- 失效页面靠文案判断：「该内容已被发布者删除」「此内容因违规无法查看」「环境异常」。
- 文章内的视频（``mpvideo.qpic.cn``）没有稳定直链，这里只处理图文。

Referer 必须带 ``https://mp.weixin.qq.com/``——微信图床有防盗链，不带会 403。
"""

from __future__ import annotations

import re

from ..media import (
    CAP_IMAGES,
    CAP_SUMMARY,
    KIND_IMAGE,
    MediaInfo,
    MediaItem,
    MediaRef,
)
from .base import BasePlatform, ParseHint
from .errors import MediaNotFound, PlatformError

LOG_TAG = "[bilibili-download]"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

REFERER = "https://mp.weixin.qq.com/"

TRIGGER_PATTERN = r"https?://mp\.weixin\.qq\.com/s[/?][0-9A-Za-z_\-=&%.:]+"

#: 短链：``/s/<短码>``；短码由 base62 组成，也可能带 ``-`` 和 ``_``
_SHORT_RE = re.compile(r"mp\.weixin\.qq\.com/s/([0-9A-Za-z_\-]+)", re.IGNORECASE)
#: 长链：``/s?__biz=…&mid=…&idx=…&sn=…``（老版本分享出来的是这种）
_LONG_RE = re.compile(r"mp\.weixin\.qq\.com/s\?([^\s\"'<>]+)", re.IGNORECASE)

#: 正文结尾：``</div>`` 后面跟 ``<script`` 或页面尾部脚本
_TITLE_RE = re.compile(
    r'<h1[^>]*id="activity-name"[^>]*>(.*?)</h1>', re.DOTALL
)
_TITLE_VAR_RE = re.compile(r'var\s+msg_title\s*=\s*[\'"](.*?)[\'"]', re.DOTALL)
_ACCOUNT_RE = re.compile(r'<a[^>]*id="js_name"[^>]*>(.*?)</a>', re.DOTALL)
_ACCOUNT_FALLBACK_RE = re.compile(r'id="js_name"[^>]*>(.*?)</', re.DOTALL)
_CT_RE = re.compile(r'var\s+ct\s*=\s*[\'"]?(\d{9,})')
_COVER_RE = re.compile(r'property="og:image"\s+content="([^"]+)"')
_DESC_RE = re.compile(r'property="og:description"\s+content="([^"]*)"')

_IMG_TAG_RE = re.compile(r"<img[^>]*>", re.IGNORECASE)
_ATTR_RE = re.compile(r'([a-zA-Z\-]+)\s*=\s*"([^"]*)"')
_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<\s*(?:br|/p|/section|/div)\s*/?\s*>", re.IGNORECASE)

#: 正文里要忽略的图片地址（表情、占位、图标）
_IMG_SKIP = ("data:image", "res.wx.qq.com", "mmbiz.qpic.cn/mmbiz_gif/", "wx_fmt=svg")

#: 失效页面的特征文案
_GONE_HINTS = (
    "该内容已被发布者删除",
    "此内容因违规无法查看",
    "此内容被多人投诉",
    "该公众号已迁移",
    "参数错误",
)
_BLOCK_HINTS = ("环境异常", "去验证", "完成验证后即可继续访问")


def _text_of(raw: str) -> str:
    """把一段 HTML 剥成纯文本（保留段落换行）。"""
    if not raw:
        return ""
    text = _BR_RE.sub("\n", raw)
    text = _TAG_RE.sub("", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def _extract_div(html: str, div_id: str) -> str:
    """按 ``id`` 取出一个 div 的内部 HTML，用深度配对而不是正则。

    正文里有嵌套 div，非贪婪正则会在第一个 ``</div>`` 就停住。
    """
    marker = f'id="{div_id}"'
    idx = html.find(marker)
    if idx < 0:
        return ""
    start = html.rfind("<div", 0, idx)
    if start < 0:
        return ""
    open_tag_end = html.find(">", idx)
    if open_tag_end < 0:
        return ""

    depth = 1
    pos = open_tag_end + 1
    length = len(html)
    while pos < length and depth > 0:
        nxt_open = html.find("<div", pos)
        nxt_close = html.find("</div", pos)
        if nxt_close < 0:
            return ""
        if 0 <= nxt_open < nxt_close:
            depth += 1
            pos = nxt_open + 4
        else:
            depth -= 1
            pos = nxt_close + 5
            if depth == 0:
                return html[open_tag_end + 1 : nxt_close]
    return html[open_tag_end + 1 : pos]


def _img_urls(body: str) -> list[str]:
    """从正文里挖图片地址。

    微信图床懒加载：真地址在 ``data-src``，``src`` 往往是 ``data:image/svg+xml``
    占位符；两者都扫，按出现顺序去重。
    """
    urls: list[str] = []
    seen: set[str] = set()
    for tag in _IMG_TAG_RE.findall(body):
        attrs = {k.lower(): v for k, v in _ATTR_RE.findall(tag)}
        for key in ("data-src", "src", "data-original", "data-backsrc"):
            url = (attrs.get(key) or "").strip()
            if not url or not url.startswith("http"):
                continue
            if any(skip in url for skip in _IMG_SKIP):
                continue
            if url in seen:
                continue
            seen.add(url)
            urls.append(url)
            break
    return urls


class WechatPlatform(BasePlatform):
    name = "wechat"
    display = "公众号"
    capabilities = frozenset({CAP_IMAGES, CAP_SUMMARY})
    trigger_pattern = TRIGGER_PATTERN

    @classmethod
    def match(cls, text: str) -> MediaRef | None:
        if not text:
            return None

        short = _SHORT_RE.search(text)
        if short:
            return MediaRef(
                platform=cls.name,
                raw=short.group(0),
                media_id=f"https://mp.weixin.qq.com/s/{short.group(1)}",
            )

        long = _LONG_RE.search(text)
        if long:
            query = long.group(1)
            # 用 mid + idx 当 id：同一篇文章的不同分享参数也能去重
            mid = re.search(r"mid=(\d+)", query)
            idx = re.search(r"idx=(\d+)", query)
            key = f"{mid.group(1)}_{idx.group(1)}" if mid else query[:32]
            return MediaRef(
                platform=cls.name,
                raw=long.group(0),
                media_id=f"https://mp.weixin.qq.com/s?{query}",
                extra={"key": key},
            )
        return None

    async def parse(self, ref: MediaRef, *, hint: ParseHint | None = None) -> MediaInfo:
        url = str(ref.media_id or "").strip()
        if not url.startswith("http"):
            raise MediaNotFound("没能从链接里识别出公众号文章地址")

        html = await self._fetch(url)
        if not html:
            raise PlatformError("公众号页面没有返回内容")

        if any(hint in html for hint in _BLOCK_HINTS):
            raise PlatformError("公众号这次返回了环境异常验证页，稍后重试")

        title = _text_of(self._first(_TITLE_RE, html) or self._first(_TITLE_VAR_RE, html))
        account = _text_of(self._first(_ACCOUNT_RE, html) or self._first(_ACCOUNT_FALLBACK_RE, html))
        cover = self._first(_COVER_RE, html)
        # og:image 给的是 http://mmbiz.qpic.cn/...，升成 https 更稳
        # （部分客户端/中间设备会拦 http 图，卡片封面就成了灰块）
        if cover.startswith("http://"):
            cover = "https://" + cover[len("http://") :]
        ct = self._first(_CT_RE, html)

        body = _extract_div(html, "js_content")
        text = _text_of(body)
        images = _img_urls(body) if body else []

        if not title and not text and not images:
            gone = next((hint for hint in _GONE_HINTS if hint in html), "")
            if gone:
                raise MediaNotFound(f"公众号提示「{gone}」")
            raise MediaNotFound("没拿到文章内容（可能是分享链接已失效，或需要登录才能看）")

        items: list[MediaItem] = [
            MediaItem(
                kind=KIND_IMAGE,
                url=image,
                referer=REFERER,
                headers={"User-Agent": UA},
            )
            for image in images[:9]
        ]

        desc = text
        if desc:
            # 正文前面常常重复一遍标题，去掉省得卡片上出现两遍
            if desc.startswith(title):
                desc = desc[len(title) :].lstrip(" \n｜|-—·:：")
        if len(desc) > 600:
            desc = desc[:600].rstrip() + "…"

        return MediaInfo(
            platform=self.name,
            media_id=str(ref.extra.get("key") or url),
            source_url=ref.raw,
            web_url=url,
            title=title or (text[:40] if text else "公众号文章"),
            author=account,
            cover=cover or (images[0] if images else ""),
            pubdate=int(ct) if ct and ct.isdigit() else 0,
            desc=desc,
            items=items,
            extras={
                "images": images,
                "account": account,
                "has_body": bool(text),
            },
            stats={},
        )

    async def _fetch(self, url: str) -> str:
        headers = {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": REFERER,
        }
        try:
            async with self.session.get(
                url,
                headers=headers,
                timeout=max(self.timeout, 30),
                allow_redirects=True,
            ) as resp:
                if resp.status >= 400:
                    raise PlatformError(f"公众号页面返回 HTTP {resp.status}")
                return await resp.text(errors="replace")
        except PlatformError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise PlatformError(f"公众号页面请求失败：{type(exc).__name__}: {exc}") from exc

    @staticmethod
    def _first(pattern: re.Pattern[str], html: str) -> str:
        matched = pattern.search(html)
        return matched.group(1).strip() if matched else ""
