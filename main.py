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
from dataclasses import dataclass
from pathlib import Path

import aiohttp

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from . import api
from .core import platforms
from .core.bili_api import REPLY_MAX_CHARS
from .core.card import (
    render_card,
    render_replies_card,
    replies_template_available,
    template_available,
    template_path,
)
from .core.constants import LOG_TAG, PLUGIN_NAME, VERSION
from .core.bili_login import BiliQrLogin
from .core.credentials import CredentialStore
from .core.runtime import RUNTIME_DEFAULTS, RUNTIME_SPEC, RuntimeStore
from .core.dispatch import DispatchMixin, ProcessResult
from .core.downloader import DownloadError, VideoTooLarge, cleanup_old_files, download_video
from .core.formatting import (
    build_caption,
    humanize_age,
    humanize_duration,
    sanitize_filename,
    short_text,
)
from .core.history import HistoryEntry, HistoryStore
from .core.link_parser import DOWNLOAD_WORD_REGEX, url_from_card
from .core.media import CAP_REPLIES, CAP_SUMMARY, MediaInfo, MediaRef
from .core.platforms import ParseHint, PlatformError
from .core.stats import RuntimeStats
from .core.summarizer import summarize_video

DATA_DIR_NAME = "astrbot_plugin_bilibilidownload"

# 「接收所有消息」的事件类型，用于 QQ 小程序 / JSON 卡片的兜底入口。
# AstrBot 的 message_str 只拼接 Plain 段，卡片里的链接进不去，@filter.regex 匹配不到。
# 若某个版本的 AstrBot 没有 EventMessageType，这里得到 None，该入口不注册
# （纯文本路径不受影响，属安全降级）。
_CARD_EVENT_TYPE = getattr(getattr(filter, "EventMessageType", None), "ALL", None)

# 「下载」关键词触发时，多久之内解析过的视频还算"当前视频"
RECENT_TTL = 30 * 60

# 卡片渲染连续失败多少次后，本进程内不再尝试（见 _render_card）
CARD_FAIL_LIMIT = 2

# 平台必须在**类定义之前**加载完成：下面的 @filter.regex 需要合并后的触发正则，
# 而那个正则由各平台自己声明（见 core/platforms/registry.combined_trigger_regex）。
# 加载顺序也决定了"同一段文本先问哪个平台"，用户可以用 enabled_platforms 调顺序。
platforms.load_builtin()

# 所有平台触发正则的并集。正则只负责"这条消息有没有可能是媒体链接"，
# 真正是否处理由各平台 match() 决定——所以宽一点没有副作用。
_TRIGGER_REGEX = platforms.combined_trigger_regex()


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
    """会话内最近一次解析结果，供「下载」关键词与 /bdl 复用。

    存的是跨平台 ``MediaInfo``：抖音/小红书解析完同样能被「下载」复用，
    编排层不需要为每个平台各存一份。
    """

    info: MediaInfo
    at: float


