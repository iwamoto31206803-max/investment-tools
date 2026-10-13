from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
import json
from pathlib import Path
import re

from ..acquisition_plan import AcquisitionMode, AcquisitionPlanner, RunDecision
from ..config import GeneratorConfig
from ..package_reader import SecurityHistory
from .engine import WindowApproval
from .models import canonical_hash
from .provider import ticker_for


def _strict_object(value, allowed: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError(f"invalid or unknown fields in {label}")
    return value


def _no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate development contract JSON key")
        result[key] = value
    return result


@dataclass(frozen=True)
class DevelopmentContract:
    data: dict

    @property
    def sha256(self) -> str:
        return canonical_hash(self.data)

    @classmethod
    def load(cls, path: str | Path, input_sha256: str, analysis_date: date):
        raw = Path(path).read_bytes()
        if len(raw) > 1024 * 1024:
            raise ValueError("development contract exceeds 1 MiB")
        data = json.loads(raw, object_pairs_hook=_no_duplicate_keys)
        _strict_object(data, {"schema_version", "input_sha256", "analysis_date",
                             "decision_source", "securities", "run_decision"}, "contract")
        if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
            raise ValueError("development contract schema_version must be 1")
        if (not isinstance(data.get("input_sha256"), str) or
                not re.fullmatch(r"[0-9a-f]{64}", data["input_sha256"]) or
                data["input_sha256"] != input_sha256):
            raise ValueError("development contract input SHA-256 mismatch")
        if data.get("analysis_date") != analysis_date.isoformat():
            raise ValueError("development contract analysis date mismatch")
        if (not isinstance(data.get("decision_source"), str) or
                not data["decision_source"].strip() or len(data["decision_source"]) > 256):
            raise ValueError("development contract requires a bounded decision_source")
        securities = data.get("securities")
        if not isinstance(securities, list) or not 1 <= len(securities) <= 10000:
            raise ValueError("development contract requires 1..10000 explicitly selected securities")
        codes = set()
        for item in securities:
            _strict_object(item, {"security_code", "yahoo_ticker", "request_start_date",
                                  "full_refresh_reason"}, "security")
            code = item.get("security_code")
            ticker_for(code, {code: item["yahoo_ticker"]} if "yahoo_ticker" in item else {})
            if code in codes:
                raise ValueError("duplicate development security")
            codes.add(code)
            if "request_start_date" in item:
                start = date.fromisoformat(item["request_start_date"])
                if start > analysis_date:
                    raise ValueError("development start date follows analysis date")
            if "full_refresh_reason" in item and (
                    not isinstance(item["full_refresh_reason"], str) or
                    not item["full_refresh_reason"].strip() or len(item["full_refresh_reason"]) > 1024):
                raise ValueError("FULL_REFRESH requires a bounded explicit reason")
        if "run_decision" in data:
            _strict_object(data["run_decision"], set(RunDecision.__dataclass_fields__), "run_decision")
            if ("no_new_price_facts" not in data["run_decision"] or
                    any(type(value) is not bool for value in data["run_decision"].values())):
                raise ValueError("run decision fields must be explicit booleans")
        return cls(data)

    def prepare(self, histories: dict[str, SecurityHistory], config: GeneratorConfig):
        items = {item["security_code"]: item for item in self.data["securities"]}
        selected = {code: histories.get(code) for code in items}
        end = date.fromisoformat(self.data["analysis_date"])
        decision = RunDecision(**self.data["run_decision"]) if "run_decision" in self.data else None
        reasons = {code: item["full_refresh_reason"] for code, item in items.items()
                   if "full_refresh_reason" in item}
        plans = AcquisitionPlanner(config).plan_all(selected, end, run_decision=decision,
                                                    full_refresh_reasons=reasons)
        approvals, materialized = {}, []
        for plan in plans:
            item = items[plan.security_code]
            if plan.acquisition_mode in {AcquisitionMode.BACKFILL, AcquisitionMode.FULL_REFRESH}:
                if "request_start_date" not in item:
                    raise ValueError("BACKFILL/FULL_REFRESH requires an explicit approved start date")
                start = date.fromisoformat(item["request_start_date"])
                plan = replace(plan, request_start_date=start)
                approvals[plan.security_code] = WindowApproval(plan.security_code, start, end,
                                                              self.data["decision_source"])
            elif "request_start_date" in item:
                raise ValueError("OVERLAP/NO_DATA_CHANGE cannot override planner start via contract")
            materialized.append(plan)
        mappings = {code: item["yahoo_ticker"] for code, item in items.items() if "yahoo_ticker" in item}
        return materialized, selected, approvals, decision, mappings
