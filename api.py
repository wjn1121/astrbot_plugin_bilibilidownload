"""插件 Web 面板的接口层。

只读监控 + 试解析：**不提供任何会改动配置的接口**（改配置仍去 AstrBot 原生
插件配置面板），这是刻意的——两处都能写配置必然分叉，见 OPTIMIZATION §8A。

接口一览（路径前缀 ``/<plugin_name>``）：

    GET  /status          运行状态、平台、缓存、关键配置摘要
    GET  /history         最近解析记录（分页/过滤）
    POST /probe           试解析：给一段链接，返回元信息 + 卡片预览 + 直链可用性
    GET  /cache           各平台缓存明细
    POST /cache/clear     清空全部平台缓存
    GET  /platforms       平台能力与启停状态
    GET  /settings        运行期调整（生效值 + 来源 + 控件元信息）
    POST /settings/set    保存运行期调整（写 runtime.json，不碰 AstrBot 配置）
    POST /bili/qrcode     生成 B 站登录二维码并开始轮询
    GET  /bili/qrcode     读扫码进度（waiting / scanned / success / expired / failed）
    POST /bili/logout     清除扫码得到的登录态

``register_web_api`` 从 AstrBot 4.24.2 起才有；低版本上这里逐个 try/except，
面板不出现但插件功能完全正常。
"""

from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .core import platforms
from .core.constants import LOG_TAG, VERSION
from .core.platforms import ParseHint, PlatformError

# 试解析限流：同一进程内两次之间至少间隔这么久。
# 面板可以被连点，而每次试解析都会真的打 B 站/下载站接口——不限流会把 412 风控招来。
PROBE_MIN_INTERVAL = 5.0

# 卡片预览转 data URI 的体积上限（超过就只回链接，避免响应体几 MB）
PREVIEW_MAX_BYTES = 4 * 1024 * 1024

_last_probe_at = 0.0

# 配置项里"可以给面板看"的那部分；sessdata 之类一律不返回（只给布尔值）
_SAFE_CONFIG_KEYS = (
    "trigger_scope",
    "need_at_in_group",
    "send_video",
    "video_send_mode",
    "max_video_mb",
    "enable_summary",
    "summary_max_chars",
    "summary_timeout",
    "reply_limit",
    "show_replies",
    "replies_show_count",
    "output_image",
    "card_width",
    "card_qrcode",
    "forward_threshold",
    "user_cooldown",
    "duplicate_window_minutes",
    "request_timeout",
    "enabled_platforms",
    "verbose_log",
)


def register(plugin) -> None:
    """把全部接口注册到 AstrBot context。"""
    routes = (
        (f"/{plugin.plugin_name}/status", api_status, ["GET"], "插件运行状态"),
        (f"/{plugin.plugin_name}/history", api_history, ["GET"], "最近解析记录"),
        (f"/{plugin.plugin_name}/probe", api_probe, ["POST"], "试解析链接"),
        (f"/{plugin.plugin_name}/cache", api_cache, ["GET"], "缓存明细"),
        (f"/{plugin.plugin_name}/cache/clear", api_cache_clear, ["POST"], "清空缓存"),
        (f"/{plugin.plugin_name}/platforms", api_platforms, ["GET"], "平台状态"),
        (f"/{plugin.plugin_name}/credentials", api_credentials, ["GET"], "凭据状态"),
        (f"/{plugin.plugin_name}/credentials/set", api_credentials_set, ["POST"], "保存凭据"),
        (f"/{plugin.plugin_name}/credentials/test", api_credentials_test, ["POST"], "测试凭据"),
        (f"/{plugin.plugin_name}/settings", api_settings, ["GET"], "运行期调整（读取）"),
        (f"/{plugin.plugin_name}/settings/set", api_settings_set, ["POST"], "运行期调整（保存）"),
        (f"/{plugin.plugin_name}/bili/qrcode", api_bili_qrcode, ["POST", "GET"], "B站扫码登录"),
        (f"/{plugin.plugin_name}/bili/logout", api_bili_logout, ["POST"], "退出 B 站登录"),
    )
    for route, func, methods, desc in routes:
        try:
            plugin.context.register_web_api(route, _bind(plugin, func), methods, desc)
        except Exception as exc:  # noqa: BLE001 - 老版本 AstrBot 没有该接口
            logger.warning(f"{LOG_TAG} 注册 Web API {route} 失败（面板不可用）：{exc}")


