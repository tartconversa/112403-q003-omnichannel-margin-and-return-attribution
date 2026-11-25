"""金额处理工具。

系统内所有金额一律以「分」为单位的整数存储，避免二进制浮点误差。
输入文件以元为单位（可带 2 位小数），由 :func:`yuan_to_cents` 转换；
输出再由 :func:`cents_to_yuan` 还原为字符串。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation


def yuan_to_cents(raw: str | int | float | Decimal | None) -> int:
    """把以元为单位的输入转换为整数分。

    空单元格视为 0（用于可选费用列）。拒绝非数字输入，避免静默吞错。
    """
    if raw is None:
        return 0
    if isinstance(raw, int):
        return raw * 100
    text = str(raw).strip()
    if text == "":
        return 0
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"无法解析金额: {raw!r}") from exc
    # 银行家舍入会让对账更难解释，统一四舍五入到分
    cents = int((value * 100).quantize(Decimal("1"), rounding="ROUND_HALF_UP"))
    return cents


def cents_to_yuan(cents: int) -> str:
    """整数分格式化为两位小数字符串。"""
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def split_cents(total_cents: int, weights: list[int]) -> list[int]:
    """按权重把一笔整数分拆成若干份，保证各份之和严格等于原值。

    使用「最大余数法」处理除不尽的尾差：先按比例取整，再把剩余的每一分
    依次分给余数最大的份。每张凭证的分摊依据都会随结果落库，可逐行核对。
    权重全为 0 时按等份处理（仍然可核对），而不是任意按销售额摊派。
    """
    n = len(weights)
    if n == 0:
        return []
    if n == 1:
        return [total_cents]
    weight_sum = sum(weights)
    if weight_sum <= 0:
        weights = [1] * n
        weight_sum = n
    # 全程整数运算：先取整除，再用最大余数法分配尾差，杜绝浮点小数分
    raw_quotients = [total_cents * w for w in weights]
    floors = [q // weight_sum for q in raw_quotients]
    remainders = [(q % weight_sum, i) for i, q in enumerate(raw_quotients)]
    leftover = total_cents - sum(floors)
    result = floors[:]
    if leftover >= 0:
        # 余数大的先分
        for _, i in sorted(remainders, reverse=True)[:leftover]:
            result[i] += 1
    else:
        for _, i in sorted(remainders)[:(-leftover)]:
            result[i] -= 1
    assert sum(result) == total_cents
    return result
