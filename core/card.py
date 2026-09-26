"""视频信息卡片渲染：把解析结果渲染成一张图片（走 AstrBot 原生文转图）。

为什么用 AstrBot 原生能力、而不是自绘：

- ``Star`` 自带 ``html_render``（HTML + Jinja2 + CSS，Playwright 截图）与
  ``text_to_image``，**零新增依赖、零仓库体积**，字体由 AstrBot 渲染侧解决；
- 两个参考项目都选择自研渲染（biliVideo: wkhtml + Pillow 三级链；
  astrbot_plugin_parser: PIL 卡片），代价是 4 个新依赖与 3.4~3.7MB 内建字体。
  本插件的卡片复杂度（标题 + 封面 + 一段总结）用不上那套。

三级降级——任何一步失败都不抛出，由调用方回退到现有的纯文本卡片：

    1) html_render → 2) text_to_image → 3) None（纯文本）

封面与头像会让 Playwright 直接去取时大概率失败（B 站图片 CDN 校验 UA/Referer），
所以渲染前先下载并转成 data URI 内嵌进 HTML，彻底规避防盗链与网络抖动。
"""

from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import logger

from .bili_api import ReplyInfo, VideoInfo
from .constants import VERSION
from .formatting import format_count, format_duration, humanize_age
from .http import BASE_HEADERS
from .qrcode import qr_svg

# 模板默认宽度（模板内用 max-width 生效，视口更宽时卡片居中）
DEFAULT_CARD_WIDTH = 760

# 内嵌图片约束：单张上限避免把 HTML 撑到几十 MB；超时短一些，失败就用占位样式
INLINE_IMAGE_MAX_BYTES = 4 * 1024 * 1024
INLINE_IMAGE_TIMEOUT = 10

# B 站图片 CDN 与视频 CDN 一样会校验 UA（实测 aiohttp 默认 UA 会被拒）
IMAGE_HEADERS = {
    **BASE_HEADERS,
    "Referer": "https://www.bilibili.com/",
    "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*",
}

_TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "video_card.html"
_REPLIES_TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent / "templates" / "replies_card.html"
)
_template_cache: str | None = None
_replies_template_cache: str | None = None


def template_path() -> Path:
    """卡片模板的实际路径（用于加载时的存在性检查与日志展示）。"""
    return _TEMPLATE_PATH


def template_available() -> bool:
    """模板文件是否存在。

    在插件加载时先查一次，比等用户发链接才发现模板缺失好排查得多；
    缺失时卡片会静默降级为纯文本（见 render_card 的三级降级）。
    """
    return _TEMPLATE_PATH.is_file()


def load_template() -> str:
    """读取并缓存 Jinja2 模板。

    文件缺失时直接抛出（由 ``render_card`` 捕获并降级），
    因为「模板丢了」属于部署问题，不该静默变成一张空白卡片。
    """
    global _template_cache
    if _template_cache is None:
        _template_cache = _TEMPLATE_PATH.read_text(encoding="utf-8")
    return _template_cache


def format_pubdate(timestamp: int | None) -> str:
    """B 站 ``pubdate`` 是 Unix 秒；缺失或非法时返回空串（模板会整行跳过）。"""
    try:
        seconds = int(timestamp or 0)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(seconds))


async def inline_image(session: aiohttp.ClientSession, url: str | None) -> str | None:
    """把远程图片转成 data URI；失败、空内容或超限时返回 None。

    返回 None 是正常路径——模板会用灰底占位块顶上，不影响卡片其余部分。
    """
    if not url:
        return None
    if url.startswith("data:"):
        return url
    if not url.startswith("http"):
        return None

    try:
        async with session.get(
            url,
            headers=IMAGE_HEADERS,
            timeout=aiohttp.ClientTimeout(total=INLINE_IMAGE_TIMEOUT),
        ) as resp:
            if resp.status >= 400:
                logger.debug(f"卡片图片内嵌失败：HTTP {resp.status}")
                return None
            content_type = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
            if not content_type.startswith("image/"):
                logger.debug(f"卡片图片内嵌失败：Content-Type={content_type}")
                return None
            data = await resp.read()
    except Exception as exc:  # noqa: BLE001 - 内嵌失败不影响主流程
        logger.debug(f"卡片图片内嵌异常（忽略）：{type(exc).__name__}: {exc}")
        return None

    if not data:
        return None
    if len(data) > INLINE_IMAGE_MAX_BYTES:
        logger.debug(f"卡片图片过大（{len(data)} 字节），跳过内嵌")
        return None

    return f"data:{content_type};base64,{base64.b64encode(data).decode()}"


