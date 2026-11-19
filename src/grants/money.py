"""金额精度工具。

按资料约定的精度（默认两位小数、ROUND_HALF_UP）在聚合层统一舍入。
逐条记录先按未舍入的 :class:`Decimal` 参与运算，避免按合同独立舍入
而凭空产生可拨余额；每次聚合舍入的残差会登记并随报告披露。
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, localcontext

ZERO = Decimal("0")


def money(value, places: int = 2) -> Decimal:
    """按约定精度对一个已聚合金额舍入。"""
    quant = Decimal(1).scaleb(-places)
    return Decimal(value).quantize(quant, rounding=ROUND_HALF_UP)


def money_input(text: str) -> Decimal:
    """解析输入金额，输入本身的精度不被提前改写。"""
    return Decimal(str(text))


def aggregate(values, places: int):
    """先精确求和，再在聚合层舍入，返回 (舍入值, 残差)。

    残差 = 精确合计 - 舍入合计，保留全部有效位，供报告核对，
    不得把残差塞进任何合同的可拨余额。
    """
    with localcontext() as ctx:
        ctx.prec = 50
        exact = sum((Decimal(v) for v in values), ZERO)
    rounded = money(exact, places)
    return rounded, exact - rounded
