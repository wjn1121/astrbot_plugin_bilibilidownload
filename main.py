"""B站视频下载 - AstrBot 插件

群成员发送 B 站视频链接（或 BV/av 号、b23.tv 短链）后：
1. 解析视频信息（标题、UP主、时长、播放量、分P）；
2. 用当前会话的大模型生成一段简短总结（基于简介与热评，如实标注依据）；
3. 按配置决定是否从 ebilibili 下载视频文件并发送到会话。

默认行为是"只发解析 + 总结"（send_video=false），下载视频文件由面板开关控制，
也可以让用户在会话里回复「下载」或使用 /bdl 命令按需获取。

发送方式说明（video_send_mode）：
- auto：先发本地文件，失败（例如协议端与 AstrBot 不在同一文件系统，
  协议端报 retcode 1200「路径不存在」）时自动改用直链；
- file：只发本地文件，要求协议端能读到 AstrBot 的文件；
- url：直接把 ebilibili 直链交给协议端拉取，本插件不落盘。

⚠️ 所有发送都必须在 await 完成后才能删除临时文件：
早期实现用 `yield chain` 后立即 unlink，生成器会在 AstrBot 真正发送**之前**
恢复执行，导致协议端去读文件时文件已被删除（retcode 1200 路径不存在）。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import aiohttp

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .core.bili_api import REPLY_MAX_CHARS, BiliApi, BiliApiError, ReplyInfo, VideoInfo
from .core.cache import TtlCache
from .core.card import (
    render_card,
    render_replies_card,
    replies_template_available,
    template_available,
    template_path,
)
from .core.constants import VERSION
from .core.downloader import DownloadError, VideoTooLarge, cleanup_old_files, download_video
from .core.ebilibili import EbilibiliClient
from .core.formatting import (
    build_caption,
    humanize_age,
    humanize_duration,
    sanitize_filename,
    short_text,
)
from .core.link_parser import (
    DOWNLOAD_WORD_REGEX,
    TRIGGER_REGEX,
    BiliRef,
    aid_from_url,
    bvid_from_url,
    find_ref,
    url_from_card,
)
from .core.stats import RuntimeStats
from .core.subtitle import fetch_subtitle, fetch_wbi_keys
from .core.summarizer import summarize_video

DATA_DIR_NAME = "astrbot_plugin_bilibilidownload"
LOG_TAG = "[bilibili-download]"

# 「接收所有消息」的事件类型，用于 QQ 小程序 / JSON 卡片的兜底入口。
# AstrBot 的 message_str 只拼接 Plain 段，卡片里的链接进不去，@filter.regex 匹配不到。
# 若某个版本的 AstrBot 没有 EventMessageType，这里得到 None，该入口不注册
# （纯文本路径不受影响，属安全降级）。
_CARD_EVENT_TYPE = getattr(getattr(filter, "EventMessageType", None), "ALL", None)

# 「下载」关键词触发时，多久之内解析过的视频还算"当前视频"
RECENT_TTL = 30 * 60

# 卡片渲染连续失败多少次后，本进程内不再尝试（见 _render_card）
CARD_FAIL_LIMIT = 2

# 视频信息 / 热评的进程内缓存 TTL（秒）。
# 播放量这类统计会变，TTL 不宜过长；但同一个视频常被多个群先后发出，
# 缓存能省掉重复请求——直链有时效，绝不缓存。
VIEW_CACHE_TTL = 300
REPLY_CACHE_TTL = 300


def _busy_key(ref: BiliRef) -> str:
    """归一化「同一视频」的判定 key，并区分分P。

    同一视频的「BV 号 / 完整链接 / b23 短链」三种写法会归一到同一个 key；
    `?p=1` 与 `?p=2` 是两个不同视频，必须分开。
    短链在此时可能还没解析出 bvid，退回用短链原串。
    """
    ident = ref.bvid or (f"av{ref.aid}" if ref.aid else "") or ref.short_url or ref.raw
    return f"{ident}#p{ref.page}"


def _admin_command(name: str, *aliases: str):
    """注册「仅管理员」指令。

    为什么要多绕一层：`filter.permission_type` 与 `command(alias=...)` 都属于较新的
    API，若直接写在装饰器上，一旦当前 AstrBot 版本不支持，就会在**导入阶段**抛异常
    ——那比「权限没生效」严重得多（整个插件加载失败）。这里缺哪个 API 就少用哪个，
    并在降级时留一条 warning。
    """

    def decorator(func):
        command = getattr(filter, "command", None)
        if command is not None:
            try:
                wrapped = command(name, alias=set(aliases))(func)
            except TypeError:
                wrapped = command(name)(func)
        else:
            wrapped = func

        admin = getattr(getattr(filter, "PermissionType", None), "ADMIN", None)
        permission_type = getattr(filter, "permission_type", None)
        if admin is not None and permission_type is not None:
            wrapped = permission_type(admin)(wrapped)
        else:
            logger.warning(
                f"{LOG_TAG} 当前 AstrBot 版本不支持 permission_type，"
                f"指令 /{name} 将不限制管理员"
            )
        return wrapped

    return decorator


@dataclass(slots=True)
class RecentVideo:
    """会话内最近一次解析结果，供「下载」关键词与 /bdl 复用。"""

    info: VideoInfo
    page: int
    link: str
    at: float


@dataclass(slots=True)
class ProcessResult:
    """一次解析的产物。"""

    caption: str = ""
    card_url: str | None = None  # 信息卡片图片；None 表示回退纯文本 caption
    replies_card_url: str | None = None  # 热评卡片（独立图片），可能为 None
    info: VideoInfo | None = None  # 供「下载并发送」阶段复用，避免重复请求 B 站
    page: int = 1
    link: str = ""
    video_path: Path | None = None  # 已下载到本地的视频（url 模式为 None）
    direct_url: str | None = None  # ebilibili 直链，供协议端自行拉取
    oversized_mb: float | None = None  # 超过体积上限时的真实体积

    @property
    def has_video(self) -> bool:
        return self.video_path is not None or bool(self.direct_url)


@dataclass(slots=True)
class OutgoingMessage:
    """一条待发送的消息（用于按阈值决定是否合并转发）。

    ``fallback_text`` 是图片发送失败时的兜底文本——卡片图发不出去时
    至少还能把解析结果以文字发出去。
    ``video_path`` 仅用于 ``kind == "video"``：本地文件兜底；合并转发时优先用
    ``payload`` 里的直链（协议端分离部署读不到机器人的本地文件）。
    """

    kind: str  # "text" | "image" | "video"
    payload: str = ""
    fallback_text: str = ""
    video_path: Path | None = None


class BilibiliDownloadPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # ── 配置项 ──────────────────────────────────────────
        self.trigger_scope = self._conf_str("trigger_scope", "group")
        self.need_at_in_group = self._conf_bool("need_at_in_group", False)
        # 收到链接时给原消息贴的表情 ID（0 = 不贴，改用文字提示）；仅 aiocqhttp 生效
        self.react_emoji_id = max(0, self._conf_int("react_emoji_id", 289))
        self.enable_summary = self._conf_bool("enable_summary", True)
        self.summary_max_chars = max(50, self._conf_int("summary_max_chars", 400))
        self.summary_timeout = max(5, self._conf_int("summary_timeout", 45))
        self.send_video = self._conf_bool("send_video", False)
        self.video_send_mode = self._conf_str("video_send_mode", "auto")
        self.max_video_mb = max(1, self._conf_int("max_video_mb", 50))
        self.request_timeout = max(5, self._conf_int("request_timeout", 20))
        self.max_concurrent = max(1, self._conf_int("max_concurrent", 1))
        self.user_cooldown = max(0, self._conf_int("user_cooldown", 60))
        # 相同视频（归一化后的视频标识）在多少分钟内只处理一次；0 = 不做这层限制
        self.duplicate_window_minutes = max(
            0, self._conf_int("duplicate_window_minutes", 10)
        )
        self.group_whitelist = self._conf_list("group_whitelist")
        self.send_ebilibili_link = self._conf_bool("send_ebilibili_link", True)
        self.reply_limit = max(0, self._conf_int("reply_limit", 10))
        # 卡片里展示给用户看的热评（与 reply_limit「给 AI 参考的条数」相互独立）
        self.show_replies = self._conf_bool("show_replies", True)
        self.replies_show_count = max(0, self._conf_int("replies_show_count", 5))
        # 解析产生的消息条数达到该值时合并转发；0 = 关闭，始终逐条发送
        self.forward_threshold = max(0, self._conf_int("forward_threshold", 3))
        self.forward_sender_name = self._conf_str("forward_sender_name", "B站视频解析") or "B站视频解析"
        self.output_image = self._conf_bool("output_image", True)
        self.card_width = max(560, min(1000, self._conf_int("card_width", 760)))
        # 热评图右上角的二维码（内容 = 视频链接）；生成失败会自动跳过该区域
        self.card_qrcode = self._conf_bool("card_qrcode", True)
        # 字幕增强（L3）：留空 sessdata 时行为与未启用完全一致
        self.sessdata = self._conf_str("sessdata", "")
        self.prefer_subtitle = self._conf_bool("prefer_subtitle", True)
        self.subtitle_max_chars = max(500, self._conf_int("subtitle_max_chars", 6000))
        self.verbose_log = self._conf_bool("verbose_log", False)

        # ── 运行时状态 ──────────────────────────────────────
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(self.max_concurrent)
        self._cooldown: dict[str, float] = {}
        self._recent: dict[str, RecentVideo] = {}
        # key 是归一化后的视频标识（见 _busy_key），value 是开始处理的时间戳
        self._busy: dict[str, float] = {}

        # 跨会话共享的接口缓存：同一个视频被多个群先后发出时不重复请求 B 站
        self._view_cache = TtlCache(max_items=256, ttl=VIEW_CACHE_TTL, name="视频信息")
        self._reply_cache = TtlCache(max_items=256, ttl=REPLY_CACHE_TTL, name="热评")

        # 运行统计 + 会话级开关（/bdlstatus、/bdloff 用）
        self._stats = RuntimeStats()
        self._disabled_sessions: set[str] = set()

        # 卡片渲染连续失败到阈值后本进程内不再尝试：
        # t2i 服务没配好时，每次渲染都要白等一次超时，会明显拖慢回复。
        self._card_fail_count = 0
        self._card_disabled = False

        # 临时文件放在 AstrBot 数据目录，而不是插件自身目录：
        # 插件目录可能被更新/覆盖，数据目录才是可写且持久的位置。
        self._data_dir = Path(StarTools.get_data_dir(DATA_DIR_NAME))
        self._tmp_dir = self._data_dir / "tmp"
        try:
            self._tmp_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(f"{LOG_TAG} 无法创建数据目录 {self._tmp_dir}：{exc}")
        cleanup_old_files(self._tmp_dir, 24)

        if self.output_image and not template_available():
            logger.warning(
                f"{LOG_TAG} 未找到卡片模板 {template_path()}，图片卡片将退化为纯文本；"
                f"请确认 templates/ 目录随插件一起部署"
            )
        if self.output_image and self.show_replies and not replies_template_available():
            logger.warning(
                f"{LOG_TAG} 未找到热评卡片模板 templates/replies_card.html，"
                f"本次将不发送热评图；请确认该文件随插件一起部署"
            )

        logger.info(
            f"{LOG_TAG} 插件已加载（触发范围={self.trigger_scope}，"
            f"自动发视频={'开' if self.send_video else '关'}，"
            f"发送方式={self.video_send_mode}，体积上限={self.max_video_mb}MB，"
            f"图片卡片={'开' if self.output_image else '关'}，"
            f"字幕增强={'开' if (self.sessdata and self.prefer_subtitle) else '关'}，"
            f"解析提示={'表情' + str(self.react_emoji_id) if self.react_emoji_id else '文字'}，"
            f"合并转发阈值={self.forward_threshold or '关'}）"
        )

    # ──────────────────────────────────────────────── 配置读取

    def _conf_bool(self, key: str, default: bool) -> bool:
        value = self.config.get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on")
        return bool(value)

    def _conf_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            logger.warning(f"{LOG_TAG} 配置项 {key} 不是合法整数，使用默认值 {default}")
            return default

    def _conf_str(self, key: str, default: str) -> str:
        value = self.config.get(key, default)
        return str(value).strip() if value is not None else default

    def _conf_list(self, key: str) -> list[str]:
        value = self.config.get(key, [])
        if isinstance(value, str):
            items = [part.strip() for part in value.replace("，", ",").split(",")]
            return [item for item in items if item]
        if isinstance(value, (list, tuple)):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    # ──────────────────────────────────────────────── 会话与工具

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._session_lock:
            if self._session is not None and not self._session.closed:
                return self._session
            self._session = aiohttp.ClientSession()
            return self._session

    @staticmethod
    def _message_text(event: AstrMessageEvent) -> str:
        return (getattr(event, "message_str", "") or "").strip()

    @staticmethod
    def _stringify(value: object) -> str:
        """把卡片字段转成文本；类型不对时返回空串。"""
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            try:
                return json.dumps(value, ensure_ascii=False)
            except (TypeError, ValueError):
                return ""
        return ""

    def _card_texts(self, event: AstrMessageEvent) -> list[str]:
        """取出 QQ 小程序 / JSON 卡片里可能藏着链接的文本，按优先级排序。

        「精确提取出的链接」排在「卡片原文」之前——卡片原文里常混着推荐位等
        无关链接，而 ``find_ref`` 只取第一个命中的。

        查两处，因为不同协议端与不同 AstrBot 版本给的东西不一样：

        1. 消息链组件——AstrBot 会把它包成 Json / Xml 组件；
        2. 协议端原始消息段——OneBot v11 的 ``[CQ:json]`` / ``[CQ:xml]``，
           也就是 ``raw_message["message"]`` 里的段。

        返回空列表表示这条消息里没有卡片内容。
        """
        raws: list[str] = []

        for component in event.get_messages():
            if type(component).__name__ not in ("Json", "Xml"):
                continue
            # 优先读 data 字段；拿不到时退回组件的字符串形式（不同版本包装方式不同）
            text = self._stringify(getattr(component, "data", None)) or str(component)
            if text:
                raws.append(text)

        raw_message = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if isinstance(raw_message, dict):
            for segment in raw_message.get("message") or []:
                if not isinstance(segment, dict):
                    continue
                if str(segment.get("type") or "").lower() not in ("json", "xml"):
                    continue
                data = segment.get("data")
                # 段数据既可能是 {"data": "<json 字符串>"}，也可能已经把 data
                # 解析成了对象，两种形态都要接住。
                if isinstance(data, dict):
                    text = self._stringify(data.get("data"))
                else:
                    text = self._stringify(data)
                if text:
                    raws.append(text)

        ordered = [url for url in (url_from_card(text) for text in raws) if url]
        ordered.extend(raws)
        return ordered

    def _is_at_bot(self, event: AstrMessageEvent) -> bool:
        self_id = str(event.get_self_id())
        for component in event.get_messages():
            if isinstance(component, Comp.At) and str(component.qq) == self_id:
                return True
        return False

    def _at_others(self, event: AstrMessageEvent) -> bool:
        """消息里是否 @ 了机器人以外的人（含 @全体成员）。

        用于避免「A 在跟 B 说话、顺手贴了个链接」被机器人插话。
        """
        self_id = str(event.get_self_id())
        for component in event.get_messages():
            if isinstance(component, Comp.At) and str(component.qq) != self_id:
                return True
        return False

    def _allowed(self, event: AstrMessageEvent, *, auto: bool = True) -> bool:
        """判断当前会话是否在插件的工作范围内。"""
        is_group = bool(event.get_group_id())

        if self.trigger_scope == "group" and not is_group:
            return False
        if self.trigger_scope == "private" and is_group:
            return False

        if is_group and self.group_whitelist:
            if str(event.get_group_id()) not in self.group_whitelist:
                return False

        # 以下三条只约束"自动响应链接"；显式命令（/bdl、/bdlon…）与「下载」关键词不受影响
        if auto:
            if event.unified_msg_origin in self._disabled_sessions:
                return False
            at_bot = self._is_at_bot(event)
            if is_group and self.need_at_in_group and not at_bot:
                return False
            if is_group and not at_bot and self._at_others(event):
                return False

        return True

    def _cooldown_blocked(self, event: AstrMessageEvent) -> bool:
        """同一用户短时间内重复发链接时静默忽略，避免刷屏与带宽浪费。"""
        if self.user_cooldown <= 0:
            return False
        key = f"{event.unified_msg_origin}:{event.get_sender_id()}"
        now = time.time()

        # 顺手清理过期条目，防止长期运行内存缓慢增长
        for old_key in [k for k, ts in self._cooldown.items() if now - ts > self.user_cooldown * 2]:
            self._cooldown.pop(old_key, None)

        last = self._cooldown.get(key)
        if last is not None and now - last < self.user_cooldown:
            logger.info(f"{LOG_TAG} 用户 {event.get_sender_id()} 触发过于频繁，已忽略")
            return True
        self._cooldown[key] = now
        return False

    def _busy_blocked(self, key: str) -> bool:
        """同一视频在判定窗口内只处理一次（**跨用户、跨会话**）。

        判定依据是归一化后的视频标识（见 `_busy_key`），所以不同用户发同一个链接
        也会被判为重复。窗口由 `duplicate_window_minutes` 配置（默认 10 分钟），
        设为 0 表示不做这层限制。

        用时间戳而不是集合：生成器被提前中断时 finally 不一定会执行，
        时间戳形式即使漏掉一次清理也会自动过期，不会永久卡死。
        """
        if self.duplicate_window_minutes <= 0:
            return False

        window = self.duplicate_window_minutes * 60
        now = time.time()
        for old_key in [k for k, ts in self._busy.items() if now - ts > window]:
            self._busy.pop(old_key, None)
        if key in self._busy:
            return True
        self._busy[key] = now
        return False

    def _remember_busy(self, key: str) -> None:
        """把归一化后的视频 key 补登记进判定窗口。

        短链在 `_busy_key` 里只能用自己的原串（那时还没解析出 bvid），
        拿到 bvid 后补登一次，才能让「短链 ↔ 长链」指向同一视频时互相拦住。
        用 setdefault 保留原时间戳，避免刷新窗口起点。
        """
        if self.duplicate_window_minutes <= 0:
            return
        self._busy.setdefault(key, time.time())

    def _remember(self, umo: str, info: VideoInfo, page: int, link: str) -> None:
        self._recent[umo] = RecentVideo(info=info, page=page, link=link, at=time.time())

    def _recall(self, umo: str) -> RecentVideo | None:
        recent = self._recent.get(umo)
        if recent is None:
            return None
        if time.time() - recent.at > RECENT_TTL:
            self._recent.pop(umo, None)
            return None
        return recent

    def _safe_unlink(self, path: Path | None) -> None:
        if path is None:
            return
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            logger.warning(f"{LOG_TAG} 清理临时文件失败 {path}：{exc}")

    # ──────────────────────────────────────────────── 消息发送

    async def _react_parsing(self, event: AstrMessageEvent) -> bool:
        """给触发消息贴一个表情，表示「已开始解析」。

        为什么用表情回应而不是发一条文字：少一条消息、不刷屏，也是同类插件的
        通行做法。返回是否贴成功——失败时调用方回退为发文字，保证用户始终有反馈。

        仅 aiocqhttp（QQ）支持；平台或协议端不支持时静默返回 False。
        """
        if not self.react_emoji_id:
            return False
        if event.get_platform_name() != "aiocqhttp":
            return False

        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )

            if not isinstance(event, AiocqhttpMessageEvent):
                return False
            await event.bot.api.call_action(
                "set_msg_emoji_like",
                message_id=event.message_obj.message_id,
                emoji_id=self.react_emoji_id,
            )
            return True
        except Exception as exc:  # noqa: BLE001 - 贴表情失败不该影响解析
            logger.debug(f"{LOG_TAG} 贴表情失败（回退为文字提示）：{type(exc).__name__}: {exc}")
            return False

    async def _send_text(self, event: AstrMessageEvent, text: str) -> bool:
        """发送一条纯文本；失败只记日志，不让一条消息打断整个流程。"""
        try:
            await event.send(event.plain_result(text))
            return True
        except Exception as exc:  # noqa: BLE001 - 平台侧异常类型随适配器而变
            logger.error(f"{LOG_TAG} 发送文本失败：{type(exc).__name__}: {exc}")
            return False

    async def _send_video(
        self, event: AstrMessageEvent, video_path: Path | None, direct_url: str | None
    ) -> bool:
        """按 video_send_mode 发送视频，返回是否成功。

        必须 await 到发送结束再返回：调用方依赖这个时机来删除临时文件。
        视频单独作为一条消息发送（不与长文本合并），避免被包装成合并转发。
        """
        attempts: list[tuple[str, Callable[[], object]]] = []
        if self.video_send_mode in ("auto", "file") and video_path is not None:
            attempts.append(
                ("本地文件", lambda: Comp.Video.fromFileSystem(path=str(video_path)))
            )
        if self.video_send_mode in ("auto", "url") and direct_url:
            attempts.append(("直链", lambda: Comp.Video.fromURL(url=direct_url)))

        for name, factory in attempts:
            try:
                component = factory()
            except Exception as exc:  # noqa: BLE001 - 例如 fromURL 收到非法 URL
                logger.warning(f"{LOG_TAG} 「{name}」方式构造视频组件失败：{exc}")
                continue

            try:
                await event.send(event.chain_result([component]))
            except Exception as exc:  # noqa: BLE001 - retcode 1200「路径不存在」等
                logger.warning(
                    f"{LOG_TAG} 「{name}」方式发送视频失败：{type(exc).__name__}: {exc}"
                )
                continue

            logger.info(f"{LOG_TAG} 视频已通过「{name}」方式发送")
            return True

        logger.error(
            f"{LOG_TAG} 视频发送失败。若协议端与 AstrBot 不在同一文件系统，"
            f"请把「视频发送方式」设为 url，或检查协议端日志"
        )
        return False

    def _collect_cards(self, result: ProcessResult) -> list[OutgoingMessage]:
        """解析结果本身对应的消息：信息卡片（或纯文本）+ 热评图。"""
        messages: list[OutgoingMessage] = []

        if result.card_url:
            messages.append(
                OutgoingMessage(
                    kind="image",
                    payload=result.card_url,
                    fallback_text=result.caption,
                )
            )
        else:
            messages.append(OutgoingMessage(kind="text", payload=result.caption))

        if result.replies_card_url:
            messages.append(OutgoingMessage(kind="image", payload=result.replies_card_url))

        return messages

    def _collect_media(
        self,
        result: ProcessResult,
        *,
        video_path: Path | None = None,
        direct_url: str | None = None,
        oversized_mb: float | None = None,
    ) -> list[OutgoingMessage]:
        """视频（或「超过体积上限」提示）对应的消息；没有就返回空列表。

        与卡片分开是为了支持「卡片先出」——关闭合并转发时先发卡片，
        下载完成后再单独发这一段。
        """
        if video_path is not None or direct_url:
            return [
                OutgoingMessage(
                    kind="video", payload=direct_url or "", video_path=video_path
                )
            ]
        if oversized_mb is not None:
            return [
                OutgoingMessage(
                    kind="text", payload=self._oversized_tip(result, oversized_mb)
                )
            ]
        return []

    def _collect_messages(
        self,
        result: ProcessResult,
        *,
        video_path: Path | None = None,
        direct_url: str | None = None,
        oversized_mb: float | None = None,
    ) -> list[OutgoingMessage]:
        """把一次解析的结果整理成待发送的消息列表（顺序即发送顺序）。

        视频与「超限提示」也在这里进列表：触发合并转发时它们会成为转发里的一个节点。
        """
        return self._collect_cards(result) + self._collect_media(
            result,
            video_path=video_path,
            direct_url=direct_url,
            oversized_mb=oversized_mb,
        )

    def _oversized_tip(self, result: ProcessResult, oversized_mb: float) -> str:
        """视频超过体积上限时的提示（尽量附上可用的下载页链接）。"""
        tip = (
            f"⚠️ 视频约 {oversized_mb:.0f} MB，超过体积上限 {self.max_video_mb} MB，未发送文件。"
        )
        if result.info is not None and self.send_ebilibili_link:
            tip += (
                f"\n可在面板调大「视频体积上限」，"
                f"或直接访问：{EbilibiliClient.web_url(result.info.bvid)}"
            )
        return tip

    async def _dispatch_messages(
        self, event: AstrMessageEvent, messages: list[OutgoingMessage]
    ) -> None:
        """发送解析产生的多条消息。

        条数达到 ``forward_threshold`` 时打包成一条合并转发（参考同类插件的做法），
        否则逐条发送。

        **注意**：视频参与合并转发时必须先下载完成（由调用方负责），所以「卡片先出」
        只在**关闭合并转发**（``forward_threshold = 0``）时成立——那时调用方会先把
        解析结果单独发一次，下载完再发视频。

        合并转发里的媒体段在部分协议端可能失败，所以整体失败时会自动回退为逐条发送。
        """
        if not messages:
            return

        if self._should_forward(event, len(messages)) and await self._send_forward(event, messages):
            return

        for message in messages:
            if message.kind == "image":
                await self._send_image(event, message.payload, message.fallback_text)
            elif message.kind == "video":
                await self._send_video(event, message.video_path, message.payload or None)
            else:
                await self._send_text(event, message.payload)

    def _should_forward(self, event: AstrMessageEvent, count: int) -> bool:
        """是否满足合并转发条件（阈值、群聊、aiocqhttp 三者缺一不可）。"""
        return (
            self.forward_threshold > 0
            and count >= self.forward_threshold
            and bool(event.get_group_id())
            and event.get_platform_name() == "aiocqhttp"
        )

    async def _send_forward(
        self, event: AstrMessageEvent, messages: list[OutgoingMessage]
    ) -> bool:
        """把多条消息打包成一条合并转发；不可用或失败时返回 False。

        AstrBot 的合并转发由 ``Nodes``（内含多个 ``Node``）构造，仅 OneBot v11
        （aiocqhttp）有效；其它平台直接返回 False，由调用方逐条发送。
        """
        node_cls = getattr(Comp, "Node", None)
        nodes_cls = getattr(Comp, "Nodes", None)
        if node_cls is None or nodes_cls is None:
            return False

        try:
            sender_id = str(event.get_self_id() or "0")
            nodes = []
            for message in messages:
                if message.kind == "image":
                    component = (
                        Comp.Image.fromURL(message.payload)
                        if message.payload.startswith("http")
                        else Comp.Image.fromFileSystem(message.payload)
                    )
                elif message.kind == "video":
                    # 优先用直链：协议端与 AstrBot 可能不在同一文件系统，
                    # 转发节点里塞本地路径会直接失败（retcode 1200），
                    # 这与 v0.1.1 修过的问题同源。
                    if message.payload:
                        component = Comp.Video.fromURL(url=message.payload)
                    elif message.video_path is not None:
                        component = Comp.Video.fromFileSystem(path=str(message.video_path))
                    else:
                        continue
                else:
                    component = Comp.Plain(message.payload)
                nodes.append(
                    node_cls(
                        uin=sender_id,
                        name=self.forward_sender_name,
                        content=[component],
                    )
                )

            await event.send(event.chain_result([nodes_cls(nodes)]))
            logger.info(f"{LOG_TAG} 已合并转发 {len(nodes)} 条消息")
            return True
        except Exception as exc:  # noqa: BLE001 - 转发失败就退回逐条发送
            logger.warning(
                f"{LOG_TAG} 合并转发失败，改为逐条发送：{type(exc).__name__}: {exc}"
            )
            return False

    async def _send_image(
        self, event: AstrMessageEvent, url: str, fallback_text: str = ""
    ) -> bool:
        """发送一张图片；失败时若给了兜底文本，则改发文本。"""
        try:
            await event.send(event.image_result(url))
            return True
        except Exception as exc:  # noqa: BLE001 - 平台侧异常类型随适配器而变
            self._stats.send_fail += 1
            logger.error(f"{LOG_TAG} 发送图片失败：{type(exc).__name__}: {exc}")
            if fallback_text:
                logger.info(f"{LOG_TAG} 图片发送失败，改发纯文本卡片")
                return await self._send_text(event, fallback_text)
            return False

    async def _render_card(
        self, info: VideoInfo, summary: str | None, basis: str | None
    ) -> str | None:
        """渲染信息卡片；连续失败达到阈值后本进程内不再尝试。"""
        if not self.output_image or self._card_disabled:
            return None

        session = await self._get_session()
        url = await render_card(
            self,
            session,
            info=info,
            summary=summary,
            summary_basis=basis,
            card_width=self.card_width,
        )
        if url:
            self._card_fail_count = 0
            self._stats.card_ok += 1
            return url

        self._card_fail_count += 1
        self._stats.card_fail += 1
        if self._card_fail_count >= CARD_FAIL_LIMIT:
            self._card_disabled = True
            logger.warning(
                f"{LOG_TAG} 卡片渲染连续失败 {self._card_fail_count} 次，本进程内改用纯文本卡片；"
                f"请确认 AstrBot 的文转图服务可用，重载插件可重置该状态"
            )
        return None

    # ──────────────────────────────────────────────── 核心流程

    async def _resolve_ref(self, api: BiliApi, ref: BiliRef) -> tuple[str, int | None]:
        """把引用归一化成 (bvid, aid)，短链需要一次跳转。"""
        bvid, aid = ref.bvid, ref.aid

        if ref.is_short_link and ref.short_url:
            final_url = await api.resolve_short_url(ref.short_url)
            bvid = bvid_from_url(final_url) or bvid
            aid = aid_from_url(final_url) or aid
            logger.info(f"{LOG_TAG} 短链已解析：{ref.short_url} -> {final_url}")

        if not bvid and aid is None:
            raise BiliApiError("无法从链接中识别视频编号")
        return bvid, aid

    async def _get_view_cached(self, api: BiliApi, *, bvid: str, aid: int | None) -> VideoInfo:
        """取视频信息，命中缓存则不请求 B 站（跨会话共享）。"""
        key = f"view:{bvid or aid}"
        cached = self._view_cache.get(key)
        if isinstance(cached, VideoInfo):
            if self.verbose_log:
                logger.info(f"{LOG_TAG} 视频信息命中缓存：{key}")
            return cached

        info = await api.get_view(bvid=bvid, aid=aid)
        self._view_cache.set(key, info)
        return info

    async def _get_replies_cached(self, api: BiliApi, aid: int, limit: int) -> list[ReplyInfo]:
        """取热评（结构化）；空结果不缓存，避免一次接口抖动把「没有热评」固化 5 分钟。"""
        key = f"reply:{aid}:{limit}"
        cached = self._reply_cache.get(key)
        if isinstance(cached, list):
            if self.verbose_log:
                logger.info(f"{LOG_TAG} 热评命中缓存：{key}")
            return cached

        replies = await api.get_hot_replies(aid, limit)
        if replies:
            self._reply_cache.set(key, replies)
        return replies

    async def _get_tags_cached(self, api: BiliApi, bvid: str) -> list[str]:
        """取视频标签（公开接口，无需登录）；与视频信息共用缓存实例与 TTL。

        标签是判断「这是什么类型的视频」最有效的线索之一，取不到就返回空列表。
        """
        if not bvid:
            return []
        key = f"tags:{bvid}"
        cached = self._view_cache.get(key)
        if isinstance(cached, list):
            if self.verbose_log:
                logger.info(f"{LOG_TAG} 视频标签命中缓存：{key}")
            return cached

        tags = await api.get_tags(bvid)
        if tags:
            self._view_cache.set(key, tags)
        return tags

    async def _get_wbi_keys(self, session: aiohttp.ClientSession) -> tuple[str, str] | None:
        """取 WBI 签名所需的 keys（缓存 5 分钟）；失败返回 None 由调用方降级。"""
        if not self.sessdata:
            return None

        cached = self._view_cache.get("wbi")
        if isinstance(cached, tuple) and len(cached) == 2:
            return cached

        keys = await fetch_wbi_keys(
            session, sessdata=self.sessdata, timeout=self.request_timeout
        )
        if keys:
            self._view_cache.set("wbi", keys)
        return keys

    async def _get_subtitle_text(self, info: VideoInfo, page: int) -> str | None:
        """尝试取字幕文本（L3）。

        未配置 sessdata、关闭了字幕优先、视频没有字幕、或任何一步失败，都返回
        None —— 调用方据此回退到元数据总结（L1/L2），不影响主流程。
        """
        if not (self.sessdata and self.prefer_subtitle):
            return None

        key = f"subtitle:{info.bvid}:{page}"
        cached = self._view_cache.get(key)
        if isinstance(cached, str):
            if self.verbose_log:
                logger.info(f"{LOG_TAG} 字幕命中缓存：{key}")
            return cached

        session = await self._get_session()
        try:
            keys = await self._get_wbi_keys(session)
            if keys is None:
                return None
            result = await fetch_subtitle(
                session,
                bvid=info.bvid,
                cid=info.cid_for_page(page),
                sessdata=self.sessdata,
                img_key=keys[0],
                sub_key=keys[1],
                timeout=self.request_timeout,
                max_chars=self.subtitle_max_chars,
            )
        except Exception as exc:  # noqa: BLE001 - 字幕是增强项，失败一律降级
            logger.warning(
                f"{LOG_TAG} 字幕获取失败，改用元数据总结：{type(exc).__name__}: {exc}"
            )
            return None

        if result is None:
            logger.info(f"{LOG_TAG} {info.bvid} 无可用字幕（或登录态失效），改用元数据总结")
            return None

        logger.info(
            f"{LOG_TAG} 字幕获取成功：{result.lan_doc or result.lan}，"
            f"{len(result.cues)} 段，{len(result.text)} 字"
        )
        self._view_cache.set(key, result.text)
        return result.text

    async def _process(self, event: AstrMessageEvent, ref: BiliRef) -> ProcessResult:
        """完成一次「解析 → 总结 → 渲染卡片」。

        **这里不下载视频**：调用方先把卡片发出去，再按需下载发送（见 on_bili_link）。
        否则用户要等下载完成才能看到解析结果，白白多等几秒到几十秒。
        """
        umo = event.unified_msg_origin
        session = await self._get_session()
        api = BiliApi(session, self.request_timeout)

        bvid, aid = await self._resolve_ref(api, ref)
        info = await self._get_view_cached(api, bvid=bvid, aid=aid)
        # 补登记 bvid：短链在 _busy_key 里只能用自己的原串，这里补上才能与长链互通
        self._remember_busy(f"{info.bvid}#p{ref.page}")

        link = (
            ref.raw
            if ref.raw.lower().startswith("http")
            else f"https://www.bilibili.com/video/{info.bvid}"
        )
        self._remember(umo, info, ref.page, link)
        logger.info(f"{LOG_TAG} 解析成功：{info.bvid} 《{info.title}》 P{ref.page}")

        # 热评只在需要总结时才去取，省一次请求
        replies: list[ReplyInfo] = []
        summary: str | None = None
        basis: str | None = None
        if self.enable_summary:
            # 热评与标签互不依赖，并行取可省一次往返；字幕要先用 WBI keys 签名，单独走
            replies, tags = await asyncio.gather(
                self._get_replies_cached(api, info.aid, self.reply_limit),
                self._get_tags_cached(api, info.bvid),
            )
            subtitle_text = await self._get_subtitle_text(info, ref.page)
            summary, basis = await summarize_video(
                self.context,
                umo=umo,
                info=info,
                # 喂 AI 时再截到 REPLY_MAX_CHARS：卡片展示用的是完整正文
                # （REPLY_DISPLAY_MAX_CHARS），这里截断只影响 token，不影响图片
                replies=[reply.content[:REPLY_MAX_CHARS] for reply in replies],
                max_chars=self.summary_max_chars,
                timeout=self.summary_timeout,
                tags=tags,
                subtitle_text=subtitle_text,
            )

        result = ProcessResult(info=info, page=ref.page, link=link)
        result.caption = build_caption(
            title=info.title or "B站视频",
            owner=info.owner,
            duration=info.duration,
            view_count=info.view,
            page=ref.page,
            summary=summary,
            summary_basis=basis,
            web_url=EbilibiliClient.web_url(info.bvid) if self.send_ebilibili_link else None,
            # 此刻还没下载，不能写「视频已附在下方」
            video_attached=False,
            oversized_mb=None,
            download_hint=(
                "🎬 视频正在下载，稍后发送…"
                if self.send_video
                else "💡 回复「下载」或发送 /bdl 可获取视频文件"
            ),
        )

        # 信息卡片：渲染失败返回 None，发送阶段自动回退纯文本
        result.card_url = await self._render_card(info, summary, basis)

        # 热评单独成图：与信息卡片分开，避免把卡片拉得过长
        if self.output_image and self.show_replies and replies and self.replies_show_count > 0:
            result.replies_card_url = await render_replies_card(
                self,
                session,
                info=info,
                replies=replies,
                show_count=self.replies_show_count,
                card_width=self.card_width,
                page=ref.page,
                with_qrcode=self.card_qrcode,
            )

        self._stats.parse_ok += 1
        return result

    async def _download(
        self, info: VideoInfo, page: int, link: str
    ) -> tuple[Path | None, float | None, str | None]:
        """取直链并（按需）下载。

        返回 (本地文件, 超限体积MB, 直链)。三种失败语义：
        - 超限：返回真实体积 + 直链置空，避免绕过上限把大文件发出去；
        - 下载失败：仍返回直链，让协议端自己试一次（它的网络可能更顺）；
        - 取直链失败：全部为空。
        """
        session = await self._get_session()
        client = EbilibiliClient(
            session,
            timeout=self.request_timeout,
            retries=2,
            verbose=self.verbose_log,
            on_attempt=self._stats.note_media_attempt,
        )
        max_bytes = self.max_video_mb * 1024 * 1024
        cid = info.cid_for_page(page)

        async with self._semaphore:
            play = await client.resolve(bvid=info.bvid, cid=cid, original_link=link)
            if play is None:
                logger.warning(f"{LOG_TAG} {info.bvid} 未能取得下载直链")
                self._stats.download_fail += 1
                self._stats.note_error("下载站", f"{info.bvid} 三条路径均未取到直链")
                return None, None, None

            if self.video_send_mode == "url":
                # 直链模式不必落盘：交给协议端拉取，省一次中转流量
                logger.info(f"{LOG_TAG} 直链模式，跳过下载：{play.filename or info.bvid}")
                return None, None, play.url

            filename = sanitize_filename(play.filename or f"{info.title}.mp4")
            if not filename.lower().endswith(".mp4"):
                filename = f"{filename}.mp4"

            logger.info(f"{LOG_TAG} 开始下载 {info.bvid} P{page} -> {filename}")
            started = time.time()
            last_logged = 0.0  # 有总长时记百分比，未知总长时记已下载 MB

            def on_progress(downloaded: int, total: int | None) -> None:
                """按 20% 粒度记录进度。

                回调每个 1MB 数据块都会触发，不节流会在几秒内刷出上百条日志。
                """
                nonlocal last_logged
                if not self.verbose_log:
                    return

                if total:
                    percent = downloaded * 100 / total
                    if percent - last_logged < 20:
                        return
                    last_logged = percent
                    logger.info(
                        f"{LOG_TAG} 下载进度 {percent:.0f}%"
                        f"（{downloaded / 1024 / 1024:.1f}MB / {total / 1024 / 1024:.1f}MB）"
                    )
                else:
                    downloaded_mb = downloaded / 1024 / 1024
                    if downloaded_mb - last_logged < 20:
                        return
                    last_logged = downloaded_mb
                    logger.info(f"{LOG_TAG} 下载进度 已下载 {downloaded_mb:.1f}MB")

            try:
                path = await download_video(
                    session,
                    play.url,
                    self._tmp_dir,
                    filename,
                    max_bytes=max_bytes,
                    timeout=self.request_timeout,
                    on_progress=on_progress,
                )
            except VideoTooLarge as exc:
                logger.info(
                    f"{LOG_TAG} {info.bvid} 体积 {exc.size_bytes / 1024 / 1024:.1f}MB "
                    f"超过上限 {self.max_video_mb}MB，改为发送链接"
                )
                return None, exc.size_bytes / 1024 / 1024, None
            except DownloadError as exc:
                logger.warning(f"{LOG_TAG} {info.bvid} 下载失败：{exc}，改为尝试直链")
                self._stats.download_fail += 1
                self._stats.note_error("下载", f"{info.bvid}: {exc}")
                return None, None, play.url

            self._stats.download_ok += 1
            logger.info(
                f"{LOG_TAG} 下载完成 {path.name}（{path.stat().st_size / 1024 / 1024:.1f}MB，"
                f"耗时 {time.time() - started:.1f}s）"
            )
            return path, None, play.url

    # ──────────────────────────────────────────────── 事件入口

    @filter.regex(TRIGGER_REGEX)
    async def on_bili_link(self, event: AstrMessageEvent):
        """自动响应消息里的 B 站视频链接（纯文本路径）。"""
        ref = find_ref(self._message_text(event))
        if ref is None:
            return
        await self._handle_ref(event, ref)

    if _CARD_EVENT_TYPE is not None:

        @filter.event_message_type(_CARD_EVENT_TYPE)
        async def on_card_link(self, event: AstrMessageEvent):
            """QQ 小程序 / JSON 卡片分享的 B 站视频。

            链接藏在 json 消息段里，而 ``message_str`` 只拼接 Plain 段，
            ``@filter.regex`` 对它完全不匹配——所以单独开一个「接收所有消息」
            的入口兜底。纯文本已经能匹配时直接让路，避免同一条消息被处理两次。
            """
            if find_ref(self._message_text(event)) is not None:
                return
            card_texts = self._card_texts(event)
            if not card_texts:
                return
            for text in card_texts:
                ref = find_ref(text)
                if ref is not None:
                    await self._handle_ref(event, ref)
                    return
            logger.debug(f"{LOG_TAG} 卡片消息里没找到 B 站链接：{card_texts[0][:200]}")

    async def _handle_ref(self, event: AstrMessageEvent, ref: BiliRef) -> None:
        """解析并回复一条已识别的 B 站视频（两个入口共用）。"""
        if not self._allowed(event):
            return
        if self._cooldown_blocked(event):
            return
        if self._busy_blocked(_busy_key(ref)):
            await self._send_text(
                event,
                f"⏳ 这个视频 {self.duplicate_window_minutes} 分钟内已经处理过了，"
                f"换个视频或稍后再试～",
            )
            return

        # 已接管这条消息：阻止默认 LLM 回复重复应答
        event.stop_event()

        result: ProcessResult | None = None
        video_path: Path | None = None
        # 贴表情表示「已开始解析」；平台/协议端不支持时回退为原来的文字提示
        if not await self._react_parsing(event):
            await self._send_text(event, "🔍 正在解析视频信息…")
        try:
            result = await self._process(event, ref)

            # 关闭合并转发时「卡片先出」：解析一完成就把卡片发出去，
            # 用户不必干等视频下载。
            # 开启合并转发时不能这么做——视频要作为转发里的一个节点，
            # 必须等它下载完才能整包发出。
            if self.forward_threshold <= 0:
                await self._dispatch_messages(event, self._collect_cards(result))

            direct_url: str | None = None
            oversized_mb: float | None = None
            if self.send_video and result.info is not None:
                video_path, oversized_mb, direct_url = await self._download(
                    result.info, result.page, result.link
                )

            if self.forward_threshold > 0:
                # 卡片与视频一起交给转发逻辑（是否真的转发由 _should_forward 判定）
                outgoing = self._collect_messages(
                    result,
                    video_path=video_path,
                    direct_url=direct_url,
                    oversized_mb=oversized_mb,
                )
            else:
                # 卡片上面已经发过了，这里只补视频（或超限提示）
                outgoing = self._collect_media(
                    result,
                    video_path=video_path,
                    direct_url=direct_url,
                    oversized_mb=oversized_mb,
                )
            await self._dispatch_messages(event, outgoing)
        except BiliApiError as exc:
            self._stats.parse_fail += 1
            self._stats.note_error("解析", str(exc))
            logger.warning(f"{LOG_TAG} 解析失败：{exc}")
            await self._send_text(event, f"❌ 解析失败：{exc}")
        except Exception as exc:  # noqa: BLE001 - 单条消息出错不该影响插件整体
            self._stats.parse_fail += 1
            self._stats.note_error("处理", f"{type(exc).__name__}: {exc}")
            logger.error(f"{LOG_TAG} 处理链接异常：{type(exc).__name__}: {exc}", exc_info=True)
            await self._send_text(event, "❌ 处理视频时出错了，请稍后再试")
        finally:
            # 必须等所有发送都 await 结束才删除，否则协议端会读到"文件不存在"
            self._safe_unlink(video_path)
            if result is not None:
                self._safe_unlink(result.video_path)

    @filter.regex(DOWNLOAD_WORD_REGEX)
    async def on_download_word(self, event: AstrMessageEvent):
        """用户直接回复「下载」时，下载本会话最近解析过的视频。"""
        if not self._allowed(event, auto=False):
            return
        recent = self._recall(event.unified_msg_origin)
        if recent is None:
            return  # 没有上下文时不响应，把消息让给 LLM 正常对话

        event.stop_event()
        await self._download_and_reply(event, recent.info, recent.page, recent.link)

    @filter.command("bdl")
    async def on_bdl_command(self, event: AstrMessageEvent):
        """命令：/bdl [链接或BV号]；不带参数时下载本会话最近解析的视频。"""
        if not self._allowed(event, auto=False):
            return
        event.stop_event()

        ref = find_ref(self._message_text(event))
        if ref is not None:
            result: ProcessResult | None = None
            video_path: Path | None = None
            await self._send_text(event, "⬇️ 正在解析视频信息，请稍候…")
            try:
                result = await self._process(event, ref)

                # /bdl 的语义就是「要视频」，所以直接下载，再由 dispatch 统一发送
                direct_url: str | None = None
                oversized_mb: float | None = None
                if result.info is not None:
                    video_path, oversized_mb, direct_url = await self._download(
                        result.info, result.page, result.link
                    )

                outgoing = self._collect_messages(
                    result,
                    video_path=video_path,
                    direct_url=direct_url,
                    oversized_mb=oversized_mb,
                )
                await self._dispatch_messages(event, outgoing)
            except BiliApiError as exc:
                self._stats.parse_fail += 1
                self._stats.note_error("解析", str(exc))
                await self._send_text(event, f"❌ 解析失败：{exc}")
            except Exception as exc:  # noqa: BLE001
                self._stats.parse_fail += 1
                self._stats.note_error("下载流程", f"{type(exc).__name__}: {exc}")
                logger.error(
                    f"{LOG_TAG} 下载流程异常：{type(exc).__name__}: {exc}", exc_info=True
                )
                await self._send_text(event, "❌ 下载视频时出错了，请稍后再试")
            finally:
                self._safe_unlink(video_path)
                if result is not None:
                    self._safe_unlink(result.video_path)
            return

        recent = self._recall(event.unified_msg_origin)
        if recent is None:
            await self._send_text(
                event,
                "还没有解析过视频哦～ 先发送一个 B 站视频链接，或者用 /bdl <链接> 直接下载。",
            )
            return

        await self._download_and_reply(event, recent.info, recent.page, recent.link)

    async def _download_and_reply(
        self,
        event: AstrMessageEvent,
        info: VideoInfo,
        page: int,
        link: str,
        *,
        send_info: bool = True,
    ) -> None:
        """已解析过的视频：下载并发送。

        ``send_info=False`` 用于「解析后自动发视频」的流程——那时卡片已经发过，
        再补一条下载提示与信息卡片就是重复刷屏。
        """
        if send_info:
            page_tag = f" P{page}" if page > 1 else ""
            await self._send_text(
                event,
                f"⬇️ 正在下载《{info.title}》{page_tag}，体积上限 {self.max_video_mb}MB…",
            )

        path: Path | None = None
        try:
            path, oversized_mb, direct_url = await self._download(info, page, link)
            has_video = path is not None or bool(direct_url)

            if has_video:
                if send_info:
                    caption = build_caption(
                        title=info.title or "B站视频",
                        owner=info.owner,
                        duration=info.duration,
                        view_count=info.view,
                        page=page,
                        summary=None,
                        summary_basis=None,
                        web_url=None,
                        video_attached=True,
                    )
                    await self._send_text(event, caption)
                if not await self._send_video(event, path, direct_url):
                    await self._send_text(
                        event,
                        "⚠️ 视频发送失败，可用下载页链接自行下载："
                        f"{EbilibiliClient.web_url(info.bvid)}",
                    )
            elif oversized_mb is not None:
                await self._send_text(
                    event,
                    f"⚠️ 视频约 {oversized_mb:.0f} MB，超过体积上限 {self.max_video_mb} MB，未发送文件。\n"
                    f"可在面板调大「视频体积上限」，或直接访问：{EbilibiliClient.web_url(info.bvid)}",
                )
            else:
                await self._send_text(
                    event,
                    f"❌ 视频下载失败（下载站可能暂时不可用），可稍后重试。\n"
                    f"也可以直接访问：{EbilibiliClient.web_url(info.bvid)}",
                )
        finally:
            # 发送全部 await 完成后才删除临时文件
            self._safe_unlink(path)

    # ──────────────────────────────────────────────── 管理命令（仅管理员）

    # 说明：AstrBot 的指令不能带空格，因此无法写成 `/bdl status` 这种子命令；
    # 而指令组在无子指令时会报错并渲染指令树，会打破现有的 `/bdl`（无参下载）
    # 与 `/bdl <链接>` 两个用法。所以这里用平级命令 + 中文别名。
    @_admin_command("bdlstatus", "bdl状态")
    async def on_bdl_status(self, event: AstrMessageEvent):
        """查看插件运行状态（仅管理员）。"""
        event.stop_event()
        await self._send_text(event, await self._build_status_text(event))

    @_admin_command("bdlclear", "bdl清缓存")
    async def on_bdl_clear(self, event: AstrMessageEvent):
        """清空接口缓存（仅管理员）。"""
        event.stop_event()
        view_count = self._view_cache.clear()
        reply_count = self._reply_cache.clear()
        logger.info(f"{LOG_TAG} 缓存已清空：视频信息 {view_count} 条、热评 {reply_count} 条")
        await self._send_text(
            event, f"🧹 已清理缓存：视频信息 {view_count} 条、热评 {reply_count} 条"
        )

    @_admin_command("bdloff", "bdl关")
    async def on_bdl_off(self, event: AstrMessageEvent):
        """关闭本会话的自动解析（仅管理员）。"""
        event.stop_event()
        umo = event.unified_msg_origin
        if umo in self._disabled_sessions:
            await self._send_text(event, "ℹ️ 本会话的自动解析已经是关闭状态")
            return
        self._disabled_sessions.add(umo)
        logger.info(f"{LOG_TAG} 会话已关闭自动解析：{umo}")
        await self._send_text(
            event,
            "🔕 本会话已关闭自动解析\n"
            "（/bdl 等命令仍可用；发送 /bdlon 可恢复，插件重启后也会恢复为开启）",
        )

    @_admin_command("bdlon", "bdl开")
    async def on_bdl_on(self, event: AstrMessageEvent):
        """恢复本会话的自动解析（仅管理员）。"""
        event.stop_event()
        umo = event.unified_msg_origin
        if umo not in self._disabled_sessions:
            await self._send_text(event, "ℹ️ 本会话的自动解析本来就是开启状态")
            return
        self._disabled_sessions.discard(umo)
        logger.info(f"{LOG_TAG} 会话已恢复自动解析：{umo}")
        await self._send_text(event, "🔔 本会话已恢复自动解析")

    async def _build_status_text(self, event: AstrMessageEvent) -> str:
        """拼装 /bdlstatus 的输出：只读、不调 LLM、不含任何凭据。"""
        stats = self._stats.snapshot()
        view_stats = self._view_cache.stats()
        reply_stats = self._reply_cache.stats()

        # 模型可用性只查 provider id，不真的发起 LLM 请求（避免花钱、避免变慢）
        try:
            provider_id = await self.context.get_current_chat_provider_id(
                umo=event.unified_msg_origin
            )
        except Exception:  # noqa: BLE001 - 查不到就按未配置展示
            provider_id = None

        if not self.enable_summary:
            summary_state = "关闭"
        elif provider_id:
            summary_state = f"开启（当前会话模型：{provider_id}）"
        else:
            summary_state = "开启（当前会话未配置模型，将跳过）"

        if not self.enable_summary:
            subtitle_state = "未启用（总结已关闭）"
        elif not self.sessdata:
            subtitle_state = "关闭（未配置 sessdata）"
        elif not self.prefer_subtitle:
            subtitle_state = "关闭（已禁用字幕优先）"
        else:
            subtitle_state = f"开启（上限 {self.subtitle_max_chars} 字）"

        if not self.output_image:
            card_state = "关闭"
        elif self._card_disabled:
            card_state = "已熔断（连续渲染失败，改用纯文本）"
        else:
            card_state = "正常"

        lines = [
            "📊 bilibili-download 状态",
            f"版本：v{VERSION} ｜ 运行：{humanize_duration(stats['uptime'])}",
            f"触发范围：{self.trigger_scope} ｜ 需@：{'是' if self.need_at_in_group else '否'}"
            f" ｜ 白名单：{len(self.group_whitelist)} 个群"
            f" ｜ 用户冷却：{self.user_cooldown}s"
            f" ｜ 同视频去重：{self.duplicate_window_minutes}min",
            "本会话自动解析："
            + ("已关闭（/bdlon 恢复）" if event.unified_msg_origin in self._disabled_sessions else "开启"),
            f"解析：成功 {stats['parse_ok']} / 失败 {stats['parse_fail']}"
            f" ｜ 下载：成功 {stats['download_ok']} / 失败 {stats['download_fail']}",
            f"AI 总结：{summary_state}",
            f"字幕增强：{subtitle_state}",
            f"图片卡片：{card_state} ｜ 宽度：{self.card_width}px"
            f" ｜ 成功 {stats['card_ok']} / 失败 {stats['card_fail']}",
            f"热评展示：{'开' if self.show_replies else '关'}"
            f"（{self.replies_show_count} 条）"
            f" ｜ 合并转发阈值：{self.forward_threshold or '关'}",
            f"缓存：{view_stats['name']} {view_stats['size']}/{view_stats['max_items']}"
            f"（命中 {view_stats['hits']}）｜ {reply_stats['name']} {reply_stats['size']}"
            f"/{reply_stats['max_items']}（命中 {reply_stats['hits']}）",
            self._format_media_line(stats["media_attempts"]),
        ]

        last_error = stats["last_error"]
        if last_error is not None:
            detail = short_text(last_error.detail, 60)
            lines.append(
                f"最近错误：{last_error.name} · {detail}（{humanize_age(last_error.at)}）"
            )

        return "\n".join(lines)

    @staticmethod
    def _format_media_line(attempts: dict) -> str:
        """把下载站三条路径的最近状态拼成一行；顺序固定，便于对比。"""
        parts: list[str] = []
        for name in ("直链接口", "表单页", "解析页"):
            attempt = attempts.get(name)
            if attempt is None:
                parts.append(f"{name} 未使用")
            else:
                state = "成功" if attempt.ok else "失败"
                parts.append(f"{name} {state}（{humanize_age(attempt.at)}）")
        return "下载站：" + " ｜ ".join(parts)

    # ──────────────────────────────────────────────── 生命周期

    async def terminate(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()
        cleanup_old_files(self._tmp_dir, 24)
        logger.info(f"{LOG_TAG} 插件已卸载")
