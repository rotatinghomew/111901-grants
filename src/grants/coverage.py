"""覆盖与成效分析。

口径要点：

* 覆盖人数以合同约定的居民组和回看周期为准；已有服务与新增覆盖分开。
* 人头在共担项目（多合同签到同一居民）和全区口径都只计一次，绝不翻倍；
  跨合同达标判断使用合同的获批权重计入加权成效。
* 不合资格课程、居民归属尚未证实、尚未生效更正所涉记录逐条说明卡点，
  不进入成效与可支付金额。
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from .models import (
    BLOCK_GROUP_UNPROVEN,
    BLOCK_INELIGIBLE_EVENT,
    BLOCK_OUTSIDE_WINDOW,
    BLOCK_UNAPPLIED_CORRECTION,
    AttendanceRecord,
    BlockedItem,
    Contract,
    Correction,
    CoverageView,
)
from .money import ZERO

DISTRICT = "ALL"


def _window(observation_date, lookback_days: int):
    return observation_date - timedelta(days=lookback_days), observation_date


def apply_corrections(
    attendance: list[AttendanceRecord],
    corrections: list[Correction],
    observation_date,
) -> tuple[list[AttendanceRecord], list[BlockedItem], list[Correction]]:
    """按观察日处理更正单。

    已生效（applied 且送达日不晚于观察日）的更正改写对应签到的居民；
    迟到/未生效的更正不改写事实，只把对应记录标记为归属待证实，
    并在阻塞清单里解释。
    """
    blocked: list[BlockedItem] = []
    pending: list[Correction] = []
    applied: list[Correction] = []
    by_key: dict[tuple, Correction] = {}
    for corr in corrections:
        if corr.status == "applied" and corr.received_date <= observation_date:
            by_key[(corr.event, corr.contract, corr.from_resident)] = corr
            applied.append(corr)
        else:
            pending.append(corr)
            blocked.append(
                BlockedItem(
                    source="attendance",
                    source_id=corr.correction_id,
                    contract=corr.contract,
                    reason=BLOCK_UNAPPLIED_CORRECTION,
                    detail=(
                        f"更正 {corr.correction_id} 状态={corr.status}，"
                        f"送达 {corr.received_date} 晚于/未达观察日 {observation_date}，"
                        f"居民 {corr.from_resident}→{corr.to_resident} 暂不生效"
                    ),
                    blocking_facts=(corr.event, corr.from_resident, corr.to_resident),
                )
            )

    resolved: list[AttendanceRecord] = []
    for rec in attendance:
        corr = by_key.get((rec.event, rec.contract, rec.resident))
        if corr is not None:
            resolved.append(
                AttendanceRecord(
                    event=rec.event,
                    resident=corr.to_resident,
                    contract=rec.contract,
                    date=rec.date,
                    eligible=rec.eligible,
                    correction=corr.correction_id,
                )
            )
        else:
            resolved.append(rec)
    return resolved, blocked, applied + pending


def _in_group(contract: Contract, resident: str, groups: dict) -> bool:
    if contract.eligible_group == "all":
        return True
    return resident in groups.get(contract.eligible_group, ())


def _tier_for(weighted: Decimal, thresholds: list[dict]) -> int | None:
    achieved = None
    for rule in sorted(thresholds, key=lambda r: r["tier"]):
        if weighted >= Decimal(str(rule["min_weighted_residents"])):
            achieved = rule["tier"]
    return achieved


def build_coverage(
    attendance: list[AttendanceRecord],
    contracts: list[Contract],
    policy: dict,
    extra_blocks: list[BlockedItem] | None = None,
    payable_by_contract: dict | None = None,
) -> tuple[tuple[CoverageView, ...], CoverageView, list[BlockedItem]]:
    observation_date = policy["observation_date"]
    if isinstance(observation_date, str):
        from datetime import date as _date

        observation_date = _date.fromisoformat(observation_date)
    earliest, latest = _window(observation_date, int(policy["lookback_days"]))
    groups = policy.get("resident_groups", {})
    prior = set(policy.get("previously_served_residents", ()))
    thresholds = policy["tier_thresholds"]
    places = int(policy.get("currency", {}).get("decimal_places", 2))
    quant = Decimal(1).scaleb(-places)
    blocks = list(extra_blocks or [])
    payable_by_contract = payable_by_contract or {}

    views: list[CoverageView] = []
    district_residents: set[str] = set()
    district_new: set[str] = set()

    for contract in contracts:
        residents: set[str] = set()
        weighted = ZERO
        for rec in attendance:
            if rec.contract != contract.contract:
                continue
            if not (earliest <= rec.date <= latest):
                blocks.append(
                    BlockedItem(
                        "attendance",
                        f"{rec.event}/{rec.resident}",
                        contract.contract,
                        BLOCK_OUTSIDE_WINDOW,
                        f"签到日 {rec.date} 不在回看窗口 {earliest}~{latest}",
                    )
                )
                continue
            if not rec.eligible:
                blocks.append(
                    BlockedItem(
                        "attendance",
                        f"{rec.event}/{rec.resident}",
                        contract.contract,
                        BLOCK_INELIGIBLE_EVENT,
                        f"事件 {rec.event} 对居民 {rec.resident} 的签到不合资格",
                    )
                )
                continue
            if not _in_group(contract, rec.resident, groups):
                blocks.append(
                    BlockedItem(
                        "attendance",
                        f"{rec.event}/{rec.resident}",
                        contract.contract,
                        BLOCK_GROUP_UNPROVEN,
                        (
                            f"居民 {rec.resident} 归属组 {contract.eligible_group} "
                            "尚未证实，不计入该合同成效"
                        ),
                    )
                )
                continue
            # 人头与加权成效都按居民去重：同一居民在该合同内多次/多事件
            # 签到只计一次，成效不得随签到次数翻倍
            if rec.resident in residents:
                continue
            residents.add(rec.resident)
            weighted += contract.share_weight

        achieved = _tier_for(weighted, thresholds)
        tier_met = achieved is not None and achieved >= contract.minimum_tier
        new_residents = residents - prior
        payable = Decimal(payable_by_contract.get(contract.contract, ZERO))
        per_capita = (payable / len(residents)).quantize(quant) if residents else ZERO
        views.append(
            CoverageView(
                contract=contract.contract,
                eligible_group=contract.eligible_group,
                distinct_residents=len(residents),
                new_residents=len(new_residents),
                previously_served=len(residents & prior),
                weighted_residents=weighted,
                achieved_tier=achieved,
                minimum_tier=contract.minimum_tier,
                tier_met=tier_met,
                payable_cost=payable,
                per_capita_cost=per_capita,
            )
        )
        district_residents |= residents
        district_new |= new_residents

    district_payable = sum(payable_by_contract.values(), ZERO)
    district_view = CoverageView(
        contract=DISTRICT,
        eligible_group="all",
        distinct_residents=len(district_residents),
        new_residents=len(district_new),
        previously_served=len(district_residents & prior),
        weighted_residents=Decimal(len(district_residents)),
        achieved_tier=None,
        minimum_tier=None,
        tier_met=True,
        payable_cost=district_payable,
        per_capita_cost=(
            (district_payable / len(district_residents)).quantize(quant)
            if district_residents
            else ZERO
        ),
    )
    return tuple(views), district_view, blocks
