"""AI 总结：把视频元数据（+热门评论）交给 AstrBot 当前的 LLM 生成简介。

诚实原则：接口拿不到字幕时，总结只能基于标题/简介/热评，
因此返回值里带一个 ``basis`` 标签，由调用方在卡片上注明依据，
不让用户误以为机器人"看过"视频。
"""

from __future__ import annotations

import asyncio

from astrbot.api import logger

from .bili_api import VideoInfo
from .formatting import format_count, format_duration, short_text

# 简介太长会挤占 prompt，300 字足够表达"这是什么视频"
DESC_LIMIT = 300

PROMPT_TEMPLATE = """你是视频摘要助手。请用中文、3~5 句话总结下面这个 B 站视频，直接给结论，不要客套话、不要分点标题，不要编造未提供的信息。如果仅凭标题和简介无法判断具体内容，就如实说明"仅凭标题与简介无法确定具体内容"，不要虚构情节。

【标题】{title}
【UP主】{owner}
【时长】{duration}
【分区】{tname}
【播放/点赞】{view} / {like}
【简介】{desc}
{replies_block}"""


def build_prompt(info: VideoInfo, replies: list[str]) -> tuple[str, str]:
    """返回 (prompt, 总结依据标签)。"""
    if replies:
        replies_block = "【热门评论】\n" + "\n".join(
            f"{index}. {text}" for index, text in enumerate(replies, 1)
        )
        basis = "基于简介与热评"
    else:
        replies_block = ""
        basis = "基于标题与简介"

    prompt = PROMPT_TEMPLATE.format(
        title=info.title or "未知",
        owner=info.owner or "未知",
        duration=format_duration(info.duration),
        tname=info.tname or "未知",
        view=format_count(info.view),
        like=format_count(info.like),
        desc=short_text(info.desc, DESC_LIMIT) or "（无简介）",
        replies_block=replies_block,
    )
    return prompt, basis


async def summarize_video(
    context,
    *,
    umo: str,
    info: VideoInfo,
    replies: list[str],
    max_chars: int,
    timeout: int,
) -> tuple[str | None, str | None]:
    """调用当前会话的 LLM 生成总结。

    返回 (总结文本, 依据标签)；任何失败都返回 (None, None)，
    由调用方继续正常发送视频——总结不该成为发视频的前置条件。
    """
    prompt, basis = build_prompt(info, replies)

    try:
        provider_id = await context.get_current_chat_provider_id(umo=umo)
        if not provider_id:
            logger.info("当前会话未配置聊天模型，跳过 AI 总结")
            return None, None

        response = await asyncio.wait_for(
            context.llm_generate(chat_provider_id=provider_id, prompt=prompt),
            timeout=max(5, timeout),
        )
    except asyncio.TimeoutError:
        logger.warning(f"AI 总结超时（>{timeout}s），跳过")
        return None, None
    except Exception as exc:  # noqa: BLE001 - LLM 未配置/额度耗尽等都会抛异常
        logger.warning(f"AI 总结失败：{type(exc).__name__}: {exc}")
        return None, None

    # 不同 AstrBot 版本返回的对象略有差异，这里做兼容取值
    text = getattr(response, "completion_text", None)
    if text is None and isinstance(response, str):
        text = response
    text = (text or "").strip()
    if not text:
        logger.info("AI 总结返回空内容，跳过")
        return None, None

    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"

    return text, basis


__all__ = ["build_prompt", "summarize_video"]
