"""极简 QR 码生成（纯标准库，输出 SVG）。

为什么不引依赖：本插件的运行时依赖只有 ``aiohttp``（见 OPTIMIZATION §9.1），
二维码只是热评图右上角的一个装饰元素，不值得为它新增依赖或内建一张图片资源。
这里走和 ``core/wbi.py`` 一样的路子——用标准库实现算法。

实现范围**刻意收窄**，只为「放一条 B 站视频链接」服务：

- 字节模式（内容按 UTF-8 编码后直接写入）；
- 纠错等级固定 **M**（约 15% 容错）；
- 版本固定 **4**（33×33，容量 62 字节，任何 B 站视频链接都放得下）。版本 < 7
  因此**不需要版本信息区**，少掉最容易写错的一段代码；
- 八个掩码全部实现并按标准罚分选最优（避免出现大片同色导致扫不出来）。

⚠️ **未实机验证**：开发环境无法执行代码，本模块既没跑过、也没扫过。
它是纯函数、失败时由调用方静默跳过，不影响卡片其余部分；扫不出来时把配置项
``card_qrcode`` 关掉即可。
"""

from __future__ import annotations

# ── 版本 4 + 纠错等级 M 的固定参数（ISO/IEC 18004 表 9）──────────────
_VERSION = 4
_SIZE = 17 + 4 * _VERSION  # 33
_DATA_CODEWORDS = 64  # 数据码字总数
_BLOCKS = 2  # 分块数
_DATA_PER_BLOCK = _DATA_CODEWORDS // _BLOCKS  # 32
_EC_PER_BLOCK = 18  # 每块纠错码字数

# 版本 4 的对齐图案中心坐标
_ALIGN_CENTERS = (6, 26)

# 字节模式可用容量：数据码字 × 8 位 − 4 位模式指示符 − 8 位字符计数
MAX_BYTES = _DATA_CODEWORDS - 2  # 62

# 纠错等级 M 在格式信息里的指示符（L=01、M=00、Q=11、H=10）
_EC_LEVEL_BITS = 0b00

# 八个掩码的判定函数（返回 True 表示该模块取反）
_MASKS = (
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
)

# 定位图案样（用于罚分规则 3）
_FINDER_LIKE = (True, False, True, True, True, False, True)


# ── GF(256) 与 Reed-Solomon ──────────────────────────────────────────


def _gf_tables() -> tuple[list[int], list[int]]:
    """构造 GF(256) 的指数表与对数表（本原多项式 0x11D）。"""
    exp = [0] * 512
    log = [0] * 256
    value = 1
    for i in range(255):
        exp[i] = value
        log[value] = i
        value <<= 1
        if value & 0x100:
            value ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


_GF_EXP, _GF_LOG = _gf_tables()


def _gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _GF_EXP[_GF_LOG[a] + _GF_LOG[b]]


def _generator_poly(degree: int) -> list[int]:
    """返回 degree 次生成多项式的系数（最高次在 index 0）。"""
    poly = [1]
    for i in range(degree):
        # 依次乘 (x + α^i)
        expanded = [0] * (len(poly) + 1)
        for j, coef in enumerate(poly):
            expanded[j] ^= coef
            expanded[j + 1] ^= _gf_mul(coef, _GF_EXP[i])
        poly = expanded
    return poly


def _rs_remainder(data: list[int], degree: int) -> list[int]:
    """用生成多项式做除法，取余数作为纠错码字。"""
    gen = _generator_poly(degree)
    remainder = [0] * degree
    for byte in data:
        factor = byte ^ remainder[0]
        remainder = remainder[1:] + [0]
        for i in range(degree):
            remainder[i] ^= _gf_mul(gen[i + 1], factor)
    return remainder


# ── 数据编码 ─────────────────────────────────────────────────────────


