"""B 站字幕获取——L3 总结的输入源。

流程：nav 取 WBI keys → 签名请求 ``/x/player/wbi/v2`` → 从
``data.subtitle.subtitles[]`` 选一条 → 下载 ``subtitle_url`` 指向的 JSON →
按时间窗合并成带 ``[mm:ss]`` 前缀的文本。

**前提：需要 SESSDATA。** DESIGN.md 4.2 实测过：未携带登录态时 ``subtitles``
恒为空数组。因此这里把「没有字幕」当作**正常结果**返回 None，由调用方回退到
元数据总结（L1/L2），不报错、不打扰用户。

设计约束：
- 不向上抛异常：所有失败路径统一返回 None——字幕是增强项，不能影响主流程；
- 凭据只进请求头，**绝不写日志**（日志里只出现 code 与语言标签）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import aiohttp

from astrbot.api import logger

from .http import BASE_HEADERS
from .wbi import key_from_url, sign

NAV_API = "https://api.bilibili.com/x/web-interface/nav"
PLAYER_WBI_API = "https://api.bilibili.com/x/player/wbi/v2"

# 时间窗：把字幕按 60 秒合并成一段，带上起始时间戳
DEFAULT_WINDOW_SECONDS = 60.0

# 优先选择的中文字幕语言标签前缀
_ZH_PREFIXES = ("zh", "ai-zh")


@dataclass(slots=True)
class SubtitleCue:
    """一条字幕。"""

    start: float
    end: float
    text: str


@dataclass(slots=True)
class SubtitleResult:
    """一次成功取到的字幕。"""

    lan: str
    lan_doc: str
    cues: list[SubtitleCue] = field(default_factory=list)
    text: str = ""


def _headers(sessdata: str) -> dict[str, str]:
    """B 站接口需要浏览器 UA + Referer；凭据只在这里出现。"""
    return {
        **BASE_HEADERS,
        "Referer": "https://www.bilibili.com/",
        "Cookie": f"SESSDATA={sessdata}",
    }


def _pick_subtitle(subtitles: object) -> dict | None:
    """优先中文字幕，其次任意一条；没有可用项返回 None。"""
    if not isinstance(subtitles, list) or not subtitles:
        return None

    for item in subtitles:
        if not isinstance(item, dict) or not item.get("subtitle_url"):
            continue
        lan = str(item.get("lan") or "")
        if lan.startswith(_ZH_PREFIXES):
            return item

    for item in subtitles:
        if isinstance(item, dict) and item.get("subtitle_url"):
            return item
    return None


def _mmss(seconds: float) -> str:
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def build_subtitle_text(
    cues: list[SubtitleCue],
    *,
    max_chars: int = 6000,
    window: float = DEFAULT_WINDOW_SECONDS,
) -> str:
    """把字幕按时间窗合并成带 ``[mm:ss]`` 前缀的段落，并按**段**截断。

    按段截断而不是按字符截断：宁可少给模型一段，也不要把一句话砍成两半。
    至少保留一段（即使它本身就超限）。
    """
    if not cues:
        return ""

    blocks: list[str] = []
    bucket: list[str] = []
    bucket_start = cues[0].start

    for cue in cues:
        if bucket and cue.start - bucket_start >= window:
            blocks.append(f"[{_mmss(bucket_start)}] " + "".join(bucket))
            bucket = []
            bucket_start = cue.start
        bucket.append(cue.text)

    if bucket:
        blocks.append(f"[{_mmss(bucket_start)}] " + "".join(bucket))

    picked: list[str] = []
    total = 0
    truncated = False
    for block in blocks:
        if max_chars and picked and total + len(block) > max_chars:
            truncated = True
            break
        picked.append(block)
        total += len(block) + 1

    text = "\n".join(picked)
    if truncated:
        text += "\n…（字幕过长，后续部分已省略）"
    return text


async def fetch_wbi_keys(
    session: aiohttp.ClientSession, *, sessdata: str, timeout: int
) -> tuple[str, str] | None:
    """取 WBI 签名所需的 img_key / sub_key。

    注意：nav 接口在未登录时返回 ``code=-101``，所以**不能**套用项目里
    「code != 0 就抛错」的通用解析，这里直接读 ``data.wbi_img``。
    """
    try:
        async with session.get(
            NAV_API,
            headers=_headers(sessdata),
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status >= 400:
                logger.debug(f"WBI keys 获取失败：HTTP {resp.status}")
                return None
            payload = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001 - 取不到就降级
        logger.debug(f"WBI keys 获取异常：{type(exc).__name__}: {exc}")
        return None

    data = (payload or {}).get("data") or {}
    wbi_img = data.get("wbi_img") or {}
    img_key = key_from_url(wbi_img.get("img_url"))
    sub_key = key_from_url(wbi_img.get("sub_url"))
    if not img_key or not sub_key:
        logger.debug("WBI keys 解析失败：nav 未返回 wbi_img（可能未登录）")
        return None
    return img_key, sub_key


async def _download_cues(
    session: aiohttp.ClientSession, url: str | None, *, sessdata: str, timeout: int
) -> list[SubtitleCue]:
    """下载字幕 JSON 并转成 SubtitleCue 列表。"""
    if not url:
        return []
    if url.startswith("//"):
        url = f"https:{url}"

    try:
        async with session.get(
            url,
            headers=_headers(sessdata),
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status >= 400:
                logger.debug(f"字幕文件下载失败：HTTP {resp.status}")
                return []
            payload = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"字幕文件下载异常：{type(exc).__name__}: {exc}")
        return []

    body = (payload or {}).get("body")
    if not isinstance(body, list):
        return []

    cues: list[SubtitleCue] = []
    for item in body:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("content") or "").split())
        if not text:
            continue
        try:
            start = float(item.get("from") or 0)
            end = float(item.get("to") or 0)
        except (TypeError, ValueError):
            continue
        cues.append(SubtitleCue(start=start, end=end, text=text))
    return cues


async def fetch_subtitle(
    session: aiohttp.ClientSession,
    *,
    bvid: str,
    cid: int,
    sessdata: str,
    img_key: str,
    sub_key: str,
    timeout: int,
    max_chars: int,
) -> SubtitleResult | None:
    """取字幕并合并成文本；未配置凭据、无字幕或任何失败都返回 None。"""
    if not (bvid and cid and sessdata and img_key and sub_key):
        return None

    params = sign({"bvid": bvid, "cid": cid}, img_key, sub_key)
    try:
        async with session.get(
            PLAYER_WBI_API,
            params=params,
            headers=_headers(sessdata),
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status >= 400:
                logger.debug(f"字幕接口返回 HTTP {resp.status}")
                return None
            payload = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"字幕接口请求异常：{type(exc).__name__}: {exc}")
        return None

    code = (payload or {}).get("code")
    if code != 0:
        # 常见：-101 登录态失效 / -403 签名不匹配。都只降级，不刷 error 噪音
        logger.info(f"字幕接口返回 code={code}，改用元数据总结")
        return None

    subtitles = (((payload.get("data") or {}).get("subtitle") or {}).get("subtitles")) or []
    picked = _pick_subtitle(subtitles)
    if picked is None:
        return None

    cues = await _download_cues(
        session, picked.get("subtitle_url"), sessdata=sessdata, timeout=timeout
    )
    if not cues:
        return None

    text = build_subtitle_text(cues, max_chars=max_chars)
    if not text:
        return None

    return SubtitleResult(
        lan=str(picked.get("lan") or ""),
        lan_doc=str(picked.get("lan_doc") or ""),
        cues=cues,
        text=text,
    )


__all__ = [
    "DEFAULT_WINDOW_SECONDS",
    "SubtitleCue",
    "SubtitleResult",
    "build_subtitle_text",
    "fetch_subtitle",
    "fetch_wbi_keys",
]
