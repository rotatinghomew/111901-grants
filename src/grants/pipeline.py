"""端到端复核管线与报告序列化。

用法::

    python -m grants --data data --out report.json

所有视图使用策略文件中的同一个观察日，保证项目口径与跨合同口径可比。
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, is_dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from .costs import analyze_costs
from .coverage import apply_corrections, build_coverage
from .disbursements import build_recommendations
from .loading import (
    load_attendance,
    load_contracts,
    load_corrections,
    load_costs,
    load_disbursements,
    load_policy,
)
from .models import AnalysisReport
from .money import ZERO


def run_analysis(data_dir: str | Path, corrections_path: str | Path | None = None) -> AnalysisReport:
    data_dir = Path(data_dir)
    policy = load_policy(data_dir / "policy.json")
    observation_date = date.fromisoformat(policy["observation_date"])
    places = int(policy["currency"]["decimal_places"])

    attendance_raw = load_attendance(data_dir / "attendance.csv")
    costs = load_costs(data_dir / "costs.csv")
    contracts = load_contracts(data_dir / "contracts.csv")
    disbursements = load_disbursements(data_dir / "disbursements.csv")
    corrections = (
        load_corrections(corrections_path)
        if corrections_path is not None
        else load_corrections(data_dir / "corrections.csv")
    )

    attendance, correction_blocks, correction_audit = apply_corrections(
        attendance_raw, corrections, observation_date
    )
    correction_notes: dict[str, list[str]] = {}
    for corr in correction_audit:
        correction_notes.setdefault(corr.contract, []).append(
            f"更正 {corr.correction_id}（{corr.status}，{corr.received_date}）："
            f"{corr.event} 居民 {corr.from_resident}→{corr.to_resident}"
        )

    cost_lines, cost_blocks, payable_by_contract, rounding_residual = analyze_costs(
        costs, attendance, policy, places
    )
    coverage_views, district_view, coverage_blocks = build_coverage(
        attendance,
        contracts,
        policy,
        extra_blocks=correction_blocks,
        payable_by_contract=payable_by_contract,
    )
    recommendations, unsent_actions = build_recommendations(
        contracts,
        list(coverage_views),
        payable_by_contract,
        disbursements,
        policy,
        places,
        correction_notes=correction_notes,
    )

    return AnalysisReport(
        observation_date=observation_date,
        ledger_revision=policy["ledger_revision"],
        decimal_places=places,
        contracts=tuple(c.contract for c in contracts),
        coverage=coverage_views,
        district_coverage=district_view,
        cost_lines=cost_lines,
        blocked_items=tuple(cost_blocks + coverage_blocks),
        recommendations=recommendations,
        corrections_status=tuple(
            {
                "correction_id": c.correction_id,
                "contract": c.contract,
                "status": c.status,
                "received_date": c.received_date.isoformat(),
                "applied": c.status == "applied" and c.received_date <= observation_date,
            }
            for c in correction_audit
        ),
        rounding_residual=rounding_residual,
    ), unsent_actions


def to_jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if is_dataclass(value):
        return {k: to_jsonable(v) for k, v in asdict(value).items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: to_jsonable(v) for k, v in value.items()}
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="补贴成效对账与拨付复核")
    parser.add_argument("--data", default="data", help="资料目录")
    parser.add_argument("--corrections", default=None, help="可选更正单 CSV 路径")
    parser.add_argument("--out", default=None, help="报告 JSON 输出路径")
    args = parser.parse_args(argv)

    report, unsent_actions = run_analysis(args.data, args.corrections)
    payload = {
        "report": to_jsonable(report),
        "unsent_batch_revisions": to_jsonable(unsent_actions),
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
