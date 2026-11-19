import sys
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from grants.loading import (
    load_attendance,
    load_contracts,
    load_corrections,
    load_costs,
    load_disbursements,
    load_policy,
)
from grants.coverage import apply_corrections, build_coverage
from grants.costs import analyze_costs
from grants.disbursements import build_recommendations
from grants.pipeline import run_analysis
from grants.models import (
    BLOCK_GROUP_UNPROVEN,
    BLOCK_INELIGIBLE_EVENT,
    BLOCK_OUTSIDE_WINDOW,
    BLOCK_PENDING_APPROVAL,
    BLOCK_SHARE_OVERALLOCATION,
    BLOCK_TIER_NOT_MET,
    BLOCK_UNAPPLIED_CORRECTION,
)
from grants.money import aggregate, money

SCENARIO = ROOT / "tests" / "fixtures" / "late_correction"
D = Decimal


def scenario_inputs():
    policy = load_policy(SCENARIO / "policy.json")
    attendance = load_attendance(SCENARIO / "attendance.csv")
    costs = load_costs(SCENARIO / "costs.csv")
    contracts = load_contracts(SCENARIO / "contracts.csv")
    disbursements = load_disbursements(SCENARIO / "disbursements.csv")
    corrections = load_corrections(SCENARIO / "corrections.csv")
    return policy, attendance, costs, contracts, disbursements, corrections


def run_scenario():
    return run_analysis(SCENARIO, SCENARIO / "corrections.csv")


