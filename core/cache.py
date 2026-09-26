"""进程内 TTL 缓存（带容量上限与命中统计）。

为什么自研而不引入 cachetools：这里的需求只有「TTL + 容量上限 + 给 /bdlstatus
看的计数」，一个 dict 惰性清理就够，能继续把运行时依赖保持在 aiohttp 一个。

设计取舍：
- 惰性清理，不启后台任务（插件生命周期里没有合适的调度点，后台任务还要处理取消）；
- 容量超限时淘汰「最早过期」的条目——比精确 LRU 简单，且对本插件这种
  「同一视频被多个群先后发出」的访问模式足够；
- 线程/协程安全：单事件循环内使用，dict 操作无 await 打断，不需要锁。
"""

from __future__ import annotations

import time
from typing import Any


class TtlCache:
    """带 TTL 与容量上限的进程内缓存。"""

    def __init__(self, *, max_items: int = 256, ttl: float = 300.0, name: str = "cache") -> None:
        self._max_items = max(1, int(max_items))
        self._ttl = max(1.0, float(ttl))
        self._name = name
        self._data: dict[str, tuple[float, Any]] = {}
        self._hits = 0
        self._misses = 0

    # ------------------------------------------------------------------ 读写

    def get(self, key: str) -> Any | None:
        """取值；过期视为未命中并顺手删除。"""
        item = self._data.get(key)
        if item is None:
            self._misses += 1
            return None

        expires_at, value = item
        if expires_at <= time.time():
            self._data.pop(key, None)
            self._misses += 1
            return None

        self._hits += 1
        return value

    def set(self, key: str, value: Any) -> None:
        self._evict_if_needed()
        self._data[key] = (time.time() + self._ttl, value)

    def clear(self) -> int:
        """清空缓存并返回被清理的条目数（供 /bdlclear 反馈）。"""
        count = len(self._data)
        self._data.clear()
        return count

    # ------------------------------------------------------------------ 统计

    def stats(self) -> dict[str, Any]:
        """返回统计快照；命中率用于判断缓存是否真的起作用。"""
        total = self._hits + self._misses
        return {
            "name": self._name,
            "size": len(self._data),
            "max_items": self._max_items,
            "ttl": self._ttl,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": (self._hits / total) if total else 0.0,
        }

    # ------------------------------------------------------------------ 内部

    def _evict_if_needed(self) -> None:
        """写入前腾出空间：先清过期，再按最早过期淘汰。"""
        if len(self._data) < self._max_items:
            return

        now = time.time()
        for key in [k for k, (expires_at, _) in self._data.items() if expires_at <= now]:
            self._data.pop(key, None)

        overflow = len(self._data) - self._max_items + 1
        if overflow > 0:
            oldest = sorted(self._data.items(), key=lambda item: item[1][0])[:overflow]
            for key, _ in oldest:
                self._data.pop(key, None)


__all__ = ["TtlCache"]