def _bind(plugin, func):
    """把 plugin 实例绑到无参 handler 上（quart 调用时不传 request）。"""

    async def handler():
        return await func(plugin)

    return handler


# ── GET /status ─────────────────────────────────────────────────────────


async def api_status(plugin):
    from quart import jsonify

    stats = plugin._stats.snapshot()
    cache_rows = _cache_rows(plugin)
    last_error = stats.get("last_error")

    return jsonify(
        {
            "success": True,
            "data": {
                "version": VERSION,
                "uptime": stats["uptime"],
                "stats": {
                    "parse_ok": stats["parse_ok"],
                    "parse_fail": stats["parse_fail"],
                    "download_ok": stats["download_ok"],
                    "download_fail": stats["download_fail"],
                    "card_ok": stats["card_ok"],
                    "card_fail": stats["card_fail"],
                    "send_fail": stats["send_fail"],
                },
                "card": {
                    "enabled": bool(plugin.output_image),
                    "disabled": bool(plugin._card_disabled),
                    "fail_streak": int(plugin._card_fail_count),
                },
                "cache": cache_rows,
                "platforms": platforms.describe_all(plugin.enabled_platforms),
                "platform_errors": platforms.load_errors(),
                "config": _safe_config(plugin),
                "media_attempts": _attempt_rows(stats.get("media_attempts") or {}),
                "last_error": (
                    None
                    if last_error is None
                    else {
                        "where": last_error.name,
                        "detail": last_error.detail,
                        "at": last_error.at,
                    }
                ),
                "history_count": plugin.history.count(),
                "history_enabled": plugin.history.enabled,
                "sessions_disabled": len(plugin._disabled_sessions),
            },
        }
    )


def _safe_config(plugin) -> dict[str, Any]:
    """配置摘要：白名单字段 + sessdata 只给「配没配」。"""
    rows: dict[str, Any] = {}
    for key in _SAFE_CONFIG_KEYS:
        value = getattr(plugin, key, None)
        if isinstance(value, (list, tuple)):
            rows[key] = list(value)
        elif isinstance(value, Path):
            rows[key] = str(value)
        else:
            rows[key] = value
    rows["sessdata_configured"] = bool(plugin.sessdata)
    return rows


def _cache_rows(plugin) -> list[dict[str, Any]]:
    """各平台自己的缓存快照（B 站两个，未来平台可能一个都没有）。"""
    rows: list[dict[str, Any]] = []
    for name, inst in list(plugin._platforms.items()):
        snapshot = getattr(inst, "cache_snapshot", None)
        if not callable(snapshot):
            continue
        try:
            data = snapshot() or {}
        except Exception as exc:  # noqa: BLE001 - 面板不该因为某个平台报错打不开
            logger.debug(f"{LOG_TAG} 读取 {name} 缓存失败：{exc}")
            continue
        for cache_name, cache_stats in data.items():
            rows.append(
                {
                    "platform": inst.display or name,
                    "name": cache_name,
                    "size": cache_stats.get("size", 0),
                    "max_items": cache_stats.get("max_items", 0),
                    "ttl": cache_stats.get("ttl", 0),
                    "hits": cache_stats.get("hits", 0),
                    "misses": cache_stats.get("misses", 0),
                    "hit_rate": cache_stats.get("hit_rate", 0.0),
                }
            )
    return rows


def _attempt_rows(attempts: dict) -> list[dict[str, Any]]:
    """下载站各路径的最近状态（B 站专属；其它平台不会产生这些 key）。"""
    rows: list[dict[str, Any]] = []
    for name in ("直链接口", "表单页", "解析页"):
        attempt = attempts.get(name)
        if attempt is None:
            rows.append({"name": name, "used": False})
            continue
        rows.append(
            {
                "name": name,
                "used": True,
                "ok": bool(attempt.ok),
                "detail": attempt.detail,
                "at": attempt.at,
            }
        )
    return rows