class BilibiliDownloadPlugin(DispatchMixin, Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # 面板接口的路由前缀用这个名字（register_web_api 的约定）
        self.plugin_name = PLUGIN_NAME

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
        # 兜底值必须与 _conf_schema.json 的 default 一致（40）：实测协议端上传群视频
        # 在 41MB 处稳定失败，兜底写成 50 会让老配置悄悄踩回那个坑。
        self.max_video_mb = max(1, self._conf_int("max_video_mb", 40))
        self.request_timeout = max(5, self._conf_int("request_timeout", 20))
        self.max_concurrent = max(1, self._conf_int("max_concurrent", 1))
        self.user_cooldown = max(0, self._conf_int("user_cooldown", 60))
        # 相同视频（归一化后的视频标识）在多少分钟内只处理一次；0 = 不做这层限制
        self.duplicate_window_minutes = max(
            0, self._conf_int("duplicate_window_minutes", 10)
        )
        self.group_whitelist = self._conf_list("group_whitelist")
        # 启用的平台；留空 = 全部启用（与 group_whitelist 留空 = 所有群 同一种直觉）
        self.enabled_platforms = self._conf_list("enabled_platforms")
        # 面板「恢复」时要能退回到这里，而不是停在面板上次写的值
        self._config_platforms = list(self.enabled_platforms)
        # 面板「最近」保留多少条解析记录；0 = 不记录
        self.history_keep = max(0, self._conf_int("history_keep", 50))
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
        # key 是归一化后的媒体标识（见 MediaRef.dedup_key），value 是开始处理的时间戳
        self._busy: dict[str, float] = {}

        # 跨会话共享的接口缓存由各平台自己持有（缓存的内容是平台专属的：
        # B 站缓存视频信息与热评，抖音缓存的是分享页 JSON）。
        # 这里只维护平台实例，按需懒构造，config 改了重载插件即可。
        self._platforms: dict[str, platforms.BasePlatform] = {}

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

        # 面板「最近」页的数据：定长环形记录，落盘在数据目录里（插件升级不丢）
        self.history = HistoryStore(self._data_dir / "history.json", keep=self.history_keep)
        # 平台凭据（Cookie / SESSDATA / 第三方接口地址）：单独一个文件，权限 600
        self.credentials = CredentialStore(self._data_dir / "credentials.json")
        # 面板里的运行期调整：覆盖 AstrBot 配置的同名项，不写回配置文件（见 core/runtime.py）
        self.runtime = RuntimeStore(self._data_dir / "runtime.json")
        # 图文笔记的发送上限（小红书这类），面板可调
        self.xhs_send_images = True
        self.xhs_max_images = 5
        self._apply_runtime()

        # B 站扫码登录：面板里生成二维码，确认后自动把 SESSDATA 写进凭据文件。
        # 登录成功后要丢掉内存里的 B 站实例，否则它还在用旧的（没登录的）cookie。
        self.bili_login = BiliQrLogin(
            self.credentials,
            on_success=lambda _username, _uid: self._platforms.pop("bilibili", None),
        )

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
            f"合并转发阈值={self.forward_threshold or '关'}，"
            f"平台={'/'.join(platforms.list_platform_names()) or '无'}）"
        )

        # 面板接口最后注册：低版本 AstrBot 没有 register_web_api 时只少一层壳，
        # 插件功能不受影响（api.register 内部逐个 try/except）
        api.register(self)

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

    # ── 运行期调整（面板可写） ──────────────────────────────────────────

    def _apply_runtime(self) -> None:
        """把 ``data/runtime.json`` 的覆盖盖到运行属性上——**每次从基线重算**。

        不能写成「有覆盖才赋值」：点面板的「恢复」只是删掉那条覆盖，属性会停在
        上一次面板写的数上，而界面还显示「来自配置」（早期实现就是这个 bug，
        `xhs_max_images` 这类不在 `_conf_schema.json` 里的键尤其明显）。

        优先级：面板覆盖 → AstrBot 配置 → ``RUNTIME_DEFAULTS``（代码默认）。
        """
        store = self.runtime
        conf = self.config if isinstance(self.config, dict) else {}

        def pick(key: str):
            if store.has(key):
                return store.get(key)
            value = conf.get(key)
            return RUNTIME_DEFAULTS.get(key) if value is None else value

        self.send_video = bool(pick("send_video"))
        self.max_video_mb = max(1, int(pick("max_video_mb")))
        self.video_send_mode = str(pick("video_send_mode") or "auto")
        self.send_ebilibili_link = bool(pick("send_ebilibili_link"))
        self.output_image = bool(pick("output_image"))
        self.card_width = max(560, min(1000, int(pick("card_width"))))
        self.card_qrcode = bool(pick("card_qrcode"))
        self.show_replies = bool(pick("show_replies"))
        self.replies_show_count = max(0, int(pick("replies_show_count")))
        self.reply_limit = max(0, int(pick("reply_limit")))
        self.enable_summary = bool(pick("enable_summary"))
        self.summary_max_chars = max(50, int(pick("summary_max_chars")))
        self.prefer_subtitle = bool(pick("prefer_subtitle"))
        self.subtitle_max_chars = max(500, int(pick("subtitle_max_chars")))
        self.xhs_send_images = bool(pick("xhs_send_images"))
        self.xhs_max_images = max(1, min(35, int(pick("xhs_max_images"))))

        # 平台启停：面板存过就用面板的，否则退回插件配置里的原始列表
        overridden = store.platforms()
        self.enabled_platforms = (
            overridden if overridden is not None else list(self._config_platforms)
        )

    def _runtime_settings(self) -> dict:
        """交给平台的运行期键（平台自己读 settings，不碰这里的主流程属性）。"""
        return {key: value for key, value in self.runtime.snapshot().items() if key in RUNTIME_SPEC}

    def apply_runtime(self, values: dict) -> tuple[list[str], list[str]]:
        """面板保存调整项。返回 (已应用键, 被拒键)。"""
        applied, rejected = self.runtime.set_many(values)
        if applied:
            self._apply_runtime()
            # 只在这些键变化时重建平台实例：它们会进 settings 或影响平台可用性。
            # 其余键（卡片宽度、热评条数……）只影响发送侧，没必要清掉平台缓存。
            if {"xhs_send_images", "xhs_max_images", "enabled_platforms"} & set(applied):
                self._platforms.clear()
            logger.info(f"{LOG_TAG} 面板已应用运行期调整：{', '.join(applied)}")
        return applied, rejected

    def reload_platforms(self) -> None:
        """丢掉所有平台实例，下次解析时按最新凭据/设置重建。"""
        self._platforms.clear()

    async def _platform(self, name: str) -> "platforms.BasePlatform | None":
        """按需构造并复用平台实例。

        平台实例自带缓存与凭据（B 站的 sessdata 只在 B 站实例里），所以必须复用，
        不能每个请求新建——否则缓存永远是冷的。session 被重建过（插件重载）时
        旧实例里的 session 已失效，这里顺手重建。
        """
        inst = self._platforms.get(name)
        if inst is not None and inst.session is self._session:
            return inst
        if inst is not None:
            self._platforms.pop(name, None)

        cls = platforms.get(name)
        if cls is None:
            logger.warning(f"{LOG_TAG} 未知平台 {name}，请检查 enabled_platforms 配置")
            return None

        session = await self._get_session()
        try:
            # 运行期覆盖（面板里的调整）也要进平台：图文上限这类参数是平台自己读的
            settings = dict(self.config)
            settings.update(self._runtime_settings())
            inst = cls.create(session, settings=settings)
            # 面板里保存的凭据优先于插件配置（凭据文件是可随时改的覆盖层）
            inst.apply_credentials(self.credentials.platform(name))
        except Exception as exc:  # noqa: BLE001 - 单平台构造失败不该影响其它平台
            logger.warning(f"{LOG_TAG} 平台 {name} 初始化失败：{type(exc).__name__}: {exc}")
            return None

        self._platforms[name] = inst
        return inst

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
        无关链接，而 ``platforms.match`` 只认第一个命中的平台与链接。

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

        判定依据是归一化后的媒体标识（见 `MediaRef.dedup_key`），所以不同用户发同一个链接
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

        短链在 `dedup_key` 里只能用自己的原串（那时还没解析出真实 ID），
        拿到 ID 后补登一次，才能让「短链 ↔ 长链」指向同一内容时互相拦住。
        用 setdefault 保留原时间戳，避免刷新窗口起点。
        """
        if self.duplicate_window_minutes <= 0:
            return
        self._busy.setdefault(key, time.time())

    def _remember(self, umo: str, info: MediaInfo) -> None:
        self._recent[umo] = RecentVideo(info=info, at=time.time())

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

    async def _render_card(
        self, info: MediaInfo, summary: str | None, basis: str | None
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

    async def _process(self, event: AstrMessageEvent, ref: MediaRef) -> ProcessResult:
        """完成一次「解析 → 总结 → 渲染卡片」。

        **这里不下载媒体**：调用方先把卡片发出去，再按需下载发送（见 _handle_ref）。
        否则用户要等下载完成才能看到解析结果，白白多等几秒到几十秒。

        这里刻意不出现任何平台名：解析交给 ``ref.platform`` 对应的平台实例，
        需要哪些总结信号由 ``ParseHint`` 告诉平台，编排层只管拼回复。
        """
        umo = event.unified_msg_origin
        platform = await self._platform(ref.platform)
        if platform is None:
            raise PlatformError(f"平台 {ref.platform} 未启用或加载失败")

        # 平台自己决定用得上哪些信号：B 站会去取热评/标签/字幕，抖音只拿文案。
        # want_subtitle 这里恒为 True —— 到底要不要字幕由平台按自己的凭据配置判断
        # （B 站看 sessdata 与 prefer_subtitle），编排层不该认识"凭据"这种概念。
        hint = ParseHint(
            reply_limit=self.reply_limit,
            want_summary=self.enable_summary,
            want_subtitle=True,
        )
        media = await platform.parse(ref, hint=hint)

        # 补登记真实 ID：短链在匹配阶段只能用自己的原串，这里补上才能与长链互通
        self._remember_busy(media.dedup_key)
        self._remember(umo, media)

        link = media.source_url or media.web_url
        logger.info(
            f"{LOG_TAG} 解析成功：{media.platform}/{media.media_id} "
            f"《{media.title}》 P{media.page}"
        )

        replies = media.extra("replies") or []
        summary: str | None = None
        basis: str | None = None
        if self.enable_summary and platform.can(CAP_SUMMARY):
            summary, basis = await summarize_video(
                self.context,
                umo=umo,
                media=media,
                # 喂 AI 时再截到 REPLY_MAX_CHARS：卡片展示用的是完整正文
                # （REPLY_DISPLAY_MAX_CHARS），这里截断只影响 token，不影响图片
                replies=[reply.content[:REPLY_MAX_CHARS] for reply in replies] or None,
                max_chars=self.summary_max_chars,
                timeout=self.summary_timeout,
            )

        web_url = platform.web_url(media) if self.send_ebilibili_link else ""
        result = ProcessResult(info=media, page=media.page, link=link, web_url=web_url)
        result.caption = build_caption(
            title=media.title or "视频",
            owner=media.author,
            duration=media.duration,
            view_count=media.stat("view"),
            page=media.page,
            summary=summary,
            summary_basis=basis,
            web_url=web_url or None,
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
        result.card_url = await self._render_card(media, summary, basis)

        # 热评单独成图：只有声明了 CAP_REPLIES 的平台（当前是 B 站）才有这一张
        if (
            self.output_image
            and self.show_replies
            and replies
            and self.replies_show_count > 0
            and platform.can(CAP_REPLIES)
        ):
            result.replies_card_url = await render_replies_card(
                self,
                await self._get_session(),
                info=media,
                replies=replies,
                show_count=self.replies_show_count,
                card_width=self.card_width,
                page=media.page,
                with_qrcode=self.card_qrcode,
            )

        self._stats.parse_ok += 1
        return result

    async def _download(
        self, media: MediaInfo
    ) -> tuple[Path | None, float | None, str | None]:
        """取直链并（按需）下载。

        返回 (本地文件, 超限体积MB, 直链)。三种失败语义：
        - 超限：返回真实体积 + 直链置空，避免绕过上限把大文件发出去；
        - 下载失败：仍返回直链，让协议端自己试一次（它的网络可能更顺）；
        - 取直链失败：全部为空。

        「怎么取直链」由平台实现：B 站要过 ebilibili 三道降级，抖音/小红书
        解析完就带着 CDN 直链。这里只负责下载、体积上限与临时文件。
        """
        platform = await self._platform(media.platform)
        if platform is None:
            return None, None, None

        session = await self._get_session()
        max_bytes = self.max_video_mb * 1024 * 1024

        async with self._semaphore:
            try:
                items = await platform.resolve_media(
                    media, on_attempt=self._stats.note_media_attempt
                )
            except PlatformError as exc:
                logger.warning(f"{LOG_TAG} {media.media_id} 未能取得直链：{exc}")
                self._stats.download_fail += 1
                self._stats.note_error("取直链", f"{media.media_id}: {exc}")
                return None, None, None

            video = next((item for item in items if item.is_video and item.url), None)
            if video is None:
                # 图文笔记本来就没有视频，这不是失败：图片由 _collect_gallery 直接发
                if any(item.kind == "image" for item in items):
                    logger.info(f"{LOG_TAG} {media.media_id} 是图文内容，跳过视频下载")
                    return None, None, None
                logger.warning(f"{LOG_TAG} {media.media_id} 平台未返回可下载的视频直链")
                self._stats.download_fail += 1
                self._stats.note_error("取直链", f"{media.media_id}: 无视频直链")
                return None, None, None

            if self.video_send_mode == "url":
                # 直链模式不必落盘：交给协议端拉取，省一次中转流量
                logger.info(
                    f"{LOG_TAG} 直链模式，跳过下载：{video.filename or media.media_id}"
                )
                return None, None, video.url

            filename = sanitize_filename(video.filename or f"{media.title}.mp4")
            if not filename.lower().endswith(".mp4"):
                filename = f"{filename}.mp4"

            # 各平台防盗链要求不同（B 站校验 UA+Referer，抖音只认移动端 UA），
            # 由 MediaItem 带过来，下载器与默认浏览器头合并
            headers = dict(video.headers or {})
            if video.referer:
                headers.setdefault("Referer", video.referer)

            logger.info(f"{LOG_TAG} 开始下载 {media.media_id} P{media.page} -> {filename}")
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
                    video.url,
                    self._tmp_dir,
                    filename,
                    max_bytes=max_bytes,
                    timeout=self.request_timeout,
                    on_progress=on_progress,
                    headers=headers,
                )
            except VideoTooLarge as exc:
                logger.info(
                    f"{LOG_TAG} {media.media_id} 体积 {exc.size_bytes / 1024 / 1024:.1f}MB "
                    f"超过上限 {self.max_video_mb}MB，改为发送链接"
                )
                return None, exc.size_bytes / 1024 / 1024, None
            except DownloadError as exc:
                logger.warning(f"{LOG_TAG} {media.media_id} 下载失败：{exc}，改为尝试直链")
                self._stats.download_fail += 1
                self._stats.note_error("下载", f"{media.media_id}: {exc}")
                return None, None, video.url

            self._stats.download_ok += 1
            logger.info(
                f"{LOG_TAG} 下载完成 {path.name}（{path.stat().st_size / 1024 / 1024:.1f}MB，"
                f"耗时 {time.time() - started:.1f}s）"
            )
            return path, None, video.url

    # ──────────────────────────────────────────────── 事件入口

    @filter.regex(_TRIGGER_REGEX)
    async def on_bili_link(self, event: AstrMessageEvent):
        """自动响应消息里的媒体链接（纯文本路径）。

        正则只负责粗筛，真正的平台判定交给 ``platforms.match``：它会按
        ``enabled_platforms`` 的顺序依次询问各平台，第一个认出来的胜出。
        """
        hit = platforms.match(self._message_text(event), enabled=self.enabled_platforms)
        if hit is None:
            return
        await self._handle_ref(event, hit[1])

    if _CARD_EVENT_TYPE is not None:

        @filter.event_message_type(_CARD_EVENT_TYPE)
        async def on_card_link(self, event: AstrMessageEvent):
            """QQ 小程序 / JSON 卡片分享的 B 站视频。

            链接藏在 json 消息段里，而 ``message_str`` 只拼接 Plain 段，
            ``@filter.regex`` 对它完全不匹配——所以单独开一个「接收所有消息」
            的入口兜底。纯文本已经能匹配时直接让路，避免同一条消息被处理两次。
            """
            if platforms.match(self._message_text(event), enabled=self.enabled_platforms) is not None:
                return
            card_texts = self._card_texts(event)
            if not card_texts:
                return
            for text in card_texts:
                hit = platforms.match(text, enabled=self.enabled_platforms)
                if hit is not None:
                    await self._handle_ref(event, hit[1])
                    return
            logger.debug(f"{LOG_TAG} 卡片消息里没找到可解析的链接：{card_texts[0][:200]}")

    async def _handle_ref(self, event: AstrMessageEvent, ref: MediaRef) -> None:
        """解析并回复一条已识别的媒体引用（两个入口共用，平台无关）。"""
        if not self._allowed(event):
            return
        if self._cooldown_blocked(event):
            return
        if self._busy_blocked(ref.dedup_key):
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
        started = time.time()
        try:
            result = await self._process(event, ref)
            # 解析成功即记一条历史：后面的发送/下载失败不影响"这条链接能解析"这个事实
            self._record_history(event, ref, ok=True, started=started, result=result)

            # 关闭合并转发时「卡片先出」：解析一完成就把卡片发出去，
            # 用户不必干等视频下载。
            # 开启合并转发时不能这么做——视频要作为转发里的一个节点，
            # 必须等它下载完才能整包发出。
            if self.forward_threshold <= 0:
                await self._dispatch_messages(event, self._collect_cards(result))

            direct_url: str | None = None
            oversized_mb: float | None = None
            if self.send_video and result.info is not None:
                video_path, oversized_mb, direct_url = await self._download(result.info)

            # 图集：与视频并行存在（图文笔记没有视频，视频笔记也可能带图封面）
            if (
                result.info is not None
                and self.xhs_send_images
                and result.info.image_items()
            ):
                result.gallery_paths = await self._download_gallery(
                    result.info, self.xhs_max_images
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
        except PlatformError as exc:
            # 失败必须回收去重窗口。否则一次 412 / 网络抖动之后，用户等半天的重试
            # 会被判成「这个视频 10 分钟内已经处理过了」——而第一次其实什么都没发出去。
            self._busy.pop(ref.dedup_key, None)
            self._stats.parse_fail += 1
            self._stats.note_error("解析", exc.detail or str(exc))
            self._record_history(
                event, ref, ok=False, started=started, detail=str(exc)
            )
            logger.warning(f"{LOG_TAG} 解析失败：{exc}（detail: {exc.detail}）")
            await self._send_text(event, f"❌ 解析失败：{exc}")
        except Exception as exc:  # noqa: BLE001 - 单条消息出错不该影响插件整体
            self._busy.pop(ref.dedup_key, None)
            self._stats.parse_fail += 1
            self._stats.note_error("处理", f"{type(exc).__name__}: {exc}")
            self._record_history(
                event, ref, ok=False, started=started,
                detail=f"{type(exc).__name__}: {exc}",
            )
            logger.error(f"{LOG_TAG} 处理链接异常：{type(exc).__name__}: {exc}", exc_info=True)
            await self._send_text(event, "❌ 处理视频时出错了，请稍后再试")
        finally:
            # 必须等所有发送都 await 结束才删除，否则协议端会读到"文件不存在"
            self._safe_unlink(video_path)
            if result is not None:
                self._safe_unlink(result.video_path)
                for gallery_path in result.gallery_paths or []:
                    self._safe_unlink(gallery_path)

    def _record_history(
        self,
        event: AstrMessageEvent,
        ref: MediaRef,
        *,
        ok: bool,
        started: float,
        detail: str = "",
        result: ProcessResult | None = None,
    ) -> None:
        """写一条解析记录（面板「最近」页的数据源）。

        只记"解析"这一步：发送与下载的成败由运行统计体现，混进历史会让
        "这条链接能不能解析"这个信号变模糊。
        """
        if not self.history.enabled:
            return
        info = result.info if result is not None else None
        self.history.add(
            HistoryEntry(
                platform=info.platform if info is not None else ref.platform,
                media_id=(
                    info.media_id if info is not None else (ref.media_id or ref.raw)
                )[:80],
                title=(info.title if info is not None else "")[:120],
                page=info.page if info is not None else ref.page,
                ok=ok,
                detail=short_text(detail, 160),
                ms=int((time.time() - started) * 1000),
                card_ok=bool(result is not None and result.card_url),
                umo=event.unified_msg_origin,
            )
        )

    @filter.regex(DOWNLOAD_WORD_REGEX)
    async def on_download_word(self, event: AstrMessageEvent):
        """用户直接回复「下载」时，下载本会话最近解析过的视频。"""
        if not self._allowed(event, auto=False):
            return
        recent = self._recall(event.unified_msg_origin)
        if recent is None:
            return  # 没有上下文时不响应，把消息让给 LLM 正常对话

        event.stop_event()
        await self._download_and_reply(event, recent.info)

    @filter.command("bdl")
    async def on_bdl_command(self, event: AstrMessageEvent):
        """命令：/bdl [链接或BV号]；不带参数时下载本会话最近解析的视频。"""
        if not self._allowed(event, auto=False):
            return
        event.stop_event()

        hit = platforms.match(self._message_text(event), enabled=self.enabled_platforms)
        if hit is not None:
            ref = hit[1]
            result: ProcessResult | None = None
            video_path: Path | None = None
            await self._send_text(event, "⬇️ 正在解析视频信息，请稍候…")
            try:
                result = await self._process(event, ref)

                # /bdl 的语义就是「要视频」，所以直接下载，再由 dispatch 统一发送
                direct_url: str | None = None
                oversized_mb: float | None = None
                if result.info is not None:
                    video_path, oversized_mb, direct_url = await self._download(result.info)

                outgoing = self._collect_messages(
                    result,
                    video_path=video_path,
                    direct_url=direct_url,
                    oversized_mb=oversized_mb,
                )
                await self._dispatch_messages(event, outgoing)
            except PlatformError as exc:
                self._busy.pop(ref.dedup_key, None)
                self._stats.parse_fail += 1
                self._stats.note_error("解析", exc.detail or str(exc))
                await self._send_text(event, f"❌ 解析失败：{exc}")
            except Exception as exc:  # noqa: BLE001
                self._busy.pop(ref.dedup_key, None)
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
                "还没有解析过视频哦～ 先发送一个视频链接，或者用 /bdl <链接> 直接下载。",
            )
            return

        await self._download_and_reply(event, recent.info)

    async def _download_gallery(
        self, info: MediaInfo, limit: int
    ) -> list[Path | None]:
        """把图集的前 ``limit`` 张图下载到本地临时目录。

        返回值与 ``info.image_items()`` 顺序一一对应，下载失败的为 ``None``
        —— 发送时会退回原始 URL，一张失败不该让整组图都发不出去。

        为什么要落盘而不是直接发 URL：图文笔记的图来自各家 CDN（小红书
        `sns-img-qc`、抖音 `p3-sign`），都有防盗链。让协议端回源拉图，
        遇到 Referer 校验或慢 CDN 就是裂图；本地下载一次，发送侧只管发文件。

        ``extras["image_fallbacks"]`` 是同序的备用地址（小红书用它做
        「无水印 CDN 取不到 → 退回带水印签名地址」的兜底）。
        """
        images = info.image_items()[: max(0, limit)]
        if not images:
            return []

        session = await self._get_session()
        fallbacks = list((info.extras or {}).get("image_fallbacks") or [])
        # 单张图的上限放宽到 20MB：图片本来就不会触发视频那个 40MB 上限，
        # 但也不能完全不设——畸形响应会把磁盘写满。
        max_bytes = max(self.max_video_mb, 20) * 1024 * 1024
        paths: list[Path | None] = []
        saved_count = 0

        for index, item in enumerate(images):
            candidates = [item.url]
            if index < len(fallbacks) and fallbacks[index]:
                candidates.append(fallbacks[index])

            headers = dict(item.headers or {})
            if item.referer:
                headers.setdefault("Referer", item.referer)

            saved: Path | None = None
            for attempt, url in enumerate(candidates):
                if not url:
                    continue
                filename = f"img_{int(time.time() * 1000)}_{index}_{attempt}.jpg"
                try:
                    saved = await download_video(
                        session,
                        url,
                        self._tmp_dir,
                        filename,
                        max_bytes=max_bytes,
                        timeout=self.request_timeout,
                        headers=headers,
                    )
                    break
                except Exception as exc:  # noqa: BLE001 - 换下一个候选地址
                    logger.debug(
                        f"{LOG_TAG} 图集第 {index + 1} 张下载失败（第 {attempt + 1} 个地址）："
                        f"{type(exc).__name__}: {exc}"
                    )
            if saved is not None:
                saved_count += 1
            paths.append(saved)

        logger.info(f"{LOG_TAG} 图集下载完成：{saved_count}/{len(images)} 张")
        return paths

    async def _download_and_reply(
        self,
        event: AstrMessageEvent,
        info: MediaInfo,
        *,
        send_info: bool = True,
    ) -> None:
        """已解析过的视频：下载并发送。

        ``send_info=False`` 用于「解析后自动发视频」的流程——那时卡片已经发过，
        再补一条下载提示与信息卡片就是重复刷屏。
        """
        if send_info:
            await self._send_text(
                event,
                f"⬇️ 正在下载《{info.title}》{info.page_suffix}，"
                f"体积上限 {self.max_video_mb}MB…",
            )

        platform = await self._platform(info.platform)
        web_url = platform.web_url(info) if platform else info.web_url

        path: Path | None = None
        try:
            path, oversized_mb, direct_url = await self._download(info)
            has_video = path is not None or bool(direct_url)

            if has_video:
                if send_info:
                    caption = build_caption(
                        title=info.title or "视频",
                        owner=info.author,
                        duration=info.duration,
                        view_count=info.stat("view"),
                        page=info.page,
                        summary=None,
                        summary_basis=None,
                        web_url=None,
                        video_attached=True,
                    )
                    await self._send_text(event, caption)
                if not await self._send_video(event, path, direct_url):
                    tip = "⚠️ 视频发送失败，可用下载页链接自行下载"
                    await self._send_text(
                        event, f"{tip}：{web_url}" if web_url else f"{tip}。"
                    )
            elif oversized_mb is not None:
                tip = (
                    f"⚠️ 视频约 {oversized_mb:.0f} MB，超过体积上限 "
                    f"{self.max_video_mb} MB，未发送文件。"
                )
                await self._send_text(
                    event,
                    f"{tip}\n可在面板调大「视频体积上限」，或直接访问：{web_url}"
                    if web_url
                    else tip,
                )
            else:
                tip = "❌ 视频下载失败（下载站可能暂时不可用），可稍后重试。"
                await self._send_text(
                    event, f"{tip}\n也可以直接访问：{web_url}" if web_url else tip
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
        """清空接口缓存（仅管理员）。

        缓存由各平台自己持有，所以这里逐个平台清——新增平台不需要改这条命令。
        """
        event.stop_event()
        parts: list[str] = []
        total = 0
        for name, inst in list(self._platforms.items()):
            clear = getattr(inst, "clear_cache", None)
            if not callable(clear):
                continue
            count = int(clear() or 0)
            total += count
            parts.append(f"{inst.display or name} {count} 条")
        logger.info(f"{LOG_TAG} 缓存已清空：{'、'.join(parts) if parts else '无可清缓存'}")
        await self._send_text(
            event,
            f"🧹 已清理缓存：{'、'.join(parts)}" if parts else "🧹 当前没有可清理的缓存",
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

        # 缓存由各平台自己持有；还没解析过任何内容时列表为空，这里如实说明
        cache_parts: list[str] = []
        for inst in self._platforms.values():
            snapshot = getattr(inst, "cache_snapshot", None)
            if not callable(snapshot):
                continue
            for cache_name, cache_stats in (snapshot() or {}).items():
                cache_parts.append(
                    f"{cache_name} {cache_stats['size']}/{cache_stats['max_items']}"
                    f"（命中 {cache_stats['hits']}）"
                )
        cache_line = (
            "缓存：" + " ｜ ".join(cache_parts) if cache_parts else "缓存：暂无（尚未解析）"
        )

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
            f"字幕增强（仅 B 站）：{subtitle_state}",
            f"图片卡片：{card_state} ｜ 宽度：{self.card_width}px"
            f" ｜ 成功 {stats['card_ok']} / 失败 {stats['card_fail']}",
            f"热评展示：{'开' if self.show_replies else '关'}"
            f"（{self.replies_show_count} 条）"
            f" ｜ 合并转发阈值：{self.forward_threshold or '关'}",
            self._format_platform_line(),
            cache_line,
            self._format_media_line(stats["media_attempts"]),
        ]

        last_error = stats["last_error"]
        if last_error is not None:
            detail = short_text(last_error.detail, 60)
            lines.append(
                f"最近错误：{last_error.name} · {detail}（{humanize_age(last_error.at)}）"
            )

        return "\n".join(lines)

    def _format_platform_line(self) -> str:
        """把各平台状态拼成一行：启用了哪些、没启用哪些、哪些加载失败。

        加载失败的平台必须点出来——否则用户只看到"某平台不响应"，却不知道是
        模块没起来（例如可选依赖缺失），排查方向会被带偏。
        """
        rows = platforms.describe_all(self.enabled_platforms)
        on = [row["display"] for row in rows if row.get("enabled")]
        off = [
            row["display"] for row in rows if not row.get("enabled") and not row.get("error")
        ]
        failed = [row["display"] for row in rows if row.get("error")]
        parts = ["启用：" + ("、".join(on) if on else "无")]
        if off:
            parts.append("未启用：" + "、".join(off))
        if failed:
            parts.append("加载失败：" + "、".join(failed))
        return "平台 " + " ｜ ".join(parts)

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
        # 平台实例里持有缓存与 session 引用，先放掉再关 session
        self._platforms.clear()
        if self._session is not None and not self._session.closed:
            await self._session.close()
        cleanup_old_files(self._tmp_dir, 24)
        logger.info(f"{LOG_TAG} 插件已卸载")
