from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
import hashlib
import json

from ..acquisition_plan import AcquisitionPlan


def json_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"unsupported JSON type: {type(value).__name__}")


def canonical_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, default=json_value, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Observation:
    SECURITY_CODE: str
    OBSERVATION_DATE: date
    OPEN: Decimal | None
    HIGH: Decimal | None
    LOW: Decimal | None
    CLOSE: Decimal | None
    ADJUSTED_CLOSE: Decimal | None
    VOLUME: Decimal | None
    SOURCE: str
    RETRIEVED_AT: datetime


class ResultStatus(str, Enum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    NO_DATA = "NO_DATA"
    NO_DATA_CHANGE = "NO_DATA_CHANGE"
    REJECTED = "REJECTED"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    DATA_QUALITY_ERROR = "DATA_QUALITY_ERROR"


@dataclass
class AcquisitionResult:
    plan: AcquisitionPlan
    status: ResultStatus
    provider: str = "Yahoo Chart API"
    ticker: str | None = None
    observations: list[Observation] = field(default_factory=list)
    response_status: int | None = None
    retry_count: int = 0
    retrieved_at: datetime | None = None
    response_metadata: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    failure_reason: str | None = None
    target_achieved: bool | None = None
    expected_overlap_count: int = 0
    returned_overlap_count: int = 0
    provider_returned_count: int | None = None
    actual_start_date: date | None = None
    actual_end_date: date | None = None
    admission_evidence: dict = field(default_factory=dict)

    def evidence(self) -> dict:
        dates = [row.OBSERVATION_DATE for row in self.observations]
        return {
            "security_code": self.plan.security_code,
            "acquisition_mode": self.plan.acquisition_mode,
            "plan": asdict(self.plan), "plan_hash": canonical_hash(asdict(self.plan)),
            "request_start_date": self.plan.request_start_date,
            "request_end_date": self.plan.request_end_date,
            "actual_start_date": self.actual_start_date,
            "actual_end_date": self.actual_end_date,
            "returned_count": self.provider_returned_count,
            "normalized_count": len(dates), "provider": self.provider, "ticker": self.ticker,
            "provider_status": self.status, "response_status": self.response_status,
            "retry_count": self.retry_count, "retrieved_at": self.retrieved_at,
            "response_metadata": self.response_metadata, "warnings": self.warnings,
            "failure_reason": self.failure_reason, "target_achieved": self.target_achieved,
            "expected_overlap_count": self.expected_overlap_count,
            "returned_overlap_count": self.returned_overlap_count,
            "admission_evidence": self.admission_evidence,
        }


def run_validation(results: list[AcquisitionResult]) -> str:
    if any(r.status in {ResultStatus.REJECTED, ResultStatus.PROVIDER_ERROR,
                        ResultStatus.DATA_QUALITY_ERROR} for r in results):
        return "FAIL"
    if any(r.status == ResultStatus.PARTIAL for r in results):
        return "PARTIAL"
    if any(r.status == ResultStatus.NO_DATA for r in results):
        return "NO_DATA"
    return "PASS"