# ── GET /history ────────────────────────────────────────────────────────


async def api_history(plugin):
    from quart import jsonify, request

    try:
        limit = int(request.args.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(200, limit))
    platform = str(request.args.get("platform") or "").strip()

    return jsonify(
        {
            "success": True,
            "data": {
                "entries": plugin.history.recent(limit=limit, platform=platform),
                "total": plugin.history.count(),
                "keep": plugin.history.keep,
            },
        }
    )


# ── GET /cache ──────────────────────────────────────────────────────────


async def api_cache(plugin):
    from quart import jsonify

    return jsonify({"success": True, "data": {"caches": _cache_rows(plugin)}})


async def api_cache_clear(plugin):
    from quart import jsonify

    parts: list[str] = []
    for name, inst in list(plugin._platforms.items()):
        clear = getattr(inst, "clear_cache", None)
        if not callable(clear):
            continue
        count = int(clear() or 0)
        parts.append(f"{inst.display or name} {count} 条")
    return jsonify(
        {
            "success": True,
            "message": f"已清理：{'、'.join(parts)}" if parts else "当前没有可清理的缓存",
            "data": {"caches": _cache_rows(plugin)},
        }
    )


# ── GET /platforms ──────────────────────────────────────────────────────


async def api_platforms(plugin):
    from quart import jsonify

    return jsonify(
        {
            "success": True,
            "data": {
                "platforms": platforms.describe_all(plugin.enabled_platforms),
                "errors": platforms.load_errors(),
            },
        }
    )


# ── POST /probe ─────────────────────────────────────────────────────────


