"""B 站 WBI 查询串签名。

用途：给 ``/x/player/wbi/v2`` 这类接口的查询参数追加 ``wts`` 与 ``w_rid``，
否则 B 站会返回 ``code=-403``。

为什么自己实现：算法很短（一张 64 项置换表 + 一次 MD5），标准库就能完成，
没必要为它引入依赖——本插件的运行时依赖保持只有 aiohttp。

⚠️ 这属于 B 站未公开约定的实现，随时可能调整。调用方必须把「签名后仍拿不到
数据」当作可降级错误处理（见 ``subtitle.fetch_subtitle`` 的返回 None 约定）。
"""

from __future__ import annotations

import hashlib
import re
import time
import urllib.parse

# 64 项置换表：B 站用它把 img_key + sub_key 重排成 mixin key
MIXIN_KEY_ENC_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)

# 签名前必须过滤掉这些字符，漏掉会导致 w_rid 不匹配
_FILTER_CHARS = re.compile(r"[!'()*]")


def mixin_key(img_key: str, sub_key: str) -> str:
    """由 img_key / sub_key 派生出 32 位 mixin key。

    用 ``index % len(raw)`` 取字符而不是直接索引：万一上游给了短串，
    这里宁可算出无效 key（后续请求失败会降级），也不要抛 IndexError。
    """
    raw = f"{img_key}{sub_key}"
    if not raw:
        return ""
    return "".join(raw[index % len(raw)] for index in MIXIN_KEY_ENC_TAB)[:32]


def sign(
    params: dict[str, object], img_key: str, sub_key: str, wts: int | None = None
) -> dict[str, str]:
    """返回带 ``wts`` / ``w_rid`` 的新字典（不改动入参）。

    ``wts`` 默认取当前时间戳；显式传入是为了让离线测试能用固定向量。
    """
    key = mixin_key(img_key, sub_key)
    merged = {str(k): str(v) for k, v in params.items()}
    merged["wts"] = str(wts if wts is not None else int(time.time()))

    query = urllib.parse.urlencode(
        [(k, _FILTER_CHARS.sub("", v)) for k, v in sorted(merged.items())]
    )
    merged["w_rid"] = hashlib.md5(f"{query}{key}".encode()).hexdigest()
    return merged


def key_from_url(url: str | None) -> str:
    """从 ``https://i0.hdslb.com/bfs/wbi/xxxxxxxx.png`` 里取出 ``xxxxxxxx``。"""
    if not url:
        return ""
    name = str(url).rsplit("/", 1)[-1]
    return name.split(".")[0].strip()


__all__ = ["MIXIN_KEY_ENC_TAB", "key_from_url", "mixin_key", "sign"]
