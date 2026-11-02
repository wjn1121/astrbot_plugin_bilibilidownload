"""面板的「运行期可调项」——一层盖在 AstrBot 配置之上的覆盖。

## 为什么要这一层

面板里要能改东西（体积上限、热评条数、卡片宽度……），但这些值本来在
``_conf_schema.json`` 里，由 AstrBot 的原生配置面板管理。如果面板直接写配置，
就出现两处写入同一份数据：用户在哪边改都可能被另一边静默覆盖，而且面板写的
值会被 AstrBot 下次保存配置时整份重写掉。

所以做**单向覆盖**：

- AstrBot 配置 = 基线（留在原位，随时可在原生面板改）
- ``data/runtime.json`` = 覆盖层（只有面板会写，键集合白名单固定）
- 生效值 = 覆盖层有就用覆盖层，没有就退回配置

面板上每一项都标注「来自面板 / 来自配置」，用户不会看到两个互相矛盾的值。

## 落盘位置

``data/plugin_data/<plugin>/runtime.json``，与 ``history.json``、
``credentials.json`` 同目录 —— 插件升级、市场更新都不会碰它。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .constants import LOG_TAG

#: 允许面板写的键 → (类型, 下限, 上限)。
#:
#: 白名单是硬约束：``process_value`` 只认这里列出的键，其余一律拒绝。
#: 上下限与 ``main.py`` 里的 clamp 保持一致，避免"面板能存进去、运行时不认"。
RUNTIME_SPEC: dict[str, tuple[str, int | None, int | None]] = {
    # 下载与发送
    "send_video": ("bool", None, None),
    "max_video_mb": ("int", 1, 2000),
    "video_send_mode": ("str", None, None),
    "send_ebilibili_link": ("bool", None, None),
    # 卡片
    "output_image": ("bool", None, None),
    "card_width": ("int", 560, 1000),
    "card_qrcode": ("bool", None, None),
    # 热评
    "show_replies": ("bool", None, None),
    "replies_show_count": ("int", 0, 10),
    "reply_limit": ("int", 0, 30),
    # AI 总结与字幕
    "enable_summary": ("bool", None, None),
    "summary_max_chars": ("int", 50, 2000),
    "prefer_subtitle": ("bool", None, None),
    "subtitle_max_chars": ("int", 500, 20000),
    # 图文平台
    "xhs_send_images": ("bool", None, None),
    "xhs_max_images": ("int", 1, 35),
}

#: 代码默认值，取值顺序里的最后一环：**面板覆盖 → AstrBot 配置 → 这里**。
#:
#: 为什么必须有这一层：点「恢复」只是删掉面板覆盖，如果这个键在 AstrBot 配置里
#: 也不存在（比如 ``xhs_max_images`` 就没进 ``_conf_schema.json``），就取不到值，
#: 属性会停在面板上次写的数上——界面上写着「来自配置」，实际还在用旧值。
#: 其余键在这里也写一份，是为了让「恢复」在配置被清空时也能落到一个确定的数。
RUNTIME_DEFAULTS: dict[str, Any] = {
    "send_video": False,
    "max_video_mb": 40,
    "video_send_mode": "auto",
    "send_ebilibili_link": True,
    "output_image": True,
    "card_width": 760,
    "card_qrcode": True,
    "show_replies": True,
    "replies_show_count": 5,
    "reply_limit": 10,
    "enable_summary": True,
    "summary_max_chars": 400,
    "prefer_subtitle": True,
    "subtitle_max_chars": 6000,
    "xhs_send_images": True,
    "xhs_max_images": 5,
}

#: 展示用元信息（面板用它生成控件，避免前后端各写一份键名）
RUNTIME_META: dict[str, dict[str, Any]] = {
    "send_video": {"label": "解析后自动发视频", "group": "bilibili", "kind": "bool"},
    "max_video_mb": {"label": "视频体积上限 (MB)", "group": "bilibili", "kind": "int", "unit": "MB"},
    "send_ebilibili_link": {"label": "附下载页链接", "group": "bilibili", "kind": "bool"},
    "output_image": {"label": "图片卡片", "group": "bilibili", "kind": "bool"},
    "card_width": {"label": "卡片宽度", "group": "bilibili", "kind": "int", "unit": "px"},
    "card_qrcode": {"label": "热评图带二维码", "group": "bilibili", "kind": "bool"},
    "show_replies": {"label": "发送热评图", "group": "bilibili", "kind": "bool"},
    "replies_show_count": {"label": "热评展示条数", "group": "bilibili", "kind": "int", "unit": "条"},
    "reply_limit": {"label": "参考热评条数", "group": "bilibili", "kind": "int", "unit": "条"},
    "enable_summary": {"label": "AI 总结", "group": "common", "kind": "bool"},
    "summary_max_chars": {"label": "总结字数上限", "group": "common", "kind": "int", "unit": "字"},
    "prefer_subtitle": {"label": "优先用字幕做总结", "group": "bilibili", "kind": "bool"},
    "subtitle_max_chars": {"label": "字幕截取长度", "group": "bilibili", "kind": "int", "unit": "字"},
    # 图集：抖音和小红书都有多图作品，所以两项都挂到这两个平台页上。
    # 键名带 xhs_ 前缀是历史原因（先做的小红书），改名会让已存的 runtime.json 失效。
    "xhs_send_images": {
        "label": "自动发送图集",
        "group": ["douyin", "xiaohongshu"],
        "kind": "bool",
    },
    "xhs_max_images": {
        "label": "图集最多发送",
        "group": ["douyin", "xiaohongshu"],
        "kind": "int",
        "unit": "张",
    },
}

#: 面板里允许改的平台启停（写进 runtime 后覆盖配置的 enabled_platforms）
PLATFORMS_KEY = "enabled_platforms"


def _clamp_int(value: Any, low: int | None, high: int | None) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if low is not None:
        number = max(low, number)
    if high is not None:
        number = min(high, number)
    return number


def _to_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on", "开"}:
            return True
        if text in {"0", "false", "no", "off", "关"}:
            return False
    return None


class RuntimeStore:
    """``data/runtime.json`` 的读写封装。

    写失败只记日志、不抛异常：面板上的一个调整项存不下，不该让整个保存请求 500。
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._data: dict[str, Any] = {}
        self._load()

    # ── 读 ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - 坏文件按"没有覆盖"处理
            logger.warning(f"{LOG_TAG} 运行期配置读取失败（按默认处理）：{exc}")
            return
        if isinstance(raw, dict):
            self._data = {
                str(key): value
                for key, value in raw.items()
                if key in RUNTIME_SPEC or key == PLATFORMS_KEY
            }

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def has(self, key: str) -> bool:
        return key in self._data

    def platforms(self) -> list[str] | None:
        """面板里保存的平台启用列表；没保存过返回 None（表示沿用配置）。"""
        value = self._data.get(PLATFORMS_KEY)
        if isinstance(value, list):
            return [str(item).strip().lower() for item in value if str(item).strip()]
        return None

    def snapshot(self) -> dict[str, Any]:
        return dict(self._data)

    # ── 写 ──────────────────────────────────────────────────────────

    def set_many(self, values: dict[str, Any]) -> tuple[list[str], list[str]]:
        """批量写入。返回 (已应用键, 被拒键)。

        只接受白名单内的键；``None`` 表示"删掉这条覆盖，退回配置值"。
        """
        applied: list[str] = []
        rejected: list[str] = []

        for key, raw in values.items():
            name = str(key).strip()
            if name == PLATFORMS_KEY:
                if raw is None:
                    self._data.pop(PLATFORMS_KEY, None)
                    applied.append(name)
                elif isinstance(raw, list):
                    self._data[PLATFORMS_KEY] = [
                        str(item).strip().lower() for item in raw if str(item).strip()
                    ]
                    applied.append(name)
                else:
                    rejected.append(name)
                continue

            spec = RUNTIME_SPEC.get(name)
            if spec is None:
                rejected.append(name)
                continue

            kind, low, high = spec
            if raw is None:
                self._data.pop(name, None)
                applied.append(name)
                continue

            if kind == "bool":
                parsed = _to_bool(raw)
            elif kind == "int":
                parsed = _clamp_int(raw, low, high)
            else:
                parsed = str(raw).strip() or None

            if parsed is None:
                rejected.append(name)
                continue

            self._data[name] = parsed
            applied.append(name)

        self._flush()
        return applied, rejected

    def clear(self) -> None:
        self._data.clear()
        self._flush()

    def _flush(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except Exception as exc:  # noqa: BLE001 - 存不下只是这次调整没生效，不该炸接口
            logger.warning(f"{LOG_TAG} 运行期配置保存失败：{type(exc).__name__}: {exc}")


__all__ = ["PLATFORMS_KEY", "RUNTIME_META", "RUNTIME_SPEC", "RuntimeStore"]