async def _build_reply_items(
    session: aiohttp.ClientSession,
    replies: list[ReplyInfo],
    limit: int,
    *,
    owner_mid: int = 0,
) -> list[dict]:
    """取前 ``limit`` 条评论，并把头像并行内嵌成 data URI。

    头像和评论附图都受 B 站图片 CDN 的 UA/Referer 校验，必须内嵌；
    ``inline_image`` 自己吞异常，所以单张失败只会退化成占位。

    这里顺手把模板要用的展示字段算好（等级配色 class、时间文案、是否 UP 主），
    避免把判断逻辑塞进 Jinja2。
    """
    picked = replies[:limit]
    if not picked:
        return []

    # 头像与评论附图一起并行内嵌：串行会让多图评论的渲染时间线性增长
    avatars, pictures = await asyncio.gather(
        asyncio.gather(*(inline_image(session, reply.avatar) for reply in picked)),
        asyncio.gather(*(inline_image(session, reply.picture) for reply in picked)),
    )

    items: list[dict] = []
    for reply, avatar, picture in zip(picked, avatars, pictures):
        # B 站等级徽章按等级配色，最高 6 级；越界值收敛到 6，避免没有对应样式
        level = max(0, min(int(reply.level or 0), 6))
        items.append(
            {
                "author": reply.author,
                "avatar": avatar or "",
                "picture": picture or "",
                "level": level,
                "level_class": f"lv{level}",
                "content": reply.content,
                "like": reply.like,
                "time_text": humanize_age(reply.ctime) if reply.ctime else "",
                "reply_count": reply.reply_count,
                "is_up": bool(owner_mid) and reply.mid == owner_mid,
                "medal_name": getattr(reply, "medal_name", "") or "",
                "medal_level": int(getattr(reply, "medal_level", 0) or 0),
                "dress_no": getattr(reply, "dress_no", "") or "",
            }
        )
    return items


def build_context(
    info: VideoInfo,
    *,
    summary: str | None,
    summary_basis: str | None,
    cover_uri: str | None,
    face_uri: str | None,
    card_width: int = DEFAULT_CARD_WIDTH,
) -> dict:
    """把 ``VideoInfo`` 映射成模板变量。

    用 ``getattr`` 兜底：``pubdate`` / ``owner_face`` 是为卡片新增的字段，
    旧版 ``VideoInfo`` 没有它们时卡片仍可渲染，只是少一行时间、头像走占位。
    """
    return {
        "card_width": card_width,
        "owner": (getattr(info, "owner", "") or "未知 UP 主").strip(),
        "owner_face": face_uri or "",
        "pubdate_text": format_pubdate(getattr(info, "pubdate", None)),
        "title": (getattr(info, "title", "") or "B站视频").strip(),
        "cover": cover_uri or "",
        "summary": (summary or "").strip(),
        "summary_basis": (summary_basis or "").strip(),
    }


def plain_text(info: VideoInfo, *, summary: str | None, summary_basis: str | None) -> str:
    """``text_to_image`` 降级用的纯文本内容。"""
    title = (getattr(info, "title", "") or "B站视频").strip()
    owner = (getattr(info, "owner", "") or "未知 UP 主").strip()
    lines = [f"📺 {title}", f"👤 {owner}"]
    if summary:
        basis = f"（{summary_basis}）" if summary_basis else ""
        lines += ["", f"AI总结：{summary}{basis}"]
    return "\n".join(lines)


async def render_card(
    star: Any,
    session: aiohttp.ClientSession,
    *,
    info: VideoInfo,
    summary: str | None,
    summary_basis: str | None = None,
    card_width: int = DEFAULT_CARD_WIDTH,
    timeout: int = 20,
) -> str | None:
    """渲染信息卡片，返回图片 URL（或本地路径）；全部路径失败时返回 None。

    ``star`` 为插件实例（``Star`` 子类），需要具备 ``html_render`` / ``text_to_image``。

    调用方约定：拿到 ``None`` 就回退现有的纯文本卡片——**卡片只是锦上添花，
    任何失败都不能让会话无响应、更不能把异常发进群里**。
    """
    # ── 1) HTML 模板 → 图片（首选，表现力最好）
    html_render = getattr(star, "html_render", None)
    if callable(html_render):
        try:
            cover_uri, face_uri = await asyncio.gather(
                inline_image(session, getattr(info, "cover", None)),
                inline_image(session, getattr(info, "owner_face", None)),
            )
            context = build_context(
                info,
                summary=summary,
                summary_basis=summary_basis,
                cover_uri=cover_uri,
                face_uri=face_uri,
                card_width=card_width,
            )
            result = await html_render(
                load_template(),
                context,
                options={
                    "full_page": True,
                    "type": "jpeg",
                    "quality": 90,
                    # Playwright 的 timeout 单位是毫秒：直接把秒数传过去（如 20）
                    # 会被当成 20ms，截图几乎必然失败。
                    "timeout": timeout * 1000,
                    # 视口宽度决定**截图宽度**。不传时 t2i 默认 800px，
                    # 卡片比它窄，两侧就会留下白边（就是之前看到的「留白」）。
                    "viewport_width": card_width,
                },
            )
            if result:
                return str(result)
            logger.warning("卡片 html_render 返回空结果，尝试 text_to_image 降级")
        except Exception as exc:  # noqa: BLE001 - 含模板缺失、t2i 服务不可用等
            logger.warning(
                f"卡片 html_render 失败，尝试 text_to_image 降级：{type(exc).__name__}: {exc}"
            )

    # ── 2) 纯文本转图（降级）
    text_to_image = getattr(star, "text_to_image", None)
    if callable(text_to_image):
        try:
            result = await text_to_image(
                plain_text(info, summary=summary, summary_basis=summary_basis)
            )
            if result:
                return str(result)
            logger.warning("卡片 text_to_image 返回空结果，回退纯文本卡片")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"卡片 text_to_image 失败，回退纯文本卡片：{type(exc).__name__}: {exc}"
            )

    # ── 3) 交给调用方回退纯文本
    return None