async def api_probe(plugin):
    """试解析：不发送到群、不写会话状态、不占用去重窗口。

    唯一的副作用是打了一次 B 站/下载站接口，以及可能消耗一次 AI 总结——
    所以带限流，并且允许调用方显式关掉总结（``summary=false``）。
    """
    from quart import jsonify, request

    global _last_probe_at

    body = await request.get_json(silent=True) or {}
    text = str(body.get("text") or "").strip()
    if not text:
        return jsonify({"success": False, "message": "请输入一个链接或分享口令"})

    now = time.time()
    if now - _last_probe_at < PROBE_MIN_INTERVAL:
        wait = PROBE_MIN_INTERVAL - (now - _last_probe_at)
        return jsonify(
            {"success": False, "message": f"试解析太频繁了，请 {wait:.1f} 秒后再试"}
        )
    _last_probe_at = now

    hit = platforms.match(text, enabled=plugin.enabled_platforms)
    if hit is None:
        return jsonify(
            {
                "success": False,
                "message": "没认出这是哪个平台的内容。目前支持："
                + "、".join(
                    row["display"]
                    for row in platforms.describe_all(plugin.enabled_platforms)
                    if row.get("enabled")
                ),
            }
        )

    platform_cls, ref = hit
    platform = await plugin._platform(platform_cls.name)
    if platform is None:
        return jsonify(
            {"success": False, "message": f"平台「{platform_cls.display}」未启用或初始化失败"}
        )

    want_summary = bool(body.get("summary", True)) and plugin.enable_summary
    started = time.time()
    try:
        media = await platform.parse(
            ref,
            hint=ParseHint(
                reply_limit=plugin.reply_limit,
                want_summary=True,
                want_subtitle=True,
            ),
        )
    except PlatformError as exc:
        return jsonify(
            {
                "success": False,
                "message": str(exc),
                "data": {"detail": exc.detail, "platform": platform_cls.display},
            }
        )
    except Exception as exc:  # noqa: BLE001 - 面板要把异常说清楚，而不是 500
        logger.warning(f"{LOG_TAG} 试解析异常：{type(exc).__name__}: {exc}")
        return jsonify({"success": False, "message": f"{type(exc).__name__}: {exc}"})

    parse_ms = int((time.time() - started) * 1000)

    # AI 总结需要一个会话上下文才能取到模型；面板没有会话，所以：
    # 用调用方指定的 umo → 最近一次解析的 umo → 都没有就跳过（不偷偷花 token）
    summary: str | None = None
    basis: str | None = None
    summary_note = ""
    umo = str(body.get("umo") or "").strip() or _recent_umo(plugin)
    if want_summary:
        if not umo:
            summary_note = "面板没有会话上下文，未生成总结（可先让机器人在某个群解析一次）"
        else:
            from .core.summarizer import summarize_video

            replies = media.extra("replies") or []
            from .core.bili_api import REPLY_MAX_CHARS

            summary, basis = await summarize_video(
                plugin.context,
                umo=umo,
                media=media,
                replies=[r.content[:REPLY_MAX_CHARS] for r in replies] or None,
                max_chars=plugin.summary_max_chars,
                timeout=plugin.summary_timeout,
            )
            if not summary:
                summary_note = "总结未生成（模型未配置或调用失败）"

    # 卡片预览
    card, card_note = await _preview_card(plugin, media, summary, basis)

    # 直链可用性：真正走一遍平台的取直链逻辑（B 站会打下载站，因此要 try）
    media_items: list[dict[str, Any]] = []
    try:
        items = await platform.resolve_media(media)
    except PlatformError as exc:
        items = []
        card_note = (card_note + " ｜ " if card_note else "") + f"取直链失败：{exc}"
    except Exception as exc:  # noqa: BLE001
        items = []
        card_note = (card_note + " ｜ " if card_note else "") + f"取直链异常：{type(exc).__name__}"

    for item in items:
        media_items.append(
            {
                "kind": item.kind,
                "filename": item.filename,
                "host": _url_host(item.url),
                "size_hint": item.size_hint,
            }
        )

    replies = media.extra("replies") or []
    return jsonify(
        {
            "success": True,
            "data": {
                "platform": media.platform,
                "platform_display": platform_cls.display,
                "media_id": media.media_id,
                "page": media.page,
                "title": media.title,
                "author": media.author,
                "author_avatar": media.author_avatar,
                "cover": media.cover,
                "duration": media.duration,
                "desc": media.desc,
                "stats": media.stats,
                "source_url": media.source_url,
                "web_url": platform.web_url(media),
                "capabilities": sorted(platform.capabilities),
                "items": media_items,
                "replies_count": len(replies),
                "tags": list(media.extra("tags") or []),
                "summary": summary or "",
                "summary_basis": basis or "",
                "summary_note": summary_note,
                "card": card,
                "card_note": card_note,
                "parse_ms": parse_ms,
                "umo": umo,
            },
        }
    )


def _recent_umo(plugin) -> str:
    """最近一次解析发生在哪个会话——试解析用它来取模型。"""
    rows = plugin.history.recent(limit=20)
    for row in rows:
        umo = str(row.get("umo") or "").strip()
        if umo:
            return umo
    return ""


def _url_host(url: str) -> str:
    """只回主机名：直链带签名，完整回传又长又没意义。"""
    from urllib.parse import urlparse

    try:
        return urlparse(url).hostname or ""
    except ValueError:
        return ""


async def _preview_card(plugin, media, summary: str | None, basis: str | None) -> tuple[str, str]:
    """返回 (前端可用的图片 src, 提示文案)。

    ``html_render`` 可能返回 http URL，也可能返回本地路径；后者要读文件转
    data URI，否则前端（sandbox iframe）取不到。
    """
    if not plugin.output_image:
        return "", "图片卡片已关闭"
    if plugin._card_disabled:
        return "", "卡片渲染已熔断（连续失败），当前走纯文本"

    try:
        url = await plugin._render_card(media, summary, basis)
    except Exception as exc:  # noqa: BLE001
        return "", f"卡片渲染异常：{type(exc).__name__}"

    if not url:
        return "", "卡片渲染失败（t2i 服务不可用？），实际会退化成纯文本"

    text = str(url)
    if text.startswith(("http://", "https://", "data:")):
        return text, ""

    path = Path(text)
    try:
        if not path.is_file():
            return "", "卡片已生成但拿不到文件"
        if path.stat().st_size > PREVIEW_MAX_BYTES:
            return "", "卡片文件过大，未回传预览"
        payload = base64.b64encode(path.read_bytes()).decode()
    except OSError as exc:
        return "", f"读取卡片文件失败：{type(exc).__name__}"
    suffix = path.suffix.lstrip(".").lower() or "jpeg"
    if suffix == "jpg":
        suffix = "jpeg"
    return f"data:image/{suffix};base64,{payload}", ""


