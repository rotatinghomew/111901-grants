"""领域记录与结果视图。

所有金额字段在内部一律使用 :class:`decimal.Decimal`，
按策略文件约定的精度在聚合层统一舍入，避免按合同独立舍入产生余额。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

# 阻塞原因代码（解释记录卡在哪里，先排除在可支付金额之外）
BLOCK_INELIGIBLE_EVENT = "ineligible_event"          # 课程/签到不合资格
BLOCK_GROUP_UNPROVEN = "resident_group_unproven"      # 居民归属尚未证实
BLOCK_PENDING_APPROVAL = "pending_approval"           # 费用未获批
BLOCK_SHARE_OVERALLOCATION = "share_overallocation"   # 同事件获批份额超分
BLOCK_TIER_NOT_MET = "minimum_tier_not_met"           # 合同未达最低成效档
BLOCK_UNAPPLIED_CORRECTION = "unapplied_correction"   # 迟到更正尚未生效
BLOCK_CAP_EXCEEDED = "cap_exceeded"                   # 超出合同额度
BLOCK_OTHER_FUNDING = "other_funding"                 # 已被其他来源资助覆盖
BLOCK_OUTSIDE_WINDOW = "outside_lookback_window"      # 不在回看周期内


@dataclass(frozen=True)
class AttendanceRecord:
    event: str
    resident: str
    contract: str
    date: date
    eligible: bool
    correction: str = ""


@dataclass(frozen=True)
class CostRecord:
    expense: str
    event: str
    contract: str
    amount: Decimal
    eligible_share: Decimal
    approval: str


@dataclass(frozen=True)
class Contract:
    contract: str
    cap: Decimal
    minimum_tier: int
    share_weight: Decimal
    eligible_group: str


@dataclass(frozen=True)
class Disbursement:
    batch: str
    contract: str
    amount: Decimal
    sent: bool
    source_revision: str


@dataclass(frozen=True)
class Correction:
    correction_id: str
    event: str
    contract: str
    from_resident: str
    to_resident: str
    status: str
    received_date: date


@dataclass(frozen=True)
class BlockedItem:
    source: str          # attendance / cost / disbursement
    source_id: str
    contract: str
    reason: str
    detail: str
    blocking_facts: tuple = ()


@dataclass(frozen=True)
class CostLine:
    expense: str
    event: str
    contract: str
    amount: Decimal
    eligible_share: Decimal
    claimed_amount: Decimal          # amount * eligible_share（未舍入）
    payable: bool
    block_reasons: tuple = ()


@dataclass(frozen=True)
class CoverageView:
    contract: str                     # "ALL" 表示全区口径
    eligible_group: str
    distinct_residents: int
    new_residents: int
    previously_served: int
    weighted_residents: Decimal       # 跨合同按获批权重计入，不翻倍人头
    achieved_tier: Optional[int]
    minimum_tier: Optional[int]
    tier_met: bool
    payable_cost: Decimal
    per_capita_cost: Decimal
    blocks: tuple = ()


@dataclass(frozen=True)
class DisbursementRecommendation:
    contract: str
    cap: Decimal
    eligible_payable_cost: Decimal
    reimbursement_rate: Decimal
    tier_entitlement: Decimal
    other_funding: Decimal
    already_sent: Decimal
    unsent_batches: tuple
    remaining_balance: Decimal        # 可拨余额（为负表示超拨）
    overclaimed: Decimal              # 超拨金额（不小于 0）
    offset_against_unsent: Decimal    # 可在同合同未送出批次内抵扣的部分
    manual_recovery: Decimal          # 需送人工追缴的差额
    payable_now: Decimal
    additional_recommendation: Decimal  # 未送出批次额度不足时的新增建议额
    minimum_tier_met: bool
    ledger_revision: str
    block_reasons: tuple = ()
    fact_notes: tuple = ()


@dataclass(frozen=True)
class AnalysisReport:
    observation_date: date
    ledger_revision: str
    decimal_places: int
    contracts: tuple
    coverage: tuple
    district_coverage: CoverageView
    cost_lines: tuple
    blocked_items: tuple
    recommendations: tuple
    corrections_status: tuple = field(default_factory=tuple)
    rounding_residual: Decimal = Decimal("0")
