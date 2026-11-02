"""发送编排：把一次解析的产物整理成消息列表，并按需合并转发。

从 main.py 搬出来的原因：这一块（收集消息 → 按阈值决定合并转发还是逐条发 →
失败兜底）跟平台、解析、渲染都无关，只跟"怎么发到 QQ"有关。搬走后 main.py
只留装配与主流程。

实现方式是 mixin（``DispatchMixin``），方法里的 ``self`` 仍然是插件实例，
所以不需要把一堆配置改成构造参数、也不用担心双份状态。代价是隐式依赖，
宿主类必须提供下面这些属性：

    _stats                  RuntimeStats
    video_send_mode         "auto" | "file" | "url"
    max_video_mb            体积上限（MB），超限提示用
    forward_threshold       合并转发阈值，0 = 关闭
    forward_sender_name     转发里显示的发送者名字
    send_ebilibili_link     是否在回复里附下载页链接
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .constants import LOG_TAG
from .media import MediaInfo


@dataclass(slots=True)
class ProcessResult:
    """一次解析的产物。"""

    caption: str = ""
    card_url: str | None = None  # 信息卡片图片；None 表示回退纯文本 caption
    replies_card_url: str | None = None  # 热评卡片（独立图片），可能为 None
    info: MediaInfo | None = None  # 供「下载并发送」阶段复用，避免重复请求
    page: int = 1
    link: str = ""
    web_url: str = ""  # 下载失败时兜底给用户的网页地址（由平台提供）
    video_path: Path | None = None  # 已下载到本地的视频（url 模式为 None）
    direct_url: str | None = None  # 直链，供协议端自行拉取
    oversized_mb: float | None = None  # 超过体积上限时的真实体积
    # 图集的本地文件（与 info.image_items() 顺序一一对应，下载失败的为 None）。
    # 为什么要落盘：直接发 URL 时 QQ 要自己回源拉图，遇到防盗链/慢 CDN 就是裂图；
    # 下载到本地再发可以复用同一个下载器（超时、并发、体积上限都一致）。
    gallery_paths: list[Path | None] = field(default_factory=list)

    @property
    def has_video(self) -> bool:
        return self.video_path is not None or bool(self.direct_url)


@dataclass(slots=True)
class OutgoingMessage:
    """一条待发送的消息（用于按阈值决定是否合并转发）。

    ``fallback_text`` 是图片/视频发送失败时的兜底文本——发不出去时
    至少还能把解析结果以文字发出去。
    ``video_path`` 仅用于 ``kind == "video"``：本地文件兜底；合并转发时优先用
    ``payload`` 里的直链（协议端分离部署读不到机器人的本地文件）。
    """

    kind: str  # "text" | "image" | "video"
    payload: str = ""
    fallback_text: str = ""
    video_path: Path | None = None
    #: 仅用于 kind == "image"：图集下载到本地的文件。有它优先发本地，
    #: 没有再退回 payload 里的 URL（下载失败时至少还能让协议端自己拉）。
    image_path: Path | None = None


class DispatchMixin:
    """发送编排（宿主类的属性约定见模块 docstring）。"""

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

    def _collect_gallery(
        self, result: ProcessResult, limit: int = 5
    ) -> list[OutgoingMessage]:
        """图文笔记的图片消息（小红书 / 抖音图集这类）。

        图片不需要占视频的体积上限，所以不跟 ``send_video`` 走——否则默认配置下
        图文笔记会「解析成功但什么都没发」。上限默认 5 张：再多会刷屏，QQ 单条
        消息也塞不下（面板里可调 1~35 张，走 runtime 覆盖层，改完即时生效）。

        优先用下载到本地的文件：``result.gallery_paths`` 与 ``image_items()``
        顺序一一对应，某项为 None 表示那张没下下来，退回发原始 URL。
        """
        info = result.info
        if info is None:
            return []
        images = info.image_items()
        if not images:
            return []
        paths = result.gallery_paths or []
        messages: list[OutgoingMessage] = []
        for index, item in enumerate(images[:limit]):
            local = paths[index] if index < len(paths) else None
            messages.append(
                OutgoingMessage(kind="image", payload=item.url, image_path=local)
            )
        # 图文作品动辄二三十张（实测一条抖音图文 34 张），默认只发前几张。
        # 不说明的话，用户会以为后面的图丢了或者没解析全。
        if len(images) > limit:
            messages.append(
                OutgoingMessage(
                    kind="text",
                    payload=(
                        f"📷 这组共 {len(images)} 张，先发了前 {limit} 张。"
                        f"需要更多可以在面板里调大「图集最多发送」。"
                    ),
                )
            )
        return messages

    def _collect_media(
        self,
        result: ProcessResult,
        *,
        video_path: Path | None = None,
        direct_url: str | None = None,
        oversized_mb: float | None = None,
    ) -> list[OutgoingMessage]:
        """视频 / 图集 / 「超过体积上限」提示对应的消息；没有就返回空列表。

        与卡片分开是为了支持「卡片先出」——关闭合并转发时先发卡片，
        下载完成后再单独发这一段。
        """
        if video_path is not None or direct_url:
            return [
                OutgoingMessage(
                    kind="video",
                    payload=direct_url or "",
                    video_path=video_path,
                    # 两条路都失败时补发这段：让用户至少能拿到下载链接，
                    # 而不是只在日志里留一行 error（2026-10-24 实测过这种静默失败）。
                    fallback_text=self._video_fallback(result),
                )
            ]
        # 图集开关与上限都能在面板里调（运行期覆盖，见 core/runtime.py）
        if getattr(self, "xhs_send_images", True):
            gallery = self._collect_gallery(
                result, limit=getattr(self, "xhs_max_images", 5)
            )
            if gallery:
                return gallery
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

    def _video_fallback(self, result: ProcessResult) -> str:
        """视频发送彻底失败时的兜底文案（附可自行下载的链接）。

        两种失败要分开说，否则用户按提示改配置也修不好：

        - 本地文件 ``retcode 1200``：协议端读不到 AstrBot 的文件（分离部署）；
        - 直链发送失败（如 NTQQ 的 ``Highway ... code 102902``）：协议端其实已经
          下载成功，卡在**上传到 QQ** 那一步，常见于视频偏大或网络中断。
        """
        lines = ["⚠️ 视频没有发送成功（本地文件与直链两种方式都失败了）。"]
        if result.info is not None:
            lines.append("常见原因：协议端读不到 AstrBot 的本地文件，或视频偏大导致 QQ 上传中断。")
            if result.web_url:
                lines.append(f"可自行下载：{result.web_url}")
        return "\n".join(lines)

    def _oversized_tip(self, result: ProcessResult, oversized_mb: float) -> str:
        """视频超过体积上限时的提示（尽量附上可用的下载页链接）。"""
        tip = (
            f"⚠️ 视频约 {oversized_mb:.0f} MB，超过体积上限 {self.max_video_mb} MB，未发送文件。"
        )
        if result.info is not None and self.send_ebilibili_link and result.web_url:
            tip += f"\n可在面板调大「视频体积上限」，或直接访问：{result.web_url}"
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
                await self._send_image(
                    event, message.payload, message.fallback_text, message.image_path
                )
            elif message.kind == "video":
                sent = await self._send_video(
                    event, message.video_path, message.payload or None
                )
                if not sent and message.fallback_text:
                    # 发送失败不能静默：补一条带下载链接的提示
                    await self._send_text(event, message.fallback_text)
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
                    # 合并转发里**优先直链**，和视频同一个理由：协议端与 AstrBot
                    # 可能不在同一文件系统，节点里塞本地路径会 retcode 1200，
                    # 而合并转发是一锤子买卖——一张图挂了整包都发不出去。
                    # 逐条发送那条路才用本地文件（见 _send_image，且有 URL 兜底）。
                    if message.payload.startswith("http"):
                        component = Comp.Image.fromURL(message.payload)
                    elif message.image_path is not None:
                        component = Comp.Image.fromFileSystem(path=str(message.image_path))
                    else:
                        continue
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
        self,
        event: AstrMessageEvent,
        url: str,
        fallback_text: str = "",
        path: Path | None = None,
    ) -> bool:
        """发送一张图片；失败时若给了兜底文本，则改发文本。

        有本地文件先走 ``fromFileSystem``（图集已经下过一次，不必让协议端回源），
        但**失败后必定退到 URL 再试一次**：协议端与 AstrBot 不在同一文件系统时
        本地路径会 retcode 1200（v0.1.1 踩过，视频那边也是同一原因），而图片
        本来就是普通的 HTTP 地址，退回 URL 通常还能救回来。
        """
        attempted: list[str] = []

        async def _try_local() -> bool:
            if path is None or not Path(path).exists():
                return False
            await event.send(event.chain_result([Comp.Image.fromFileSystem(path=str(path))]))
            return True

        async def _try_url() -> bool:
            if not url.startswith("http"):
                return False
            await event.send(event.image_result(url))
            return True

        for label, sender in (("本地文件", _try_local), ("图片直链", _try_url)):
            try:
                if await sender():
                    if attempted:
                        logger.info(f"{LOG_TAG} 图片改用{label}发送成功（前一次失败：{attempted[0]}）")
                    return True
            except Exception as exc:  # noqa: BLE001 - 平台侧异常类型随适配器而变
                attempted.append(f"{label}:{type(exc).__name__}")
                logger.warning(f"{LOG_TAG} 图片以{label}发送失败：{type(exc).__name__}: {exc}")

        self._stats.send_fail += 1
        logger.error(f"{LOG_TAG} 发送图片失败（两种方式都试过了：{attempted}）")
        if fallback_text:
            logger.info(f"{LOG_TAG} 图片发送失败，改发纯文本卡片")
            return await self._send_text(event, fallback_text)
        return False


__all__ = ["DispatchMixin", "OutgoingMessage", "ProcessResult"]