# ── 凭据 ────────────────────────────────────────────────────────────────

#: 各平台凭据面板的说明（面板顶部提示用）
_CREDENTIAL_NOTE = {
    "bilibili": "填了能用字幕做总结；不填完全不需要登录，功能照常。",
    "xiaohongshu": "部分笔记不登录只能看到部分内容，填 Cookie 成功率更高。",
    "douyin": "抖音自带签名校验，插件无法直接取数据；填第三方解析接口后才会真正下载。",
}


async def api_credentials(plugin):
    from quart import jsonify

    return jsonify(
        {
            "success": True,
            "data": {
                "platforms": plugin.credentials.status(),
                "notes": _CREDENTIAL_NOTE,
                "path": str(plugin.credentials.path),
            },
        }
    )


async def api_credentials_set(plugin):
    """保存或清除一项凭据。

    返回体里**不含明文**，只有更新后的状态（是否配置 + 尾号）。
    """
    from quart import jsonify, request

    body = await request.get_json(silent=True) or {}
    platform = str(body.get("platform") or "").strip().lower()
    key = str(body.get("key") or "").strip()
    value = str(body.get("value") or "")

    changed, message = plugin.credentials.set(platform, key, value)
    if changed:
        # 丢掉已构造的实例，下次解析时按新凭据重建（否则旧 cookie 还在内存里）
        plugin._platforms.pop(platform, None)

    return jsonify(
        {
            "success": changed,
            "message": message,
            "data": {"platforms": plugin.credentials.status()},
        }
    )


async def api_credentials_test(plugin):
    """用当前保存的凭据真发一次请求，告诉用户这条 key 到底有没有用。"""
    from quart import jsonify, request

    body = await request.get_json(silent=True) or {}
    platform = str(body.get("platform") or "").strip().lower()

    ok, message = await _test_credential(plugin, platform)
    return jsonify({"success": ok, "message": message})


async def _test_credential(plugin, platform: str) -> tuple[bool, str]:
    if platform not in _CREDENTIAL_NOTE:
        return False, f"未知平台：{platform}"

    creds = plugin.credentials.platform(platform)
    if not creds:
        return False, "还没有配置凭据"

    instance = await plugin._platform(platform)
    if instance is None:
        return False, "平台未启用或初始化失败"

    session = await plugin._get_session()

    if platform == "bilibili":
        return await _test_bilibili(session, instance)
    if platform == "xiaohongshu":
        return await _test_xiaohongshu(session, instance)
    return await _test_douyin(session, instance)


async def _test_bilibili(session, instance) -> tuple[bool, str]:
    """用 nav 接口验证登录态：这是 B 站判断"当前是否登录"的标准接口。"""
    sessdata = getattr(instance, "sessdata", "")
    if not sessdata:
        return False, "没有配置 SESSDATA"
    try:
        async with session.get(
            "https://api.bilibili.com/x/web-interface/nav",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
                ),
                "Referer": "https://www.bilibili.com/",
                "Cookie": f"SESSDATA={sessdata}",
            },
            timeout=timeout_for(instance.timeout),
        ) as resp:
            payload = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        return False, f"请求 B 站失败：{type(exc).__name__}"

    data = payload.get("data") or {}
    if payload.get("code") == 0 and data.get("isLogin"):
        return True, f"登录态有效（{data.get('uname') or '已登录'}）"
    return False, "SESSDATA 无效或已过期（B 站返回未登录）"


