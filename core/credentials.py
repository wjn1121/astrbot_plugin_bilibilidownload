"""平台凭据存储（登录态 / 第三方接口地址）。

为什么放在数据目录而不是插件配置里：

- ``_conf_schema.json`` 是 AstrBot 的配置面，改它要走 AstrBot 的保存流程，而且
  一旦用户在面板改过、又在原生配置页保存，两边会打架（与 OPTIMIZATION §8A
  否决「配置分组」是同一条理由）；
- 凭据是敏感数据，单独一个文件能收紧权限（600）、单独备份、单独删除，
  误删 config 也不会把登录态一起清掉。

安全约束（都在代码里落实）：

- 文件权限 600（POSIX）；Windows 没有等价物，退化为默认权限；
- **不写日志**：任何异常信息里都不带凭据值；
- 面板只回显「是否配置 + 尾号 4 位 + 长度」，永不返回明文；
- 写入前做长度与字符集校验，避免把整段网页/换行粘进来。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .constants import LOG_TAG

# 单个凭据值的长度上限：Cookie 可能很长，但也不该是几 MB
MAX_VALUE_LEN = 4096

# 各平台的凭据字段（面板据此渲染输入框，平台侧据此取值）
CREDENTIAL_FIELDS: dict[str, dict[str, dict[str, str]]] = {
    "bilibili": {
        "sessdata": {
            "label": "SESSDATA",
            "hint": "从浏览器 Cookie 里复制 SESSDATA，用于读取视频字幕（建议用小号）。留空则不需要登录。",
            "secret": "1",
            "placeholder": "如 3a7f...%2C1793...",
        }
    },
    "xiaohongshu": {
        "cookie": {
            "label": "Cookie",
            "hint": "整段 Cookie（含 web_session）。部分笔记不登录只能看到部分内容，填了成功率更高。",
            "secret": "1",
            "placeholder": "web_session=xxx; a1=xxx; ...",
        }
    },
    "douyin": {
        "api_url": {
            "label": "第三方解析接口",
            "hint": "抖音自带 a_bogus 签名校验，插件无法直接取数据。填一个能返回 JSON 的解析接口，"
            "支持 {url} 占位符（如 https://api.example.com/parse?url={url}）；留空则只识别不解析。",
            "secret": "",
            "placeholder": "https://api.example.com/parse?url={url}",
        }
    },
}


def _tail(value: str, length: int = 4) -> str:
    if not value:
        return ""
    return value[-length:] if len(value) > length else "*" * len(value)


class CredentialStore:
    """``<data_dir>/credentials.json`` 的读写封装。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._data: dict[str, dict[str, str]] = {}
        self._load()

    # ── 读 ──────────────────────────────────────────────────────────────

    def platform(self, name: str) -> dict[str, str]:
        """取某个平台的凭据字典（不含空值）。"""
        row = self._data.get((name or "").strip().lower()) or {}
        return {key: value for key, value in row.items() if value}

    def get(self, name: str, key: str, default: str = "") -> str:
        return self.platform(name).get(key, default)

    def status(self) -> dict[str, Any]:
        """面板用：每个平台每个字段是否已配置、尾号、长度。**不含明文**。"""
        rows: dict[str, Any] = {}
        for platform, fields in CREDENTIAL_FIELDS.items():
            current = self.platform(platform)
            rows[platform] = {
                key: {
                    "label": meta.get("label", key),
                    "hint": meta.get("hint", ""),
                    "secret": bool(meta.get("secret")),
                    "placeholder": meta.get("placeholder", ""),
                    "configured": bool(current.get(key)),
                    "tail": _tail(current.get(key, "")),
                    "length": len(current.get(key, "")),
                }
                for key, meta in fields.items()
            }
        return rows

    # ── 写 ──────────────────────────────────────────────────────────────

    def set(self, name: str, key: str, value: str) -> tuple[bool, str]:
        """保存一个凭据值；``value`` 为空表示删除该项。返回 (是否变更, 提示)。"""
        platform = (name or "").strip().lower()
        field = (key or "").strip()
        allowed = CREDENTIAL_FIELDS.get(platform) or {}
        if field not in allowed:
            return False, f"未知的凭据项：{platform}.{field}"

        text = (value or "").strip()
        if len(text) > MAX_VALUE_LEN:
            return False, f"内容过长（{len(text)} 字符，上限 {MAX_VALUE_LEN}）"
        # Cookie 里允许换行外的控制字符很少见，直接拒绝，避免粘进整段网页
        if any(ch in text for ch in ("\r", "\n", "\x00")):
            return False, "内容里不能有换行或控制字符"

        row = self._data.setdefault(platform, {})
        if not text:
            if field not in row:
                return False, "本来就没有配置"
            row.pop(field, None)
            if not row:
                self._data.pop(platform, None)
            self._flush()
            return True, "已清除"

        if row.get(field) == text:
            return False, "内容没有变化"
        row[field] = text
        self._flush()
        return True, "已保存"

    def clear_platform(self, name: str) -> bool:
        platform = (name or "").strip().lower()
        if platform not in self._data:
            return False
        self._data.pop(platform, None)
        self._flush()
        return True

    # ── 内部 ────────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # 只记异常类型，不把文件内容带进日志
            logger.warning(f"{LOG_TAG} 读取凭据文件失败（忽略）：{type(exc).__name__}")
            return

        platforms = raw.get("platforms") if isinstance(raw, dict) else None
        if not isinstance(platforms, dict):
            return
        for name, row in platforms.items():
            if not isinstance(row, dict):
                continue
            cleaned = {
                str(key): str(value)
                for key, value in row.items()
                if isinstance(value, (str, int, float)) and str(value).strip()
            }
            if cleaned:
                self._data[str(name).strip().lower()] = cleaned

    def _flush(self) -> None:
        payload = {"version": 1, "platforms": self._data}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass  # Windows 上没有 POSIX 权限，忽略
            tmp.replace(self.path)
        except OSError as exc:
            logger.warning(f"{LOG_TAG} 写入凭据文件失败：{type(exc).__name__}")


__all__ = ["CREDENTIAL_FIELDS", "MAX_VALUE_LEN", "CredentialStore"]
