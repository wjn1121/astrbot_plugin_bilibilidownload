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

from .bili_api import ReplyInfo
from .constants import VERSION
from .formatting import format_count, format_duration, humanize_age
from .http import BASE_HEADERS
from .media import MediaInfo
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

# 图片 CDN 的防盗链按 Referer 判定，写死 B 站会让别的平台全部 403。
# 实测（2026-11-01）：小红书封面带 bilibili Referer → 403、带小红书 Referer → 200，
# 所以这里必须按来源平台换。头像两边都放行，但统一处理更省心。
PLATFORM_REFERER = {
    "bilibili": "https://www.bilibili.com/",
    "douyin": "https://www.douyin.com/",
    "xiaohongshu": "https://www.xiaohongshu.com/",
}
DEFAULT_REFERER = "https://www.bilibili.com/"

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


async def inline_image(
    session: aiohttp.ClientSession, url: str | None, *, referer: str | None = None
) -> str | None:
    """把远程图片转成 data URI；失败、空内容或超限时返回 None。

    ``referer`` 由调用方按平台给（见 ``PLATFORM_REFERER``）。给 None 时沿用
    B 站 Referer，保持 v0.2.x 的行为不变。

    返回 None 是正常路径——模板会用灰底占位块顶上，不影响卡片其余部分。
    """
    if not url:
        return None
    if url.startswith("data:"):
        return url
    if not url.startswith("http"):
        return None

    headers = IMAGE_HEADERS if not referer else {**IMAGE_HEADERS, "Referer": referer}
    try:
        async with session.get(
            url,
            headers=headers,
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
    info: MediaInfo,
    *,
    summary: str | None,
    summary_basis: str | None,
    cover_uri: str | None,
    face_uri: str | None,
    card_width: int = DEFAULT_CARD_WIDTH,
) -> dict:
    """把 ``MediaInfo`` 映射成模板变量。

    入参是跨平台模型，所以新增平台不需要再写一套卡片：只要 ``parse()`` 填好了
    标题 / 作者 / 封面 / 头像 / 发布时间，模板就能直接渲染。
    变量名保持与 v0.2.x 一致（``owner`` / ``title`` …），模板文件不用改。

    ``brand`` 是右上角那个平台角标：模板按它决定画哪家的标识。取不到平台
    （老数据 / 第三方构造的 MediaInfo）时给空串，模板会整块跳过——**宁可没有
    角标，也不要把抖音的内容标成 bilibili**。
    """
    return {
        "card_width": card_width,
        "brand": (info.platform or "").strip().lower(),
        "owner": (info.author or "未知 UP 主").strip(),
        "owner_face": face_uri or "",
        "pubdate_text": format_pubdate(info.pubdate),
        "title": (info.title or "视频").strip(),
        "cover": cover_uri or "",
        "summary": (summary or "").strip(),
        "summary_basis": (summary_basis or "").strip(),
    }


def plain_text(info: MediaInfo, *, summary: str | None, summary_basis: str | None) -> str:
    """``text_to_image`` 降级用的纯文本内容。"""
    title = (info.title or "视频").strip()
    owner = (info.author or "未知 UP 主").strip()
    lines = [f"📺 {title}", f"👤 {owner}"]
    if summary:
        basis = f"（{summary_basis}）" if summary_basis else ""
        lines += ["", f"AI总结：{summary}{basis}"]
    return "\n".join(lines)


# 单张卡片的**总渲染预算**（秒）。
# AstrBot 的 t2i 会在多个端点之间轮换重试（日志里的 "trying next..."），服务整体
# 挂掉时一次 html_render 也可能连拖好几次；没有这个上限时，一条消息能被拖到 4 分钟
# 以上才发出（2026-10-23 实测：20:54:32 开始渲染，20:59:41 才发出视频）。
# 超预算就当渲染失败，走纯文本降级——卡片是锦上添花，不能拖慢回复。
CARD_RENDER_BUDGET = 45


async def render_card(
    star: Any,
    session: aiohttp.ClientSession,
    *,
    info: MediaInfo,
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
            referer = PLATFORM_REFERER.get((info.platform or "").lower(), DEFAULT_REFERER)
            cover_uri, face_uri = await asyncio.gather(
                inline_image(session, info.cover, referer=referer),
                inline_image(session, info.author_avatar, referer=referer),
            )
            context = build_context(
                info,
                summary=summary,
                summary_basis=summary_basis,
                cover_uri=cover_uri,
                face_uri=face_uri,
                card_width=card_width,
            )
            # 外层再套一层总预算：html_render 自己的 timeout 只管「一次截图」，
            # 管不住 t2i 在多个端点之间的轮换重试。
            result = await asyncio.wait_for(
                html_render(
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
                ),
                timeout=CARD_RENDER_BUDGET,
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
            result = await asyncio.wait_for(
                text_to_image(
                    plain_text(info, summary=summary, summary_basis=summary_basis)
                ),
                timeout=CARD_RENDER_BUDGET,
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


def _meta_items(info: MediaInfo) -> list[dict]:
    """热评图顶部信息区的「标签 / 值」列表（对齐参考图的排布）。

    只用 ``parse()`` 已经拿到的字段，**不额外发请求**：分辨率与分区来自
    ``extras``（B 站填的），评论数与时长来自通用字段。非 B 站平台没填的项自动省略，
    所以新平台即使什么都不补，热评图也只是少两格而已。
    """
    items: list[dict] = []

    try:
        width = int(info.extra("width") or 0)
        height = int(info.extra("height") or 0)
    except (TypeError, ValueError):
        width = height = 0
    if width and height:
        items.append({"label": "分辨率", "value": f"{width} x {height}"})

    tname = str(info.extra("tname") or "").strip()
    if tname:
        items.append({"label": "类型", "value": tname})

    reply_count = info.stat("reply")
    if reply_count:
        items.append({"label": "评论", "value": f"{format_count(reply_count)} 条"})

    if info.duration:
        items.append({"label": "时长", "value": format_duration(info.duration)})

    return items


def _qr_markup(info: MediaInfo, page: int) -> str:
    """视频链接的二维码 SVG；拿不到链接或生成失败时返回空串（模板整块跳过）。

    链接优先取平台给的 ``extras["qr_url"]``（B 站要指向 bilibili.com 而不是下载站），
    没有就退回用户发的原始链接——扫出来至少能打开同一个内容。
    """
    url = str(info.extra("qr_url") or "").strip() or info.source_url or info.web_url
    if not url:
        return ""
    if page > 1 and "p=" not in url and "?" not in url:
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
    info: MediaInfo,
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
        try:
            owner_id = int(info.author_id or 0)
        except (TypeError, ValueError):
            owner_id = 0
        items = await _build_reply_items(
            session,
            replies,
            show_count,
            owner_mid=owner_id,
        )
        if not items:
            return None

        context = {
            "card_width": card_width,
            "brand": (info.platform or "").strip().lower(),
            "video_title": (info.title or "").strip(),
            "plugin_version": VERSION,
            "meta_items": _meta_items(info),
            "qrcode": _qr_markup(info, page) if with_qrcode else "",
            "replies": items,
        }
        # 与信息卡片一样套总预算：热评图渲染失败只是少一张图，不该拖住整条回复。
        result = await asyncio.wait_for(
            html_render(
                load_replies_template(),
                context,
                options={
                    "full_page": True,
                    "type": "jpeg",
                    "quality": 90,
                    "timeout": timeout * 1000,
                    "viewport_width": card_width,
                },
            ),
            timeout=CARD_RENDER_BUDGET,
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
