"""B 站扫码登录：在面板里生成二维码，手机确认后自动把 SESSDATA 存进凭据文件。

## 流程（都是 B 站 Web 端自己的接口，不涉及第三方）

1. ``GET passport.bilibili.com/x/passport-login/web/qrcode/generate``
   → 拿到 ``url``（要编码成二维码的内容）和 ``qrcode_key``（轮询凭据）
2. 把 ``url`` 画成二维码丢给面板，用户用 B 站 App 扫
3. 后台每 2 秒 ``GET .../qrcode/poll?qrcode_key=...``，返回体里的 ``data.code``：
   - ``86101`` 未扫码
   - ``86090`` 已扫码，等手机上确认
   - ``0``     确认完成，Cookie 在响应头 ``Set-Cookie`` 里
   - ``86038`` 二维码过期（约 180 秒）
4. 确认成功后**先校验再落盘**：用候选 Cookie 请求 ``x/web-interface/nav``，
   只有 ``isLogin`` 为真才写入 ``credentials.json``。绝不拿一个没验证过的
   值去顶掉用户原来还能用的登录态。

## 为什么是"单会话"

同一个插件实例里只保留一个登录会话：面板是个页面，用户不会同时扫两张码，
而多会话要处理 task_id 回收、状态字典上限、UI 轮询对不上等一堆边角。
重复点「获取二维码」直接取消上一次，语义更简单，也不会漏任务。

## 二维码渲染

优先 ``qrcode`` + ``PIL``（AstrBot 本体自带，不新增依赖）输出 base64 PNG ——
PNG 自带白底，深色面板上也能扫。缺库时退到 SVG；两者都没有就如实报错，
让用户手动填 Cookie，而不是显示一个扫不出来的码。
"""

from __future__ import annotations

import asyncio
import base64
import io
from typing import Any, Callable

from astrbot.api import logger

from .constants import LOG_TAG

GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"

#: 轮询间隔（秒）。B 站二维码有效期 180 秒，2 秒一轮约 90 次请求，足够及时也不冒犯。
POLL_INTERVAL = 2.0
#: 二维码总有效期（秒），略小于 B 站自己的 180，留出收尾余量。
QR_TTL = 175.0

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}

#: 交给面板的状态机取值
STATE_IDLE = "idle"
STATE_WAITING = "waiting"
STATE_SCANNED = "scanned"
STATE_SUCCESS = "success"
STATE_EXPIRED = "expired"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"


def render_qr_png(content: str) -> str:
    """把字符串画成 base64 PNG（``data:image/png;base64,...``）。

    失败抛异常，由调用方决定怎么降级——这里不静默返回空串，否则面板会
    出现一个空白框，比报错更难排查。
    """
    import qrcode  # AstrBot 本体自带；缺失时 ImportError 交由调用方处理

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(content)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def render_qr_svg(content: str) -> str:
    """退路：输出 SVG 字符串（需要面板自己给白底）。"""
    import qrcode
    import qrcode.image.svg

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=2,
    )
    qr.add_data(content)
    qr.make(fit=True)
    image = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
    buffer = io.BytesIO()
    image.save(buffer)
    return buffer.getvalue().decode("utf-8", "replace")


def render_qr(content: str) -> tuple[str, str]:
    """返回 (形式, 内容)：``("png", data_uri)`` 或 ``("svg", markup)``。"""
    try:
        return "png", render_qr_png(content)
    except Exception as exc:  # noqa: BLE001 - 缺 PIL 时退 SVG
        logger.debug(f"{LOG_TAG} PNG 二维码生成失败，改用 SVG：{type(exc).__name__}: {exc}")
    return "svg", render_qr_svg(content)


def _cookie_from_set_cookie(headers: list[str]) -> dict[str, str]:
    """把 ``Set-Cookie`` 头拆成键值对。

    只取每段第一个 ``k=v``：后面的 ``Path`` / ``Expires`` / ``HttpOnly`` 等
    属性对重放登录态没有意义，混进去还会污染 Cookie 串。
    """
    cookies: dict[str, str] = {}
    for header in headers:
        part = header.split(";")[0].strip()
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if key:
            cookies[key] = value.strip()
    return cookies