def _encode_data(payload: bytes) -> list[int]:
    """字节模式编码：模式指示符 + 字符计数 + 数据 + 终止符 + 补齐字节。"""
    if len(payload) > MAX_BYTES:
        raise ValueError(f"内容过长：{len(payload)} 字节，上限 {MAX_BYTES}")

    bits: list[int] = []

    def push(value: int, length: int) -> None:
        for shift in range(length - 1, -1, -1):
            bits.append((value >> shift) & 1)

    push(0b0100, 4)  # 字节模式
    push(len(payload), 8)  # 版本 1~9 的字符计数固定 8 位
    for byte in payload:
        push(byte, 8)

    # 终止符最多 4 位，剩余空间不足就按实际补
    push(0, min(4, _DATA_CODEWORDS * 8 - len(bits)))

    # 补齐到字节边界
    while len(bits) % 8:
        bits.append(0)

    codewords: list[int] = []
    current = 0
    for index, bit in enumerate(bits):
        current = (current << 1) | bit
        if index % 8 == 7:
            codewords.append(current)
            current = 0

    # 交替填充 0xEC / 0x11 直到填满
    padding = (0xEC, 0x11)
    pad_index = 0
    while len(codewords) < _DATA_CODEWORDS:
        codewords.append(padding[pad_index % 2])
        pad_index += 1
    return codewords


def _build_codewords(payload: bytes) -> list[int]:
    """分块做 RS 纠错，再按标准交织成最终码字序列。"""
    data = _encode_data(payload)
    blocks = [
        data[i * _DATA_PER_BLOCK : (i + 1) * _DATA_PER_BLOCK] for i in range(_BLOCKS)
    ]
    ec_blocks = [_rs_remainder(block, _EC_PER_BLOCK) for block in blocks]

    result: list[int] = []
    for i in range(_DATA_PER_BLOCK):  # 数据码字交织
        for block in blocks:
            result.append(block[i])
    for i in range(_EC_PER_BLOCK):  # 纠错码字交织
        for block in ec_blocks:
            result.append(block[i])
    return result


# ── 矩阵构建 ─────────────────────────────────────────────────────────


def _blank() -> tuple[list[list[bool | None]], list[list[bool]]]:
    matrix: list[list[bool | None]] = [[None] * _SIZE for _ in range(_SIZE)]
    reserved = [[False] * _SIZE for _ in range(_SIZE)]
    return matrix, reserved


def _place_finder(
    matrix: list[list[bool | None]], reserved: list[list[bool]], row0: int, col0: int
) -> None:
    """7×7 定位图案，外圈一圈分隔符（只标记占用，保持浅色）。"""
    for r in range(-1, 8):
        for c in range(-1, 8):
            row, col = row0 + r, col0 + c
            if not (0 <= row < _SIZE and 0 <= col < _SIZE):
                continue
            reserved[row][col] = True
            if 0 <= r <= 6 and 0 <= c <= 6:
                on_edge = r in (0, 6) or c in (0, 6)
                in_core = 2 <= r <= 4 and 2 <= c <= 4
                matrix[row][col] = on_edge or in_core


def _place_timing(matrix: list[list[bool | None]], reserved: list[list[bool]]) -> None:
    """第 6 行 / 第 6 列的时序图案（i 为偶数时是深色）。"""
    for i in range(8, _SIZE - 8):
        dark = i % 2 == 0
        matrix[6][i] = dark
        reserved[6][i] = True
        matrix[i][6] = dark
        reserved[i][6] = True


def _place_alignment(
    matrix: list[list[bool | None]], reserved: list[list[bool]]
) -> None:
    """对齐图案（版本 4 共 4 个位置，与定位图案重叠的三个跳过）。"""
    for row0 in _ALIGN_CENTERS:
        for col0 in _ALIGN_CENTERS:
            if (row0, col0) in ((6, 6), (6, 26), (26, 6)):
                continue
            for r in range(-2, 3):
                for c in range(-2, 3):
                    row, col = row0 + r, col0 + c
                    if reserved[row][col]:
                        continue
                    reserved[row][col] = True
                    on_edge = abs(r) == 2 or abs(c) == 2
                    matrix[row][col] = on_edge or (r == 0 and c == 0)


