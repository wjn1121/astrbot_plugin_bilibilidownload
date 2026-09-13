"""视频流式下载与临时文件管理。

设计要点：
1. 先做体积预检再落盘，避免把一个几百 MB 的文件写到一半才发现超限；
2. 边下边校验累计大小，因为 Content-Length 可能缺失或被 CDN 说谎；
3. 下载失败/超限时只删自己创建的文件，绝不留下半截文件被当成有效缓存。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

import aiohttp

from astrbot.api import logger

from .http import BASE_HEADERS

# 单次读取块大小：1MB 在内存占用与写盘次数之间比较平衡
CHUNK_SIZE = 1024 * 1024

# B 站 CDN 会校验 UA：aiohttp 默认 UA 会拿到 403（实测），必须显式带浏览器 UA。
# Referer 指向 B 站，兼容对防盗链检查更严格的 CDN 节点。
DOWNLOAD_HEADERS = {
    **BASE_HEADERS,
    "Referer": "https://www.bilibili.com/",
}


class DownloadError(Exception):
    """下载失败（网络、状态码、写入等）。"""


class VideoTooLarge(DownloadError):
    """视频体积超过配置上限。"""

    def __init__(self, size_bytes: int, limit_bytes: int) -> None:
        super().__init__(f"视频体积 {size_bytes} 字节超过上限 {limit_bytes} 字节")
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes


def _human_mb(size_bytes: int | None) -> float:
    return round((size_bytes or 0) / 1024 / 1024, 1)


async def probe_size(session: aiohttp.ClientSession, url: str, timeout: int) -> int | None:
    """尝试用 HEAD 预取文件大小；不支持 HEAD 时返回 None（不视为错误）。"""
    try:
        async with session.head(
            url,
            headers=DOWNLOAD_HEADERS,
            timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=True,
        ) as resp:
            if resp.status >= 400:
                return None
            length = resp.headers.get("Content-Length")
            return int(length) if length and length.isdigit() else None
    except Exception as exc:  # noqa: BLE001 - 预检失败不影响主流程
        logger.debug(f"预览视频体积失败（忽略）：{type(exc).__name__}: {exc}")
        return None


async def download_video(
    session: aiohttp.ClientSession,
    url: str,
    dest_dir: Path,
    filename: str,
    *,
    max_bytes: int,
    timeout: int,
    on_progress: Callable[[int, int | None], None] | None = None,
) -> Path:
    """把 url 流式下载到 dest_dir/filename，返回落地路径。

    抛出 DownloadError / VideoTooLarge，调用方负责降级。
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / filename
    part = dest_dir / (filename + ".part")

    known_size = await probe_size(session, url, timeout)
    if known_size is not None and known_size > max_bytes:
        raise VideoTooLarge(known_size, max_bytes)

    downloaded = 0
    try:
        async with session.get(
            url,
            headers=DOWNLOAD_HEADERS,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=timeout, sock_read=timeout * 3),
            allow_redirects=True,
        ) as resp:
            if resp.status >= 400:
                raise DownloadError(f"下载返回状态码 {resp.status}")

            content_length = resp.headers.get("Content-Length")
            total = known_size or (int(content_length) if content_length and content_length.isdigit() else None)
            if total is not None and total > max_bytes:
                raise VideoTooLarge(total, max_bytes)

            with part.open("wb") as file_obj:
                async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                    downloaded += len(chunk)
                    if downloaded > max_bytes:
                        raise VideoTooLarge(downloaded, max_bytes)
                    file_obj.write(chunk)
                    if on_progress:
                        on_progress(downloaded, total)

        if downloaded == 0:
            raise DownloadError("下载内容为空")

        # 先写 .part 再原子改名：中途异常时不会留下看似完整的坏文件
        part.replace(target)
        return target
    except (VideoTooLarge, DownloadError):
        _safe_unlink(part)
        raise
    except asyncio.TimeoutError as exc:
        _safe_unlink(part)
        raise DownloadError(f"下载超时：{exc}") from exc
    except aiohttp.ClientError as exc:
        _safe_unlink(part)
        raise DownloadError(f"网络错误：{exc}") from exc
    except OSError as exc:
        _safe_unlink(part)
        raise DownloadError(f"写入文件失败：{exc}") from exc


def _safe_unlink(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        logger.warning(f"清理临时文件失败 {path}：{exc}")


def cleanup_old_files(directory: Path, max_age_hours: int = 24) -> int:
    """清理目录下过期的临时文件，返回清理数量。

    机器人被强杀/重启时 finally 不会执行，残留只能靠启动时兜底清理。
    """
    if not directory.is_dir():
        return 0
    deadline = time.time() - max_age_hours * 3600
    removed = 0
    for item in directory.iterdir():
        try:
            if item.is_file() and item.stat().st_mtime < deadline:
                item.unlink()
                removed += 1
        except OSError as exc:
            logger.debug(f"跳过无法清理的文件 {item}：{exc}")
    if removed:
        logger.info(f"已清理 {removed} 个过期临时视频文件")
    return removed


__all__ = [
    "DownloadError",
    "VideoTooLarge",
    "cleanup_old_files",
    "download_video",
    "probe_size",
]