async def _test_xiaohongshu(session, instance) -> tuple[bool, str]:
    """用带 Cookie 的探索页判断登录态。

    小红书没有稳定的公开"我是谁"接口（要签名），所以这里做的是弱验证：
    带上 Cookie 请求探索页，看服务端是否把页面导向登录/验证。结论用词也如实说明。
    """
    cookie = getattr(instance, "cookie", "")
    if not cookie:
        return False, "没有配置 Cookie"
    if "web_session" not in cookie:
        return False, "Cookie 里没有 web_session，多半不是登录态（只是游客 cookie）"

    headers = {"Referer": "https://www.xiaohongshu.com/"}
    try:
        headers.update(instance._headers(html=True))  # noqa: SLF001 - 复用平台的 UA
    except Exception:  # noqa: BLE001
        pass

    try:
        async with session.get(
            "https://www.xiaohongshu.com/explore",
            headers=headers,
            timeout=timeout_for(instance.timeout),
            allow_redirects=True,
        ) as resp:
            if resp.status >= 400:
                return False, f"小红书返回 HTTP {resp.status}"
            text = await resp.text(errors="replace")
    except Exception as exc:  # noqa: BLE001
        return False, f"请求小红书失败：{type(exc).__name__}"

    if "登录后查看" in text or "请先登录" in text:
        return False, "页面仍要求登录，Cookie 可能已失效"

    # 页面 SSR 里有 user.loggedIn / user.userInfo.nickname：
    # 能读到就直接告诉用户「登的是谁」，这比"看起来有效"有用得多
    # （小红书没有免签名的「我是谁」接口，这是唯一不依赖 x-s 签名的地方）。
    state = _xhs_page_state(text)
    user = state.get("user") if isinstance(state, dict) else None
    if isinstance(user, dict):
        if user.get("loggedIn"):
            info = user.get("userInfo") or user.get("user_info") or {}
            name = str(info.get("nickname") or info.get("nickName") or "").strip()
            user_id = str(info.get("userId") or "").strip()
            detail = name + (f"（{user_id}）" if user_id else "")
            return True, f"已登录：{detail}" if detail else "已登录（Cookie 有效）"
        if "loggedIn" in user:
            return False, "Cookie 无效：小红书返回的是未登录状态（loggedIn = false）"

    if "noteData" in text or "__INITIAL_STATE__" in text:
        return True, "Cookie 看起来有效（能拿到笔记数据页）；实际效果以解析结果为准"
    return False, "拿到的不是笔记页（可能被风控），建议换一条链接再试"


def _xhs_page_state(html: str) -> dict:
    """从探索页 HTML 里取 ``window.__INITIAL_STATE__``。

    解析失败返回空字典——这里只是"尽力告诉用户登的是谁"，不该因为
    页面结构变动就把一次本来成功的验证判成失败。
    """
    match = re.search(
        r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>", html, re.S
    )
    if not match:
        return {}
    try:
        return json.loads(match.group(1).replace("undefined", "null"))
    except ValueError:
        return {}


async def _test_douyin(session, instance) -> tuple[bool, str]:
    """抖音只能验证第三方接口是否可达、是否返回 JSON。"""
    api_url = getattr(instance, "api_url", "")
    if not api_url:
        return False, "还没有配置第三方解析接口"

    endpoint = api_url.replace("{url}", "https%3A%2F%2Fwww.douyin.com%2F")
    try:
        async with session.get(
            endpoint,
            headers={"Accept": "application/json, */*"},
            timeout=timeout_for(instance.timeout),
        ) as resp:
            if resp.status >= 400:
                return False, f"接口返回 HTTP {resp.status}"
            body = await resp.text(errors="replace")
    except Exception as exc:  # noqa: BLE001
        return False, f"接口请求失败：{type(exc).__name__}"

    try:
        json.loads(body)
    except ValueError:
        return False, "接口返回的不是 JSON（多数解析服务是 JSON，纯文本无法解析）"
    return True, "接口可达且返回 JSON；真正能否解析以群里的实际结果为准"


