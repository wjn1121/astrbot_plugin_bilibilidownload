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
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import aiohttp

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .core.bili_api import BiliApi, BiliApiError, VideoInfo
from .core.downloader import DownloadError, VideoTooLarge, cleanup_old_files, download_video
from .core.ebilibili import EbilibiliClient
from .core.formatting import build_caption, sanitize_filename
from .core.link_parser import (
    DOWNLOAD_WORD_REGEX,
    TRIGGER_REGEX,
    BiliRef,
    aid_from_url,
    bvid_from_url,
    find_ref,
)
from .core.summarizer import summarize_video

DATA_DIR_NAME = "astrbot_plugin_bilibilidownload"
LOG_TAG = "[bilibili-download]"

# 「下载」关键词触发时，多久之内解析过的视频还算"当前视频"
RECENT_TTL = 30 * 60


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
    video_path: Path | None = None  # 已下载到本地的视频（url 模式为 None）
    direct_url: str | None = None  # ebilibili 直链，供协议端自行拉取
    oversized_mb: float | None = None  # 超过体积上限时的真实体积

    @property
    def has_video(self) -> bool:
        return self.video_path is not None or bool(self.direct_url)


class BilibiliDownloadPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        # ── 配置项 ──────────────────────────────────────────
        self.trigger_scope = self._conf_str("trigger_scope", "group")
        self.need_at_in_group = self._conf_bool("need_at_in_group", False)
        self.enable_summary = self._conf_bool("enable_summary", True)
        self.summary_max_chars = max(50, self._conf_int("summary_max_chars", 200))
        self.summary_timeout = max(5, self._conf_int("summary_timeout", 45))
        self.send_video = self._conf_bool("send_video", False)
        self.video_send_mode = self._conf_str("video_send_mode", "auto")
        self.max_video_mb = max(1, self._conf_int("max_video_mb", 50))
        self.request_timeout = max(5, self._conf_int("request_timeout", 20))
        self.max_concurrent = max(1, self._conf_int("max_concurrent", 1))
        self.user_cooldown = max(0, self._conf_int("user_cooldown", 60))
        self.group_whitelist = self._conf_list("group_whitelist")
        self.send_ebilibili_link = self._conf_bool("send_ebilibili_link", True)
        self.reply_limit = max(0, self._conf_int("reply_limit", 10))
        self.verbose_log = self._conf_bool("verbose_log", False)

        # ── 运行时状态 ──────────────────────────────────────
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(self.max_concurrent)
        self._cooldown: dict[str, float] = {}
        self._recent: dict[str, RecentVideo] = {}
        # key 是原始链接文本，value 是开始处理的时间戳
        self._busy: dict[str, float] = {}

        # 临时文件放在 AstrBot 数据目录，而不是插件自身目录：
        # 插件目录可能被更新/覆盖，数据目录才是可写且持久的位置。
        self._data_dir = Path(StarTools.get_data_dir(DATA_DIR_NAME))
        self._tmp_dir = self._data_dir / "tmp"
        try:
            self._tmp_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning(f"{LOG_TAG} 无法创建数据目录 {self._tmp_dir}：{exc}")
        cleanup_old_files(self._tmp_dir, 24)

        logger.info(
            f"{LOG_TAG} 插件已加载（触发范围={self.trigger_scope}，"
            f"自动发视频={'开' if self.send_video else '关'}，"
            f"发送方式={self.video_send_mode}，体积上限={self.max_video_mb}MB）"
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

    def _is_at_bot(self, event: AstrMessageEvent) -> bool:
        self_id = str(event.get_self_id())
        for component in event.get_messages():
            if isinstance(component, Comp.At) and str(component.qq) == self_id:
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

        # 只在"自动响应链接"时要求艾特；显式命令/关键词不受影响
        if auto and is_group and self.need_at_in_group and not self._is_at_bot(event):
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
        """同一视频正在处理时不再重复开工。

        用时间戳而不是集合：生成器被提前中断时 finally 不一定会执行，
        时间戳形式即使漏掉一次清理也会自动过期，不会永久卡死。
        """
        now = time.time()
        for old_key in [k for k, ts in self._busy.items() if now - ts > 600]:
            self._busy.pop(old_key, None)
        if key in self._busy:
            return True
        self._busy[key] = now
        return False

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

    async def _process(
        self, event: AstrMessageEvent, ref: BiliRef, *, want_video: bool
    ) -> ProcessResult:
        """完成一次解析 → 总结 →（可选）下载。"""
        umo = event.unified_msg_origin
        session = await self._get_session()
        api = BiliApi(session, self.request_timeout)

        bvid, aid = await self._resolve_ref(api, ref)
        info = await api.get_view(bvid=bvid, aid=aid)

        link = (
            ref.raw
            if ref.raw.lower().startswith("http")
            else f"https://www.bilibili.com/video/{info.bvid}"
        )
        self._remember(umo, info, ref.page, link)
        logger.info(f"{LOG_TAG} 解析成功：{info.bvid} 《{info.title}》 P{ref.page}")

        # 热评只在需要总结时才去取，省一次请求
        replies: list[str] = []
        summary: str | None = None
        basis: str | None = None
        if self.enable_summary:
            replies = await api.get_hot_replies(info.aid, self.reply_limit)
            summary, basis = await summarize_video(
                self.context,
                umo=umo,
                info=info,
                replies=replies,
                max_chars=self.summary_max_chars,
                timeout=self.summary_timeout,
            )

        result = ProcessResult()
        if want_video:
            result.video_path, result.oversized_mb, result.direct_url = await self._download(
                info, ref.page, link
            )

        result.caption = build_caption(
            title=info.title or "B站视频",
            owner=info.owner,
            duration=info.duration,
            view_count=info.view,
            page=ref.page,
            summary=summary,
            summary_basis=basis,
            web_url=EbilibiliClient.web_url(info.bvid) if self.send_ebilibili_link else None,
            video_attached=result.has_video,
            oversized_mb=result.oversized_mb,
            download_hint=(
                None
                if (result.has_video or self.send_video)
                else "💡 回复「下载」或发送 /bdl 可获取视频文件"
            ),
        )
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
        )
        max_bytes = self.max_video_mb * 1024 * 1024
        cid = info.cid_for_page(page)

        async with self._semaphore:
            play = await client.resolve(bvid=info.bvid, cid=cid, original_link=link)
            if play is None:
                logger.warning(f"{LOG_TAG} {info.bvid} 未能取得下载直链")
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
                return None, None, play.url

            logger.info(
                f"{LOG_TAG} 下载完成 {path.name}（{path.stat().st_size / 1024 / 1024:.1f}MB，"
                f"耗时 {time.time() - started:.1f}s）"
            )
            return path, None, play.url

    # ──────────────────────────────────────────────── 事件入口

    @filter.regex(TRIGGER_REGEX)
    async def on_bili_link(self, event: AstrMessageEvent):
        """自动响应消息中的 B 站视频链接。"""
        ref = find_ref(self._message_text(event))
        if ref is None:
            return
        if not self._allowed(event):
            return
        if self._cooldown_blocked(event):
            return
        if self._busy_blocked(ref.raw):
            await self._send_text(event, "⏳ 这个视频正在处理中，请稍后再试～")
            return

        # 已接管这条消息：阻止默认 LLM 回复重复应答
        event.stop_event()

        result: ProcessResult | None = None
        await self._send_text(event, "🔍 正在解析视频信息…")
        try:
            result = await self._process(event, ref, want_video=self.send_video)
            await self._send_text(event, result.caption)
            if result.has_video:
                if not await self._send_video(event, result.video_path, result.direct_url):
                    await self._send_text(
                        event,
                        "⚠️ 视频发送失败：协议端可能读不到机器人本地的文件。\n"
                        "可把「视频发送方式」改为「直链 URL」后重试，或用上面的下载页链接自行下载。",
                    )
        except BiliApiError as exc:
            logger.warning(f"{LOG_TAG} 解析失败：{exc}")
            await self._send_text(event, f"❌ 解析失败：{exc}")
        except Exception as exc:  # noqa: BLE001 - 单条消息出错不该影响插件整体
            logger.error(f"{LOG_TAG} 处理链接异常：{type(exc).__name__}: {exc}", exc_info=True)
            await self._send_text(event, "❌ 处理视频时出错了，请稍后再试")
        finally:
            # 必须等所有发送都 await 结束才删除，否则协议端会读到"文件不存在"
            self._safe_unlink(result.video_path if result else None)

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
            await self._send_text(event, "⬇️ 正在解析并下载视频，请稍候…")
            try:
                result = await self._process(event, ref, want_video=True)
                await self._send_text(event, result.caption)
                if not result.has_video:
                    logger.info(f"{LOG_TAG} 未取得视频，仅发送解析结果")
                elif not await self._send_video(event, result.video_path, result.direct_url):
                    await self._send_text(
                        event, "⚠️ 视频发送失败，请查看日志或改用下载页链接。"
                    )
            except BiliApiError as exc:
                await self._send_text(event, f"❌ 解析失败：{exc}")
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    f"{LOG_TAG} 下载流程异常：{type(exc).__name__}: {exc}", exc_info=True
                )
                await self._send_text(event, "❌ 下载视频时出错了，请稍后再试")
            finally:
                self._safe_unlink(result.video_path if result else None)
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
        self, event: AstrMessageEvent, info: VideoInfo, page: int, link: str
    ) -> None:
        """已解析过的视频：下载并发送（必要时补充解析卡片）。"""
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

    # ──────────────────────────────────────────────── 生命周期

    async def terminate(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()
        cleanup_old_files(self._tmp_dir, 24)
        logger.info(f"{LOG_TAG} 插件已卸载")
