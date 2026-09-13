"""文本与文件名的格式化工具。

集中放在这里是为了让 main.py 只关注编排逻辑，也方便单独测试这些纯函数。
"""

from __future__ import annotations

import re

# Windows 文件名非法字符 + ASCII 控制字符
_UNSAFE_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MULTI_SPACE = re.compile(r"\s+")
_MULTI_UNDERSCORE = re.compile(r"_{2,}")


def format_duration(seconds: int | float | None) -> str:
    """把秒数格式化成中文时长，例如 213 -> 3分33秒。"""
    if not seconds or seconds <= 0:
        return "未知"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes}分" if minutes else f"{hours}小时"
    if minutes:
        return f"{minutes}分{secs}秒" if secs else f"{minutes}分"
    return f"{secs}秒"


def format_count(value: int | float | None) -> str:
    """把播放量这类大数字格式化成 1.2万 / 3.4亿。"""
    if value is None:
        return "0"
    number = float(value)
    if number >= 100_000_000:
        return f"{number / 100_000_000:.1f}亿"
    if number >= 10_000:
        return f"{number / 10_000:.1f}万"
    return str(int(number))


def sanitize_filename(name: str, fallback: str = "bilibili_video.mp4", max_len: int = 80) -> str:
    """把视频标题清洗成安全的本地文件名。

    保留中文与常见符号（QQ 群文件名中文很常见，不需要 ASCII 化），
    只去掉路径分隔符、保留字符冲突项与控制字符，并限制长度避免超长路径。
    """
    cleaned = _UNSAFE_FILENAME_CHARS.sub("_", (name or "").strip())
    cleaned = _MULTI_SPACE.sub(" ", cleaned)
    # 相邻的非法字符（例如 ?"）会连出多个下划线，压成一个更整洁
    cleaned = _MULTI_UNDERSCORE.sub("_", cleaned).strip(" .")
    if not cleaned:
        return fallback
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(" .")
    return cleaned or fallback


def short_text(text: str | None, limit: int) -> str:
    """截断长文本，超长时加省略号。"""
    if not text:
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def build_caption(
    *,
    title: str,
    owner: str,
    duration: int | float | None,
    view_count: int | float | None,
    page: int,
    summary: str | None,
    summary_basis: str | None,
    web_url: str | None,
    video_attached: bool,
    oversized_mb: float | None = None,
    download_hint: str | None = None,
) -> str:
    """组装发送给用户的信息卡片（纯文本部分）。"""
    lines: list[str] = []

    page_tag = f"（P{page}）" if page and page > 1 else ""
    lines.append(f"📺 {title}{page_tag}")

    meta = [owner or "未知UP主", format_duration(duration), f"{format_count(view_count)}播放"]
    lines.append("👤 " + " · ".join(part for part in meta if part))

    if summary:
        basis = f"（{summary_basis}）" if summary_basis else ""
        lines.append("")
        lines.append(f"📝 AI 总结{basis}")
        lines.append(summary)

    if video_attached:
        lines.append("")
        lines.append("🎬 视频文件已附在下方")
    elif oversized_mb is not None:
        lines.append("")
        lines.append(f"⚠️ 视频约 {oversized_mb:.0f} MB，超过体积上限，未自动发送文件")

    if web_url:
        lines.append("")
        lines.append(f"🔗 下载页：{web_url}")

    if download_hint:
        lines.append(download_hint)

    return "\n".join(lines)
