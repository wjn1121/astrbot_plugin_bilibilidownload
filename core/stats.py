"""轻量运行统计，供 ``/bdlstatus`` 展示。

定位与边界：
- 只保留「计数 + 最近一次」的量级信息，不做时序存储、不落盘；
- **绝不记录凭据**：这里只放错误描述与计数，`sessdata` 之类的值不得进入；
- 内存占用恒定（路径数量固定为 3），长期运行不会增长。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class Attempt:
    """某个环节最近一次尝试的结果。"""

    name: str
    ok: bool
    detail: str
    at: float


class RuntimeStats:
    """进程内运行统计。"""

    def __init__(self) -> None:
        self.started_at = time.time()

        # 计数
        self.parse_ok = 0
        self.parse_fail = 0
        self.download_ok = 0
        self.download_fail = 0
        self.card_ok = 0
        self.card_fail = 0
        self.send_fail = 0

        # 最近一次错误（只留一条，避免无界增长）
        self.last_error_where = ""
        self.last_error_detail = ""
        self.last_error_at = 0.0

        # 下载站各路径最近一次尝试（固定 3 条）
        self.media_attempts: dict[str, Attempt] = {}

    # ------------------------------------------------------------------ 记录

    def note_error(self, where: str, detail: str) -> None:
        """记录最近一次失败；新错误覆盖旧错误。"""
        self.last_error_where = where
        self.last_error_detail = detail
        self.last_error_at = time.time()

    def note_media_attempt(self, name: str, ok: bool, detail: str = "") -> None:
        """由 ``EbilibiliClient`` 构造时传入的 on_attempt 回调驱动。"""
        self.media_attempts[name] = Attempt(name=name, ok=ok, detail=detail, at=time.time())

    # ------------------------------------------------------------------ 读取

    def uptime(self) -> float:
        return max(0.0, time.time() - self.started_at)

    def last_error(self) -> Attempt | None:
        if not self.last_error_at:
            return None
        return Attempt(
            name=self.last_error_where,
            ok=False,
            detail=self.last_error_detail,
            at=self.last_error_at,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "uptime": self.uptime(),
            "parse_ok": self.parse_ok,
            "parse_fail": self.parse_fail,
            "download_ok": self.download_ok,
            "download_fail": self.download_fail,
            "card_ok": self.card_ok,
            "card_fail": self.card_fail,
            "send_fail": self.send_fail,
            "last_error": self.last_error(),
            "media_attempts": dict(self.media_attempts),
        }


__all__ = ["Attempt", "RuntimeStats"]
