"""最近解析记录（面板「最近」页的数据源）。

设计取舍：

- **环形队列 + 落盘**：插件重启后面板不该变成空白，但也不能无界增长，
  所以只保留最近 ``history_keep`` 条（默认 50），新的在最前；
- **同步小文件写入**：每次解析只追加一条、文件通常几十 KB，直接写比引一个
  异步 IO 队列简单得多；真到写入失败（磁盘满、只读挂载）也只记日志，
  绝不让面板数据影响解析主流程；
- **不存凭据**：只记平台、内容 ID、标题、结果与耗时。群号（umo）存下来是为了
  面板能分辨"哪个群在发"，与群聊记忆的处理一致，面板接口可以选择不返回。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .constants import LOG_TAG

DEFAULT_KEEP = 50


@dataclass(slots=True)
class HistoryEntry:
    """一条解析记录。"""

    at: float = field(default_factory=time.time)
    platform: str = ""
    media_id: str = ""
    title: str = ""
    page: int = 1
    ok: bool = True
    detail: str = ""       # 失败原因（成功时为空）
    ms: int = 0            # 解析耗时（毫秒）
    card_ok: bool = False  # 这次是否成功出图（false 时走的是纯文本降级）
    umo: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class HistoryStore:
    """定长历史记录，落盘到 ``<data_dir>/history.json``。"""

    def __init__(self, path: Path | str, keep: int = DEFAULT_KEEP) -> None:
        # 允许传 str：调用方有时手里只有配置里读出来的字符串路径
        self.path = Path(path)
        self._entries: list[HistoryEntry] = []
        self.set_keep(keep)
        self._load()

    # ── 配置 ────────────────────────────────────────────────────────────

    def set_keep(self, keep: int) -> None:
        """条数上限。填 0 表示不记录（调用方据此跳过 add）。"""
        try:
            self.keep = max(0, int(keep))
        except (TypeError, ValueError):
            self.keep = DEFAULT_KEEP
        if self.keep and len(self._entries) > self.keep:
            del self._entries[self.keep :]

    @property
    def enabled(self) -> bool:
        return self.keep > 0

    # ── 读写 ────────────────────────────────────────────────────────────

    def add(self, entry: HistoryEntry) -> None:
        if not self.enabled:
            return
        self._entries.insert(0, entry)
        del self._entries[self.keep :]
        self._flush()

    def recent(self, limit: int | None = None, platform: str = "") -> list[dict[str, Any]]:
        """倒序返回记录；``platform`` 非空时只返回该平台。"""
        items = self._entries
        if platform:
            items = [item for item in items if item.platform == platform]
        if limit is not None and limit > 0:
            items = items[:limit]
        return [item.to_dict() for item in items]

    def clear(self) -> int:
        count = len(self._entries)
        self._entries.clear()
        self._flush()
        return count

    def count(self) -> int:
        return len(self._entries)

    # ── 内部 ────────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning(f"{LOG_TAG} 读取解析历史失败（忽略）：{type(exc).__name__}: {exc}")
            return

        rows = payload.get("entries") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return

        loaded: list[HistoryEntry] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                loaded.append(
                    HistoryEntry(
                        at=float(row.get("at") or 0),
                        platform=str(row.get("platform") or ""),
                        media_id=str(row.get("media_id") or ""),
                        title=str(row.get("title") or ""),
                        page=int(row.get("page") or 1),
                        ok=bool(row.get("ok", True)),
                        detail=str(row.get("detail") or ""),
                        ms=int(row.get("ms") or 0),
                        card_ok=bool(row.get("card_ok")),
                        umo=str(row.get("umo") or ""),
                    )
                )
            except (TypeError, ValueError):
                # 单条坏数据不该让整份历史作废
                continue
        self._entries = loaded[: self.keep] if self.keep else []

    def _flush(self) -> None:
        if not self.enabled:
            return
        payload = {
            "version": 1,
            "saved_at": time.time(),
            "entries": [item.to_dict() for item in self._entries],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            # 先写临时文件再原子替换：写一半被中断时不会留下坏 JSON
            tmp.replace(self.path)
        except OSError as exc:
            logger.warning(f"{LOG_TAG} 写入解析历史失败（忽略）：{type(exc).__name__}: {exc}")


__all__ = ["DEFAULT_KEEP", "HistoryEntry", "HistoryStore"]