async def check_sessdata(session, sessdata: str, timeout: float = 15.0) -> dict[str, Any]:
    """用 nav 接口验证 SESSDATA 是否有效。返回 ``{valid, username, uid, error}``。"""
    if not sessdata:
        return {"valid": False, "username": "", "uid": 0, "error": "没有 SESSDATA"}
    try:
        async with session.get(
            NAV_URL,
            headers={"User-Agent": UA, "Referer": "https://www.bilibili.com/",
                     "Cookie": f"SESSDATA={sessdata}"},
            timeout=timeout,
        ) as resp:
            payload = await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001 - 网络问题不该被当成"登录失效"
        return {
            "valid": False,
            "username": "",
            "uid": 0,
            "error": f"请求 B 站失败：{type(exc).__name__}",
        }

    data = payload.get("data") or {}
    if payload.get("code") == 0 and data.get("isLogin"):
        return {
            "valid": True,
            "username": str(data.get("uname") or ""),
            "uid": int(data.get("mid") or 0),
            "error": "",
        }
    # -101 = 账号未登录，这是"这个 SESSDATA 确实不认"
    return {
        "valid": False,
        "username": "",
        "uid": 0,
        "error": str(payload.get("message") or "Cookie 无效"),
    }


class BiliQrLogin:
    """单会话的扫码登录状态机。

    ``on_success`` 在写盘成功后调用，用来让插件丢掉旧的平台实例（否则内存里
    还是没登录的那个）。
    """

    def __init__(
        self,
        credentials,
        *,
        on_success: Callable[[str, int], None] | None = None,
    ) -> None:
        self._credentials = credentials
        self._on_success = on_success
        self._task: asyncio.Task | None = None
        self._state: dict[str, Any] = {
            "state": STATE_IDLE,
            "message": "还没有开始扫码",
            "username": "",
            "uid": 0,
            "started_at": 0.0,
            "expires_in": 0,
            "qr": "",
            "qr_kind": "",
        }

    # ── 状态 ────────────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        payload = dict(self._state)
        payload["running"] = self._task is not None and not self._task.done()
        return payload

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    # ── 开始 / 取消 ──────────────────────────────────────────────────

    async def start(self, session, *, timeout: float = 20.0) -> dict[str, Any]:
        """生成二维码并启动轮询。返回面板要的状态（含二维码）。"""
        self.cancel("已取消上一次扫码")

        try:
            async with session.get(
                GENERATE_URL,
                headers=HEADERS,
                timeout=timeout,
            ) as resp:
                payload = await resp.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            self._state = {
                **self._idle_state(),
                "state": STATE_FAILED,
                "message": f"请求 B 站生成二维码失败：{type(exc).__name__}",
            }
            return self.status()

        data = payload.get("data") or {}
        key = str(data.get("qrcode_key") or "")
        url = str(data.get("url") or "")
        if payload.get("code") != 0 or not key or not url:
            self._state = {
                **self._idle_state(),
                "state": STATE_FAILED,
                "message": f"B 站没有返回二维码：{payload.get('message') or '未知原因'}",
            }
            return self.status()

        try:
            qr_kind, qr_content = render_qr(url)
        except Exception as exc:  # noqa: BLE001 - 缺 qrcode/PIL
            self._state = {
                **self._idle_state(),
                "state": STATE_FAILED,
                "message": (
                    "当前环境无法生成二维码（缺少 qrcode/Pillow）。"
                    f"可以改用「手动填 Cookie」，或安装依赖后重试：{type(exc).__name__}"
                ),
            }
            return self.status()

        self._state = {
            "state": STATE_WAITING,
            "message": "用 B 站手机客户端扫码",
            "username": "",
            "uid": 0,
            "started_at": asyncio.get_event_loop().time(),
            "expires_in": int(QR_TTL),
            "qr": qr_content,
            "qr_kind": qr_kind,
        }
        self._task = asyncio.create_task(self._poll_loop(session, key, timeout))
        return self.status()

    def cancel(self, message: str = "已取消") -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
        if message and self._state.get("state") in {STATE_WAITING, STATE_SCANNED}:
            self._state = {**self._state, "state": STATE_CANCELLED, "message": message,
                           "qr": "", "qr_kind": ""}

    # ── 轮询 ────────────────────────────────────────────────────────

    async def _poll_loop(self, session, qrcode_key: str, timeout: float) -> None:
        loop = asyncio.get_event_loop()
        started = loop.time()
        try:
            while True:
                if loop.time() - started > QR_TTL:
                    self._state = {
                        **self._state,
                        "state": STATE_EXPIRED,
                        "message": "二维码已过期，请重新获取",
                        "qr": "",
                        "qr_kind": "",
                    }
                    return

                await asyncio.sleep(POLL_INTERVAL)

                try:
                    async with session.get(
                        POLL_URL,
                        params={"qrcode_key": qrcode_key},
                        headers=HEADERS,
                        timeout=timeout,
                    ) as resp:
                        payload = await resp.json(content_type=None)
                        set_cookies = resp.headers.getall("Set-Cookie", [])
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - 单次失败不该终止整轮
                    logger.debug(f"{LOG_TAG} 扫码轮询单次失败：{type(exc).__name__}")
                    continue

                data = payload.get("data") or {}
                code = data.get("code")

                if code == 86038:
                    self._state = {
                        **self._state,
                        "state": STATE_EXPIRED,
                        "message": "二维码已过期，请重新获取",
                        "qr": "",
                        "qr_kind": "",
                    }
                    return

                if code == 86090:
                    self._state = {
                        **self._state,
                        "state": STATE_SCANNED,
                        "message": "已扫码，请在手机上点「确认登录」",
                        "expires_in": max(0, int(QR_TTL - (loop.time() - started))),
                    }
                    continue

                if code == 86101:
                    self._state = {
                        **self._state,
                        "expires_in": max(0, int(QR_TTL - (loop.time() - started))),
                    }
                    continue

                if code != 0:
                    self._state = {
                        **self._state,
                        "state": STATE_FAILED,
                        "message": str(data.get("message") or "扫码登录失败"),
                        "qr": "",
                        "qr_kind": "",
                    }
                    return

                # ── 登录成功：先校验，通过了才落盘 ──
                cookies = _cookie_from_set_cookie(set_cookies)
                sessdata = cookies.get("SESSDATA", "")
                if not sessdata:
                    self._state = {
                        **self._state,
                        "state": STATE_FAILED,
                        "message": "B 站没有返回 SESSDATA，请重试",
                        "qr": "",
                        "qr_kind": "",
                    }
                    return

                verdict = await check_sessdata(session, sessdata, timeout)
                if not verdict["valid"]:
                    self._state = {
                        **self._state,
                        "state": STATE_FAILED,
                        "message": (
                            "拿到的 Cookie 没通过校验，已保留原有登录态："
                            f"{verdict.get('error') or '未知原因'}"
                        ),
                        "qr": "",
                        "qr_kind": "",
                    }
                    return

                changed, message = self._credentials.set("bilibili", "sessdata", sessdata)
                if not changed:
                    self._state = {
                        **self._state,
                        "state": STATE_FAILED,
                        "message": f"保存 SESSDATA 失败：{message}",
                        "qr": "",
                        "qr_kind": "",
                    }
                    return

                # 顺手把 bili_jct 也留下：它是部分写操作的 CSRF token，
                # 现在用不上，但用户以后要接别的接口时不用再扫一次。
                if cookies.get("bili_jct"):
                    self._credentials.set("bilibili", "bili_jct", cookies["bili_jct"])

                username = verdict["username"]
                uid = verdict["uid"]
                self._state = {
                    "state": STATE_SUCCESS,
                    "message": f"登录成功：{username or '已登录'}（UID {uid}）",
                    "username": username,
                    "uid": uid,
                    "started_at": self._state.get("started_at", 0.0),
                    "expires_in": 0,
                    "qr": "",
                    "qr_kind": "",
                }
                logger.info(f"{LOG_TAG} B站扫码登录成功：{username}（UID {uid}）")

                if self._on_success is not None:
                    try:
                        self._on_success(username, uid)
                    except Exception as exc:  # noqa: BLE001 - 回调失败不影响登录结果
                        logger.warning(f"{LOG_TAG} 扫码登录回调失败：{type(exc).__name__}: {exc}")
                return

        except asyncio.CancelledError:
            if self._state.get("state") in {STATE_WAITING, STATE_SCANNED}:
                self._state = {
                    **self._state,
                    "state": STATE_CANCELLED,
                    "message": "扫码已取消",
                    "qr": "",
                    "qr_kind": "",
                }
            raise
        except Exception as exc:  # noqa: BLE001 - 任何意外都要落到一个可读状态
            logger.warning(f"{LOG_TAG} 扫码轮询异常：{type(exc).__name__}: {exc}")
            self._state = {
                **self._state,
                "state": STATE_FAILED,
                "message": f"扫码过程出错：{type(exc).__name__}",
                "qr": "",
                "qr_kind": "",
            }

    @staticmethod
    def _idle_state() -> dict[str, Any]:
        return {
            "state": STATE_IDLE,
            "message": "",
            "username": "",
            "uid": 0,
            "started_at": 0.0,
            "expires_in": 0,
            "qr": "",
            "qr_kind": "",
        }


__all__ = [
    "BiliQrLogin",
    "STATE_CANCELLED",
    "STATE_EXPIRED",
    "STATE_FAILED",
    "STATE_IDLE",
    "STATE_SCANNED",
    "STATE_SUCCESS",
    "STATE_WAITING",
    "check_sessdata",
    "render_qr",
]
