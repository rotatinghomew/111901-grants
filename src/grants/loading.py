"""资料加载。

CSV 全部以 UTF-8 读取；空字段保留为 ``""``，按 README 约定不得据此推断事实。
更正单为可选资料（corrections.csv），没有该文件时视为本月无更正，
而不是把 attendance 里的 correction 列当作空事实处理。
"""
from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

from .models import AttendanceRecord, Contract, Correction, CostRecord, Disbursement
from .money import money_input


def _parse_date(text: str) -> date:
    return date.fromisoformat(text.strip())


def load_policy(path: str | Path) -> dict:
    policy = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(policy, dict):
        raise ValueError("策略资料必须是 JSON 对象")
    policy.setdefault("observation_date", date.today().isoformat())
    policy.setdefault("lookback_days", 30)
    policy.setdefault("currency", {})
    policy["currency"].setdefault("decimal_places", 2)
    policy["currency"].setdefault("rounding", "ROUND_HALF_UP")
    policy["currency"].setdefault("quantize_level", "aggregate")
    policy.setdefault("share_sum_limit", 1.0)
    policy.setdefault("resident_groups", {})
    policy.setdefault("previously_served_residents", [])
    policy.setdefault("other_funding", {})
    policy.setdefault("ledger_revision", "unversioned")
    if not policy.get("tier_thresholds"):
        raise ValueError("策略资料缺少 tier_thresholds")
    return policy


def load_attendance(path: str | Path) -> list[AttendanceRecord]:
    rows = []
    with Path(path).open(encoding="utf-8", newline="") as stream:
        for raw in csv.DictReader(stream):
            rows.append(
                AttendanceRecord(
                    event=raw["event"],
                    resident=raw["resident"],
                    contract=raw["contract"],
                    date=_parse_date(raw["date"]),
                    eligible=raw["eligible"].strip() == "1",
                    correction=(raw.get("correction") or "").strip(),
                )
            )
    return rows


def load_costs(path: str | Path) -> list[CostRecord]:
    rows = []
    with Path(path).open(encoding="utf-8", newline="") as stream:
        for raw in csv.DictReader(stream):
            rows.append(
                CostRecord(
                    expense=raw["expense"],
                    event=raw["event"],
                    contract=raw["contract"],
                    amount=money_input(raw["amount"]),
                    eligible_share=money_input(raw["eligible_share"]),
                    approval=raw["approval"].strip(),
                )
            )
    return rows


def load_contracts(path: str | Path) -> list[Contract]:
    rows = []
    with Path(path).open(encoding="utf-8", newline="") as stream:
        for raw in csv.DictReader(stream):
            rows.append(
                Contract(
                    contract=raw["contract"],
                    cap=money_input(raw["cap"]),
                    minimum_tier=int(raw["minimum_tier"]),
                    share_weight=money_input(raw["share_weight"]),
                    eligible_group=raw["eligible_group"].strip(),
                )
            )
    return rows


def load_disbursements(path: str | Path) -> list[Disbursement]:
    rows = []
    with Path(path).open(encoding="utf-8", newline="") as stream:
        for raw in csv.DictReader(stream):
            rows.append(
                Disbursement(
                    batch=raw["batch"],
                    contract=raw["contract"],
                    amount=money_input(raw["amount"]),
                    sent=raw["sent"].strip() == "1",
                    source_revision=raw["source_revision"].strip(),
                )
            )
    return rows


def load_corrections(path: str | Path) -> list[Correction]:
    path = Path(path)
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8", newline="") as stream:
        for raw in csv.DictReader(stream):
            rows.append(
                Correction(
                    correction_id=raw["correction_id"],
                    event=raw["event"],
                    contract=raw["contract"],
                    from_resident=raw["from_resident"],
                    to_resident=raw["to_resident"],
                    status=raw["status"].strip(),
                    received_date=_parse_date(raw["received_date"]),
                )
            )
    return rows
