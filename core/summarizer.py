"""AI 总结：把视频元数据（+ 热门评论 + 标签）交给 AstrBot 当前的 LLM。

两条原则：

1. **尽力推断**：拿不到字幕时，总结只能基于元数据与热评；但要求模型充分利用
   UP 主名、分区、标签、分P名、热评关键词等线索，不要因为信息不全就只回一句
   「无法确定」——那等于没做总结；
2. **讲清依据**：返回值带 ``basis`` 标签，由调用方在卡片上注明依据，不让用户
   误以为机器人"看过"视频；不确定的内容要求用「推测」这类措辞表达。
"""

from __future__ import annotations

import asyncio

from astrbot.api import logger

from .bili_api import VideoInfo
from .formatting import format_count, format_duration, short_text

# 简介太长会挤占 prompt，300 字足够表达"这是什么视频"
DESC_LIMIT = 300

PROMPT_TEMPLATE = """你是视频内容概括助手。请用中文、3~5 句话直接讲述这个 B 站视频**讲了什么**，像给朋友复述一样自然连贯，不要客套话、不要分点标题。

硬性要求：
1. **禁止一切"关于信息来源"的元话语**：不要写「视频看起来」「可能」「大概」「简介提到」「热门评论说」「需要观众自行补完」「无法确定」——依据由系统单独标注，正文里不必交代；
2. **不要总结热门评论**：热评只用来帮你判断内容方向，正文要讲视频本身；
3. **把话讲完**：用现有线索（UP主名、分区、标签、分P名、简介、热评关键词）推断出最可能的内容主线并直接叙述，宁可给出有保留的判断，也不要写免责声明；
4. 不要编造具体数据、人名或未提供的情节细节。

示例语感（只示范句式，不要照抄内容）：
「主角在废弃工厂遭遇机械守卫，靠拆解配电箱制造短路脱身，随后用改装无人机反制追踪者，最终在顶层揭露幕后工程师正是他的旧友，一段关于信任与背叛的赛博故事。」

【标题】{title}
【UP主】{owner}
【时长】{duration}
【分区】{tname}
【标签】{tags}
【分P】{pages}
【播放/点赞】{view} / {like}
【简介】{desc}
{replies_block}"""


def _format_pages(info: VideoInfo, limit: int = 6) -> str:
    """把分P标题拼成一行——多P视频的 P 名常常直接说明内容（如「实况」「教程」）。

    单P视频没什么信息量，直接返回「（单P）」以免浪费 prompt 空间。
    """
    pages = [page for page in (getattr(info, "pages", None) or []) if page.part]
    if len(pages) <= 1:
        return "（单P）"
    shown = " / ".join(short_text(page.part, 20) for page in pages[:limit])
    if len(pages) > limit:
        shown += f" / …（共 {len(pages)}P）"
    return shown


PROMPT_WITH_SUBTITLE_TEMPLATE = """你是视频内容概括助手。下面是这个 B 站视频的**字幕文本**（按时间分段，段首方括号是时间点），请用中文、3~5 句话直接讲述视频**实际讲了什么**：内容主线、关键信息、结论或看点，像给朋友复述一样自然连贯。

硬性要求：
1. 一切以字幕为准，不要加入字幕中没有的情节或数据；
2. **不要写「视频看起来」「可能」「字幕提到」这类元话语**，直接叙述内容本身；也不要总结评论区；
3. 可以在关键处附上时间点（如 [03:45]）方便跳转，但不要每句都加；
4. 字幕是语音识别结果，可能有错别字、口语重复或断句问题，按语义理解即可，不要照抄病句；
5. 若字幕本身几乎没有内容（纯音乐、无解说），用一句话说明即可。

示例语感（只示范句式，不要照抄内容）：
「主角在废弃工厂遭遇机械守卫，靠拆解配电箱制造短路脱身，随后用改装无人机反制追踪者，最终在顶层揭露幕后工程师正是他的旧友，一段关于信任与背叛的赛博故事。」

【标题】{title}
【UP主】{owner}
【时长】{duration}
【分区】{tname}
【字幕】
{subtitle}"""


def build_prompt(
    info: VideoInfo,
    replies: list[str],
    tags: list[str] | None = None,
    subtitle_text: str | None = None,
) -> tuple[str, str]:
    """返回 (prompt, 总结依据标签)。

    有字幕时走字幕模板——只有它能说出「视频里讲了什么」；没有字幕时走元数据
    模板，只能推断内容方向。两条路径都要求模型不编造，且用措辞区分事实与推断。
    """
    if subtitle_text:
        prompt = PROMPT_WITH_SUBTITLE_TEMPLATE.format(
            title=info.title or "未知",
            owner=info.owner or "未知",
            duration=format_duration(info.duration),
            tname=info.tname or "未知",
            subtitle=subtitle_text,
        )
        return prompt, "基于字幕"

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
        tags="、".join(tags) if tags else "（无）",
        pages=_format_pages(info),
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
    tags: list[str] | None = None,
    subtitle_text: str | None = None,
) -> tuple[str | None, str | None]:
    """调用当前会话的 LLM 生成总结。

    返回 (总结文本, 依据标签)；任何失败都返回 (None, None)，
    由调用方继续正常发送视频——总结不该成为发视频的前置条件。
    """
    prompt, basis = build_prompt(info, replies, tags, subtitle_text)

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
