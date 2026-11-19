"""下一笔拨付测算与超拨处理。

可拨余额由四类事实共同决定：合资格费用、成效档（含合同最低档要求）、
合同额度、已经生效的其他来源资助；已经送达财务并确认到账的批次只做
扣减、绝不重写。

超拨差额只允许在同一合同尚未送出的建议内抵扣，余额送人工追缴；
不得跨合同、不经批准抵扣其他用途的款。未达最低成效档的合同本轮
可拨为 0，且不能领取下一档补贴。尚未送出的批次属于可修正建议：
按本轮重算的可拨额调减或追加，并保留其原账版本以便追溯。
"""
from __future__ import annotations

from decimal import Decimal

from .models import (
    BLOCK_CAP_EXCEEDED,
    BLOCK_OTHER_FUNDING,
    BLOCK_TIER_NOT_MET,
    Contract,
    CoverageView,
    Disbursement,
    DisbursementRecommendation,
)
from .money import ZERO, money


def _rate_for(tier: int | None, thresholds: list[dict]) -> Decimal:
    if tier is None:
        return ZERO
    for rule in thresholds:
        if rule["tier"] == tier:
            return Decimal(str(rule["reimbursement_rate"]))
    return ZERO


def build_recommendations(
    contracts: list[Contract],
    coverage: list[CoverageView],
    payable_by_contract: dict[str, Decimal],
    disbursements: list[Disbursement],
    policy: dict,
    places: int,
    correction_notes: dict[str, list[str]] | None = None,
) -> tuple[tuple[DisbursementRecommendation, ...], list[dict]]:
    coverage_by = {view.contract: view for view in coverage}
    thresholds = policy["tier_thresholds"]
    other_funding = {
        key: Decimal(str(value)) for key, value in policy.get("other_funding", {}).items()
    }
    correction_notes = correction_notes or {}
    recommendations: list[DisbursementRecommendation] = []
    unsent_actions: list[dict] = []

    for contract in contracts:
        view = coverage_by[contract.contract]
        rate = _rate_for(view.achieved_tier, thresholds)
        eligible_cost = Decimal(payable_by_contract.get(contract.contract, ZERO))
        reasons: list[str] = []
        notes: list[str] = list(correction_notes.get(contract.contract, ()))

        if not view.tier_met:
            reasons.append(BLOCK_TIER_NOT_MET)
            rate = ZERO
            notes.append(
                f"加权成效 {view.weighted_residents} 对应档 {view.achieved_tier}，"
                f"低于最低档 {contract.minimum_tier}，本轮不领取任何档位补贴"
            )

        entitlement = money(eligible_cost * rate, places)
        if entitlement > contract.cap:
            reasons.append(BLOCK_CAP_EXCEEDED)
            notes.append(f"档位可报 {entitlement} 超合同额度 {contract.cap}，按额度封顶")
            entitlement = contract.cap

        funding = other_funding.get(contract.contract, ZERO)
        if funding:
            reasons.append(BLOCK_OTHER_FUNDING)
            notes.append(f"已生效其他来源资助 {funding} 先行扣减")

        sent_rows = [d for d in disbursements if d.contract == contract.contract and d.sent]
        unsent_rows = [
            d for d in disbursements if d.contract == contract.contract and not d.sent
        ]
        already_sent = sum((d.amount for d in sent_rows), ZERO)
        for row in sent_rows:
            notes.append(
                f"批次 {row.batch} 已送达确认 {row.amount}（账版本 {row.source_revision}），锁定不重写"
            )
        for row in unsent_rows:
            notes.append(
                f"批次 {row.batch} 尚未送出 {row.amount}（账版本 {row.source_revision}），可按本轮事实修正"
            )
        if eligible_cost == ZERO:
            notes.append("本轮合资格可付费用为 0，费用阻塞明细见 blocked_items（份额超分/未获批/不合资格）")

        # 余额只与已送达批次对冲；未送出批次不是既成拨款，不参与余额计算
        balance = entitlement - funding - already_sent
        total_overclaimed = -balance if balance < ZERO else ZERO
        payable_now = money(max(ZERO, balance), places)

        # 在同合同未送出批次内顺序处理：超拨先抵扣，否则按本轮可拨额调减
        unsent_total = sum((d.amount for d in unsent_rows), ZERO)
        offset = ZERO
        additional = ZERO
        if total_overclaimed > ZERO:
            outstanding_claim = total_overclaimed
            for row in unsent_rows:
                cut = min(row.amount, outstanding_claim)
                outstanding_claim -= cut
                offset += cut
                unsent_actions.append(
                    {
                        "batch": row.batch,
                        "contract": row.contract,
                        "original_amount": money(row.amount, places),
                        "revised_amount": money(row.amount - cut, places),
                        "offset": money(cut, places),
                        "source_revision": row.source_revision,
                        "ledger_revision_at_approval": row.source_revision,
                        "reason": "同合同超拨抵扣（建议尚未送出，可修正）"
                        if cut
                        else "保留原建议",
                    }
                )
            manual_recovery = total_overclaimed - offset
        else:
            # 可拨额在未送出批次内按顺序分配，超出未送出额度的部分才新增建议
            allocation_pool = payable_now
            revised_unsent_total = ZERO
            for row in unsent_rows:
                revised = min(row.amount, allocation_pool)
                allocation_pool -= revised
                revised_unsent_total += revised
                cut = row.amount - revised
                unsent_actions.append(
                    {
                        "batch": row.batch,
                        "contract": row.contract,
                        "original_amount": money(row.amount, places),
                        "revised_amount": money(revised, places),
                        "offset": money(cut, places),
                        "source_revision": row.source_revision,
                        "ledger_revision_at_approval": row.source_revision,
                        "reason": "按本轮重算可拨额调减未送出建议"
                        if cut
                        else "保留原建议",
                    }
                )
            manual_recovery = ZERO
            additional = money(payable_now - revised_unsent_total, places)

        recommendations.append(
            DisbursementRecommendation(
                contract=contract.contract,
                cap=contract.cap,
                eligible_payable_cost=eligible_cost,
                reimbursement_rate=rate,
                tier_entitlement=entitlement,
                other_funding=funding,
                already_sent=already_sent,
                unsent_batches=tuple(
                    (d.batch, str(money(d.amount, places)), d.source_revision) for d in unsent_rows
                ),
                remaining_balance=money(balance, places),
                overclaimed=money(total_overclaimed, places),
                offset_against_unsent=money(offset, places),
                manual_recovery=money(manual_recovery, places),
                payable_now=payable_now,
                minimum_tier_met=view.tier_met,
                ledger_revision=policy["ledger_revision"],
                block_reasons=tuple(reasons),
                fact_notes=tuple(notes),
                additional_recommendation=additional,
            )
        )
    return tuple(recommendations), unsent_actions