def load_replies_template() -> str:
    """读取并缓存热评卡片模板。"""
    global _replies_template_cache
    if _replies_template_cache is None:
        _replies_template_cache = _REPLIES_TEMPLATE_PATH.read_text(encoding="utf-8")
    return _replies_template_cache


def replies_template_available() -> bool:
    """热评卡片模板是否存在（在插件加载时先查一次，便于早发现问题）。"""
    return _REPLIES_TEMPLATE_PATH.is_file()


def _meta_items(info: VideoInfo) -> list[dict]:
    """热评图顶部信息区的「标签 / 值」列表（对齐参考图的排布）。

    只用 ``view`` 接口里已有的字段，**不额外发请求**。参考图里还有「大小 / 画质」
    两个格子，但它们要额外调 ``playurl``，这里不做。
    """
    items: list[dict] = []

    width = int(getattr(info, "width", 0) or 0)
    height = int(getattr(info, "height", 0) or 0)
    if width and height:
        items.append({"label": "分辨率", "value": f"{width} x {height}"})

    tname = (getattr(info, "tname", "") or "").strip()
    if tname:
        items.append({"label": "类型", "value": tname})

    reply_count = int(getattr(info, "reply", 0) or 0)
    if reply_count:
        items.append({"label": "评论", "value": f"{format_count(reply_count)} 条"})

    duration = int(getattr(info, "duration", 0) or 0)
    if duration:
        items.append({"label": "时长", "value": format_duration(duration)})

    return items


def _qr_markup(info: VideoInfo, page: int) -> str:
    """视频链接的二维码 SVG；拿不到链接或生成失败时返回空串（模板整块跳过）。"""
    bvid = (getattr(info, "bvid", "") or "").strip()
    if not bvid:
        return ""
    url = f"https://www.bilibili.com/video/{bvid}"
    if page > 1:
        url += f"?p={page}"
    try:
        return qr_svg(url)
    except Exception as exc:  # noqa: BLE001 - 二维码只是装饰，不该影响整张卡片
        logger.debug(f"二维码生成失败（跳过）：{type(exc).__name__}: {exc}")
        return ""


async def render_replies_card(
    star: Any,
    session: aiohttp.ClientSession,
    *,
    info: VideoInfo,
    replies: list[ReplyInfo],
    show_count: int,
    card_width: int = DEFAULT_CARD_WIDTH,
    timeout: int = 20,
    page: int = 1,
    with_qrcode: bool = True,
) -> str | None:
    """把热门评论渲染成**一张独立图片**（参考 B 站评论区的观感）。

    与信息卡片分开成图的原因：热评条数一多会把信息卡片拉得很长，
    单独成图既保持信息卡片紧凑，也让评论区排版更接近原生客户端。

    失败一律返回 None（不发热评图即可）——评论是锦上添花，不参与文本降级。
    """
    if show_count <= 0 or not replies:
        return None

    html_render = getattr(star, "html_render", None)
    if not callable(html_render):
        return None

    try:
        items = await _build_reply_items(
            session,
            replies,
            show_count,
            owner_mid=getattr(info, "owner_mid", 0) or 0,
        )
        if not items:
            return None

        context = {
            "card_width": card_width,
            "video_title": (getattr(info, "title", "") or "").strip(),
            "plugin_version": VERSION,
            "meta_items": _meta_items(info),
            "qrcode": _qr_markup(info, page) if with_qrcode else "",
            "replies": items,
        }
        result = await html_render(
            load_replies_template(),
            context,
            options={
                "full_page": True,
                "type": "jpeg",
                "quality": 90,
                "timeout": timeout * 1000,
                "viewport_width": card_width,
            },
        )
        if result:
            return str(result)
        logger.warning("热评卡片 html_render 返回空结果，本次不发热评图")
    except Exception as exc:  # noqa: BLE001 - 热评图失败不影响主流程
        logger.warning(f"热评卡片渲染失败（本次不发热评图）：{type(exc).__name__}: {exc}")
    return None


__all__ = [
    "DEFAULT_CARD_WIDTH",
    "build_context",
    "format_pubdate",
    "inline_image",
    "load_replies_template",
    "load_template",
    "plain_text",
    "render_card",
    "render_replies_card",
    "replies_template_available",
    "template_available",
    "template_path",
]
