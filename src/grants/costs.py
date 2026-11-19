"""共同支出与可报销费用分析。

规则：

* 同一事件各合同的获批份额合计超过策略 ``share_sum_limit`` 即判定超分，
  该事件全部费用先排除在可支付金额之外，并逐条说明卡点；新的合资格费用
  不得掩盖未处理的重叠。
* 审批状态不是 approved 的费用不得计入（pending 等状态逐条解释）。
* 没有任何合资格签到支撑的事件费用视为课程不合资格。
* 可报销额 = 金额 × 获批份额，先以未舍入 :class:`Decimal` 累计，
  按合同在聚合层统一舍入并登记舍入残差。
"""
from __future__ import annotations

from decimal import Decimal

from .models import (
    BLOCK_INELIGIBLE_EVENT,
    BLOCK_PENDING_APPROVAL,
    BLOCK_SHARE_OVERALLOCATION,
    AttendanceRecord,
    BlockedItem,
    CostLine,
    CostRecord,
)
from .money import ZERO, aggregate, money


def analyze_costs(
    costs: list[CostRecord],
    attendance: list[AttendanceRecord],
    policy: dict,
    places: int,
) -> tuple[tuple[CostLine, ...], list[BlockedItem], dict[str, Decimal], Decimal]:
    limit = Decimal(str(policy.get("share_sum_limit", 1.0)))
    events: dict[str, list[CostRecord]] = {}
    for rec in costs:
        events.setdefault(rec.event, []).append(rec)

    eligible_events = {rec.event for rec in attendance if rec.eligible}
    overallocated: set[str] = set()
    blocks: list[BlockedItem] = []

    for event, rows in sorted(events.items()):
        share_sum = sum((r.eligible_share for r in rows), ZERO)
        amounts = {r.amount for r in rows}
        if share_sum > limit:
            overallocated.add(event)
            detail = (
                f"事件 {event} 获批份额合计 {share_sum} 超过上限 {limit}："
                + ", ".join(f"{r.contract}={r.eligible_share}" for r in rows)
            )
            for r in rows:
                blocks.append(
                    BlockedItem(
                        "cost",
                        r.expense,
                        r.contract,
                        BLOCK_SHARE_OVERALLOCATION,
                        detail,
                        (event, str(share_sum)),
                    )
                )
        if len(amounts) > 1:
            for r in rows:
                blocks.append(
                    BlockedItem(
                        "cost",
                        r.expense,
                        r.contract,
                        BLOCK_SHARE_OVERALLOCATION,
                        f"事件 {event} 各合同登记金额不一致：{sorted(map(str, amounts))}",
                        (event,),
                    )
                )
            overallocated.add(event)

    lines: list[CostLine] = []
    exact_by_contract: dict[str, list[Decimal]] = {}
    for rec in costs:
        reasons: list[str] = []
        if rec.event not in eligible_events:
            reasons.append(BLOCK_INELIGIBLE_EVENT)
            blocks.append(
                BlockedItem(
                    "cost",
                    rec.expense,
                    rec.contract,
                    BLOCK_INELIGIBLE_EVENT,
                    f"事件 {rec.event} 无合资格签到，费用 {rec.expense} 不合资格",
                )
            )
        if rec.approval != "approved":
            reasons.append(BLOCK_PENDING_APPROVAL)
            blocks.append(
                BlockedItem(
                    "cost",
                    rec.expense,
                    rec.contract,
                    BLOCK_PENDING_APPROVAL,
                    f"费用 {rec.expense} 审批状态为 {rec.approval}，尚未获批",
                )
            )
        if rec.event in overallocated:
            reasons.append(BLOCK_SHARE_OVERALLOCATION)
        claimed = rec.amount * rec.eligible_share
        payable = not reasons
        line = CostLine(
            expense=rec.expense,
            event=rec.event,
            contract=rec.contract,
            amount=rec.amount,
            eligible_share=rec.eligible_share,
            claimed_amount=claimed,
            payable=payable,
            block_reasons=tuple(reasons),
        )
        lines.append(line)
        if payable:
            exact_by_contract.setdefault(rec.contract, []).append(claimed)

    payable_by_contract: dict[str, Decimal] = {}
    residual = ZERO
    for contract, values in exact_by_contract.items():
        rounded, drop = aggregate(values, places)
        payable_by_contract[contract] = rounded
        residual += drop
    # 没有可支付费用的合同显式给 0，避免下游推断
    for rec in costs:
        payable_by_contract.setdefault(rec.contract, money(0, places))
    return tuple(lines), blocks, payable_by_contract, residual