def _reserve_format(reserved: list[list[bool]]) -> None:
    """标记两处格式信息占用的位置（内容稍后由 _place_format 写入）。"""
    for i in range(9):
        reserved[8][i] = True
        reserved[i][8] = True
    for i in range(8):
        reserved[8][_SIZE - 1 - i] = True
        reserved[_SIZE - 1 - i][8] = True


def _place_data(
    matrix: list[list[bool | None]], reserved: list[list[bool]], codewords: list[int]
) -> None:
    """把码字按之字形（自右下起、逐列两格、上下往复）填进非功能模块。"""
    bits = [
        (byte >> shift) & 1 for byte in codewords for shift in range(7, -1, -1)
    ]
    index = 0
    upward = True
    col = _SIZE - 1
    while col > 0:
        if col == 6:  # 第 6 列整列是竖向时序图案
            col -= 1
        for step in range(_SIZE):
            row = _SIZE - 1 - step if upward else step
            for c in (col, col - 1):
                if reserved[row][c]:
                    continue
                # 码字位数多于此处的模块数时（不该发生）留浅色即可
                matrix[row][c] = bool(bits[index]) if index < len(bits) else False
                index += 1
        upward = not upward
        col -= 2


def _format_bits(mask: int) -> int:
    """15 位格式信息：2 位纠错等级 + 3 位掩码 + 10 位 BCH 校验，最后异或固定常量。"""
    data = (_EC_LEVEL_BITS << 3) | mask
    remainder = data << 10
    generator = 0b10100110111
    while remainder.bit_length() > 10:
        remainder ^= generator << (remainder.bit_length() - 11)
    return ((data << 10) | remainder) ^ 0b101010000010010


def _place_format(matrix: list[list[bool]], mask: int) -> None:
    """写入两处格式信息。

    位序按 ISO/IEC 18004 图 25：拷贝 1 的 bit0 在 (8,0)，沿**第 8 行**向右，
    到 (8,8) 后转到**第 8 列**向上，bit14 落在 (0,8)；拷贝 2 的 bit0 在
    (8, size-1) 向左 8 位，再转到第 8 列向上，bit14 落在 (size-7, 8)。
    两处都要跳过第 6 行 / 第 6 列的时序图案。
    """
    bits = _format_bits(mask)

    for i in range(15):
        bit = bool((bits >> i) & 1)
        if i < 6:
            matrix[8][i] = bit  # (8,0) …… (8,5)
        elif i == 6:
            matrix[8][7] = bit
        elif i == 7:
            matrix[8][8] = bit
        elif i == 8:
            matrix[7][8] = bit
        else:
            matrix[14 - i][8] = bit  # i=9 → (5,8) …… i=14 → (0,8)

    for i in range(15):
        bit = bool((bits >> i) & 1)
        if i < 8:
            matrix[8][_SIZE - 1 - i] = bit  # (8,32) …… (8,25)
        else:
            matrix[_SIZE - 1 - (i - 8)][8] = bit  # (32,8) …… (26,8)


def _apply_mask(
    matrix: list[list[bool | None]], reserved: list[list[bool]], mask: int
) -> list[list[bool]]:
    """对非功能模块套用掩码，返回纯布尔矩阵。"""
    fn = _MASKS[mask]
    return [
        [
            (bool(matrix[r][c]) ^ fn(r, c)) if not reserved[r][c] else bool(matrix[r][c])
            for c in range(_SIZE)
        ]
        for r in range(_SIZE)
    ]