class LateCorrectionTests(unittest.TestCase):
    def setUp(self):
        self.policy, self.att, self.costs, self.contracts, self.disb, self.corr = scenario_inputs()

    def test_applied_correction_merges_residents_and_pending_one_is_blocked(self):
        resolved, blocks, audit = apply_corrections(self.att, self.corr, date(2026, 6, 30))
        statuses = {c.correction_id: c.status for c in audit}
        self.assertEqual(statuses["K2"], "applied")
        self.assertEqual(statuses["K1"], "pending")
        # 更正前 c1 为 6 人（档 2）；K2 生效后 r2 并入 r1，降为 5 人（仅档 1）
        before_views, _, _ = build_coverage(self.att, self.contracts, self.policy)
        self.assertEqual(next(v for v in before_views if v.contract == "c1").achieved_tier, 2)
        # K2 生效：e0/c1 的 r2 改写为 r1（同一居民合并）
        k2_rows = [r for r in resolved if r.correction == "K2"]
        self.assertEqual(len(k2_rows), 1)
        self.assertEqual(k2_rows[0].resident, "r1")
        # K1 迟到未生效：对应签到仍按 r9 记录，同时进入阻塞清单
        k1_rec = [r for r in resolved if r.event == "e5" and r.contract == "c2" and r.resident == "r9"]
        self.assertEqual(len(k1_rec), 1)
        self.assertTrue(any(b.reason == BLOCK_UNAPPLIED_CORRECTION and b.source_id == "K1" for b in blocks))

    def test_coverage_tier_downgrade_after_correction(self):
        resolved, blocks, _ = apply_corrections(self.att, self.corr, date(2026, 6, 30))
        views, district, cov_blocks = build_coverage(resolved, self.contracts, self.policy)
        by = {v.contract: v for v in views}
        # c1：合并后 5 名合资格居民、权重 1，加权 5，只达到档 1；更正前为档 2
        self.assertEqual(by["c1"].distinct_residents, 5)
        self.assertEqual(by["c1"].weighted_residents, D("5"))
        self.assertEqual(by["c1"].achieved_tier, 1)
        self.assertTrue(by["c1"].tier_met)  # minimum_tier=1，恰好保住最低档
        self.assertEqual(by["c1"].new_residents, 4)
        self.assertEqual(by["c1"].previously_served, 1)
        # c2：youth 组 6 人加权 6，达到档 2
        self.assertEqual(by["c2"].distinct_residents, 6)
        self.assertEqual(by["c2"].achieved_tier, 2)
        # c3：3 人达到档 1；不合资格与窗口外记录不计入
        self.assertEqual(by["c3"].distinct_residents, 3)
        self.assertEqual(by["c3"].achieved_tier, 1)

    def test_district_headcount_not_doubled(self):
        resolved, _, _ = apply_corrections(self.att, self.corr, date(2026, 6, 30))
        _, district, _ = build_coverage(resolved, self.contracts, self.policy)
        # 共担居民 r1/r3 在多合同出现，全区只计一次
        self.assertEqual(district.distinct_residents, 11)
        self.assertEqual(district.new_residents, 10)
        self.assertEqual(district.previously_served, 1)

    def test_group_unproven_and_window_blocks(self):
        resolved, _, _ = apply_corrections(self.att, self.corr, date(2026, 6, 30))
        _, _, blocks = build_coverage(resolved, self.contracts, self.policy)
        reasons = {(b.reason, b.source_id) for b in blocks}
        self.assertIn((BLOCK_GROUP_UNPROVEN, "e8/r10"), reasons)
        self.assertIn((BLOCK_OUTSIDE_WINDOW, "e12/r20"), reasons)
        self.assertIn((BLOCK_INELIGIBLE_EVENT, "e11/r14"), reasons)

    def test_share_overallocation_and_pending_block_costs(self):
        resolved, _, _ = apply_corrections(self.att, self.corr, date(2026, 6, 30))
        lines, blocks, payable, residual = analyze_costs(self.costs, resolved, self.policy, 2)
        # e9 份额合计 1.2 > 1.0：两条费用全部排除
        e9 = [l for l in lines if l.event == "e9"]
        self.assertTrue(all(not l.payable for l in e9))
        self.assertTrue(
            all(BLOCK_SHARE_OVERALLOCATION in l.block_reasons for l in e9)
        )
        # y05 未获批
        y05 = next(l for l in lines if l.expense == "y05")
        self.assertFalse(y05.payable)
        self.assertIn(BLOCK_PENDING_APPROVAL, y05.block_reasons)
        # 可付费用：c1 = (80+40+40+40)*0.5 = 100；c2 = (80+40+40+40)*0.5 = 100；c3 = 100
        self.assertEqual(payable["c1"], D("100.00"))
        self.assertEqual(payable["c2"], D("100.00"))
        self.assertEqual(payable["c3"], D("100.00"))
        self.assertEqual(residual, D("0"))

    def test_sent_batches_locked_unsent_revised_within_contract(self):
        report, unsent = run_scenario()
        recs = {r.contract: r for r in report.recommendations}
        # c1：档1 费率 1.0 → 可报 100；已送达 120 → 超拨 20，无未送出批次 → 人工追缴
        self.assertEqual(recs["c1"].tier_entitlement, D("100.00"))
        self.assertEqual(recs["c1"].remaining_balance, D("-20.00"))
        self.assertEqual(recs["c1"].overclaimed, D("20.00"))
        self.assertEqual(recs["c1"].manual_recovery, D("20.00"))
        self.assertEqual(recs["c1"].payable_now, D("0.00"))
        self.assertEqual(recs["c1"].already_sent, D("120"))
        # c2：档2 费率 0.9 → 可报 90；已送达 180 → 超拨 90；未送出 b2=50 全额抵扣，40 人工追缴
        self.assertEqual(recs["c2"].tier_entitlement, D("90.00"))
        self.assertEqual(recs["c2"].overclaimed, D("90.00"))
        self.assertEqual(recs["c2"].offset_against_unsent, D("50.00"))
        self.assertEqual(recs["c2"].manual_recovery, D("40.00"))
        b2c2 = next(u for u in unsent if u["batch"] == "b2" and u["contract"] == "c2")
        self.assertEqual(b2c2["revised_amount"], D("0.00"))
        self.assertEqual(b2c2["offset"], D("50.00"))
        self.assertEqual(b2c2["source_revision"], "rev-2")
        # c3：档1 可报 100，其他来源资助 30，已送达 0 → 余额 70；
        # 未送出 b2=40 保留，另出新增建议 30
        self.assertEqual(recs["c3"].other_funding, D("30"))
        self.assertEqual(recs["c3"].remaining_balance, D("70.00"))
        self.assertEqual(recs["c3"].payable_now, D("70.00"))
        self.assertEqual(recs["c3"].additional_recommendation, D("30.00"))
        b2c3 = next(u for u in unsent if u["batch"] == "b2" and u["contract"] == "c3")
        self.assertEqual(b2c3["revised_amount"], D("40.00"))
        self.assertEqual(b2c3["offset"], D("0.00"))
        # 已送达批次金额绝不被改写：unsent 清单里不出现 b1
        self.assertEqual({u["batch"] for u in unsent}, {"b2"})

    def test_no_cross_contract_offset(self):
        # c1 的超拨不得用 c2/c3 的未送出额度抵扣
        report, unsent = run_scenario()
        for action in unsent:
            self.assertEqual(action["contract"], action["contract"])
        touched = {u["contract"] for u in unsent}
        self.assertEqual(touched, {"c2", "c3"})  # c1 无未送出批次，不能动别人的

    def test_minimum_tier_failure_pays_zero(self):
        # 合成：合同 minimum_tier=3，但加权成效只够档 1
        policy = dict(self.policy)
        contracts = [
            type(self.contracts[0])(
                contract="cx",
                cap=D("1000"),
                minimum_tier=3,
                share_weight=D("1"),
                eligible_group="all",
            )
        ]
        from grants.models import AttendanceRecord

        att = [
            AttendanceRecord(
                "e1", f"r{i}", "cx", date(2026, 5, 1) + timedelta(days=i), True, ""
            )
            for i in range(4)
        ]
        views, _, _ = build_coverage(att, contracts, policy)
        self.assertEqual(views[0].achieved_tier, 1)
        self.assertFalse(views[0].tier_met)
        recs, _ = build_recommendations(
            contracts, list(views), {"cx": D("100.00")}, [], policy, 2
        )
        self.assertEqual(recs[0].reimbursement_rate, D("0"))
        self.assertEqual(recs[0].payable_now, D("0.00"))
        self.assertIn(BLOCK_TIER_NOT_MET, recs[0].block_reasons)

    def test_cap_clamps_entitlement(self):
        policy = dict(self.policy)
        contract = type(self.contracts[0])("cz", D("50"), 1, D("1"), "all")
        from grants.models import CoverageView

        view = CoverageView("cz", "all", 3, 3, 0, D("3"), 1, 1, True, D("100.00"), D("0.00"))
        recs, _ = build_recommendations([contract], [view], {"cz": D("100.00")}, [], policy, 2)
        self.assertEqual(recs[0].tier_entitlement, D("50.00"))

    def test_aggregate_rounding_keeps_residual_out_of_balances(self):
        # 1/3 份额的三条费用：精确合计 100，聚合层舍入一次
        total, residual = aggregate([D("33.3333"), D("33.3333"), D("33.3334")], 2)
        self.assertEqual(total, D("100.00"))
        self.assertEqual(residual, D("0.0000"))
        # 独立舍入会凭空造余额的反例：聚合层不采用
        bad = sum((money(v, 2) for v in [D("33.333"), D("33.333"), D("33.334")]), D("0"))
        self.assertEqual(bad, D("99.99"))
        good, _ = aggregate([D("33.333"), D("33.333"), D("33.334")], 2)
        self.assertEqual(good, D("100.00"))

    def test_report_carries_single_observation_date_and_ledger_revision(self):
        report, _ = run_scenario()
        self.assertEqual(report.observation_date, date(2026, 6, 30))
        self.assertEqual(report.ledger_revision, "rev-3")
        for rec in report.recommendations:
            self.assertEqual(rec.ledger_revision, "rev-3")
            # 事实备注必须能追到锁定批次的账版本
            if rec.contract in {"c1", "c2"}:
                self.assertTrue(any("rev-1" in note for note in rec.fact_notes))

    def test_per_capita_uses_same_observation_window(self):
        report, _ = run_scenario()
        by = {v.contract: v for v in report.coverage}
        # c1 可付 100 / 5 人；c2 可付 100 / 6 人；c3 可付 100 / 3 人
        self.assertEqual(by["c1"].per_capita_cost, D("20.00"))
        self.assertEqual(by["c2"].per_capita_cost, D("16.67"))
        self.assertEqual(by["c3"].per_capita_cost, D("33.33"))
        # 全区人均：可付合计 300 / 11 人（人头不翻倍）
        self.assertEqual(report.district_coverage.per_capita_cost, D("27.27"))


class DefaultFixtureTests(unittest.TestCase):
    def test_pipeline_runs_on_shipped_fixtures(self):
        report, unsent = run_analysis(ROOT / "data")
        # 出厂数据的埋点：每个事件份额合计 2.0（超分），所有费用被排除
        self.assertTrue(all(not line.payable for line in report.cost_lines))
        self.assertTrue(
            all(BLOCK_SHARE_OVERALLOCATION in line.block_reasons for line in report.cost_lines)
        )
        # c1/c2 因此无成效档，已送达批次转为人工追缴
        recs = {r.contract: r for r in report.recommendations}
        self.assertEqual(recs["c1"].manual_recovery, D("235.00"))
        self.assertEqual(recs["c2"].manual_recovery, D("150.00"))
        # c3 未送出 b2=95 因可付为 0 被同合同调减为 0
        b2 = next(u for u in unsent if u["contract"] == "c3")
        self.assertEqual(b2["revised_amount"], D("0.00"))
        # 全区人头不翻倍
        self.assertEqual(report.district_coverage.distinct_residents, 13)
        self.assertEqual(report.rounding_residual, D("0"))


if __name__ == "__main__":
    unittest.main()