# ── 运行期调整（面板可写） ──────────────────────────────────────────────


async def api_settings(plugin):
    """面板「各种调整」的唯一数据源：生效值 + 来源 + 控件元信息。

    每一项都告诉面板两件事：现在生效的是什么值、这个值来自面板还是 AstrBot
    配置。用户因此不会看到「面板写 40、配置写 50」这种猜谜局面。
    """
    from quart import jsonify

    from .core.runtime import PLATFORMS_KEY, RUNTIME_META

    store = plugin.runtime
    baseline = plugin.config if isinstance(plugin.config, dict) else {}

    items = []
    for key, meta in RUNTIME_META.items():
        from_panel = store.has(key)
        # 优先回显运行属性上的**实际生效值**：`_apply_runtime` 已经按
        # 「面板 → 配置 → 代码默认」算过一遍，这里再独立算一次容易两边不一致
        # （早期实现的「恢复后仍显示旧值」就是这么来的）。
        value = getattr(plugin, key, None)
        if value is None:
            value = store.get(key) if from_panel else baseline.get(key)
        items.append(
            {
                "key": key,
                "label": meta.get("label", key),
                "group": meta.get("group", ""),
                "kind": meta.get("kind", "str"),
                "unit": meta.get("unit", ""),
                "value": value,
                "source": "panel" if from_panel else "config",
            }
        )

    enabled = plugin.runtime.platforms()
    return jsonify(
        {
            "success": True,
            "data": {
                "items": items,
                "platforms": enabled if enabled is not None else plugin.enabled_platforms,
                "platform_source": "panel" if enabled is not None else "config",
                "platforms_key": PLATFORMS_KEY,
                "path": str(plugin.runtime.path),
            },
        }
    )


async def api_settings_set(plugin):
    """保存调整项。``null`` 表示删掉该覆盖、退回配置值。"""
    from quart import jsonify, request

    body = await request.get_json(silent=True) or {}
    values = body.get("values")
    if not isinstance(values, dict) or not values:
        return jsonify({"success": False, "message": "没有可保存的项"})

    applied, rejected = plugin.apply_runtime(values)
    message = f"已应用 {len(applied)} 项"
    if rejected:
        message += f"，忽略未知项：{', '.join(rejected)}"
    # 平台启停变了要让前端重新拉一次状态
    return jsonify(
        {
            "success": bool(applied),
            "message": message,
            "data": {"applied": applied, "rejected": rejected},
        }
    )


# ── B 站扫码登录 ────────────────────────────────────────────────────────


async def api_bili_qrcode(plugin):
    """POST 生成二维码并开始轮询；GET 读当前扫码状态。

    一个接口两种方法：前端先 POST 拿码，再定时 GET 读进度，语义上就是
    「同一个扫码会话」，拆成两个路径反而要在前端记两套。
    """
    from quart import jsonify, request

    if request.method.upper() == "GET":
        return jsonify({"success": True, "data": plugin.bili_login.status()})

    session = await plugin._get_session()
    state = await plugin.bili_login.start(session, timeout=plugin.request_timeout)
    return jsonify(
        {
            "success": state.get("state") not in {"failed"},
            "message": state.get("message", ""),
            "data": state,
        }
    )


async def api_bili_logout(plugin):
    """清除扫码得到的 SESSDATA（配置里那份不受影响，面板值是覆盖层）。"""
    from quart import jsonify

    plugin.bili_login.cancel("")
    plugin.credentials.set("bilibili", "sessdata", "")
    plugin.credentials.set("bilibili", "bili_jct", "")
    plugin.reload_platforms()
    return jsonify(
        {
            "success": True,
            "message": "已清除面板里的登录态（AstrBot 配置里的 sessdata 仍然有效）",
            "data": {"platforms": plugin.credentials.status()},
        }
    )


def timeout_for(seconds: int):
    import aiohttp

    return aiohttp.ClientTimeout(total=max(5, int(seconds)))


__all__ = ["register"]