def _penalty(matrix: list[list[bool]]) -> int:
    """标准四条罚分规则，分数越低越好。"""
    score = 0

    # 规则 1：行 / 列里连续同色 ≥5 个
    lines = [list(row) for row in matrix]
    lines += [list(col) for col in zip(*matrix)]
    for line in lines:
        run = 1
        for i in range(1, _SIZE):
            if line[i] == line[i - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
        if run >= 5:
            score += 3 + (run - 5)

    # 规则 2：2×2 同色块
    for r in range(_SIZE - 1):
        for c in range(_SIZE - 1):
            if matrix[r][c] == matrix[r][c + 1] == matrix[r + 1][c] == matrix[r + 1][c + 1]:
                score += 3

    # 规则 3：形似定位图案的 1:1:3:1:1，且一侧带 4 个浅色模块
    def has_quiet_run(values: list[bool]) -> bool:
        return len(values) >= 4 and not any(values)

    for r in range(_SIZE):
        for c in range(_SIZE - 6):
            if tuple(matrix[r][c + k] for k in range(7)) != _FINDER_LIKE:
                continue
            left = [matrix[r][c - k - 1] for k in range(4)] if c >= 4 else []
            right = (
                [matrix[r][c + 7 + k] for k in range(4)] if c + 11 <= _SIZE else []
            )
            if has_quiet_run(left) or has_quiet_run(right):
                score += 40
    for c in range(_SIZE):
        for r in range(_SIZE - 6):
            if tuple(matrix[r + k][c] for k in range(7)) != _FINDER_LIKE:
                continue
            up = [matrix[r - k - 1][c] for k in range(4)] if r >= 4 else []
            down = (
                [matrix[r + 7 + k][c] for k in range(4)] if r + 11 <= _SIZE else []
            )
            if has_quiet_run(up) or has_quiet_run(down):
                score += 40

    # 规则 4：深色模块比例偏离 50% 的程度
    dark = sum(1 for row in matrix for cell in row if cell)
    percent = dark * 100 / (_SIZE * _SIZE)
    score += 10 * (int(abs(percent - 50)) // 5)

    return score


def _best_mask(
    matrix: list[list[bool | None]], reserved: list[list[bool]]
) -> list[list[bool]]:
    """八种掩码各评一次分，取最低的那个。"""
    best: list[list[bool]] | None = None
    best_score = -1
    for index in range(8):
        candidate = _apply_mask(matrix, reserved, index)
        _place_format(candidate, index)
        score = _penalty(candidate)
        if best is None or score < best_score:
            best, best_score = candidate, score
    assert best is not None  # 循环必定至少跑一次，仅用于类型收窄
    return best


def _matrix(payload: bytes) -> list[list[bool]]:
    matrix, reserved = _blank()
    _place_finder(matrix, reserved, 0, 0)
    _place_finder(matrix, reserved, 0, _SIZE - 7)
    _place_finder(matrix, reserved, _SIZE - 7, 0)
    _place_timing(matrix, reserved)
    _place_alignment(matrix, reserved)
    # 固定的暗模块
    matrix[_SIZE - 8][8] = True
    reserved[_SIZE - 8][8] = True
    _reserve_format(reserved)
    _place_data(matrix, reserved, _build_codewords(payload))
    return _best_mask(matrix, reserved)


# ── 对外接口 ─────────────────────────────────────────────────────────


def qr_svg(text: str, *, scale: int = 4, border: int = 2) -> str:
    """把 ``text`` 编码成二维码并返回 SVG 字符串。

    ``border`` 是静区宽度（单位：模块），标准要求至少 4 个模块，这里按 SVG 的
    视觉留白取 2 并靠白色底图补足；内容超过 62 字节时抛 ``ValueError``。
    """
    if not text:
        raise ValueError("内容为空")

    width = _SIZE + border * 2
    dimension = width * scale
    modules = _matrix(text.encode("utf-8"))

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{dimension}" '
        f'height="{dimension}" viewBox="0 0 {dimension} {dimension}" '
        f'shape-rendering="crispEdges">',
        f'<rect width="{dimension}" height="{dimension}" fill="#FFFFFF"/>',
        '<g fill="#18191C">',
    ]
    # 按行合并连续的深色模块：逐格一个 rect 会让 SVG 膨胀到几十 KB
    for r in range(_SIZE):
        c = 0
        while c < _SIZE:
            if not modules[r][c]:
                c += 1
                continue
            start = c
            while c < _SIZE and modules[r][c]:
                c += 1
            parts.append(
                f'<rect x="{(start + border) * scale}" y="{(r + border) * scale}" '
                f'width="{(c - start) * scale}" height="{scale}"/>'
            )
    parts.append("</g></svg>")
    return "".join(parts)


__all__ = ["MAX_BYTES", "qr_svg"]
