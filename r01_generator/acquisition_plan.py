from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum

from .config import GeneratorConfig
from .package_reader import SecurityHistory
from .validation import PlanValidationError


class AcquisitionMode(str, Enum):
    NO_DATA_CHANGE = "NO_DATA_CHANGE"
    OVERLAP = "OVERLAP"
    BACKFILL = "BACKFILL"
    FULL_REFRESH = "FULL_REFRESH"


class PlanStatus(str, Enum):
    READY = "READY"
    ACQUISITION_PLAN_BLOCKED = "ACQUISITION_PLAN_BLOCKED"


@dataclass(frozen=True)
class OverrideEvidence:
    reason: str
    decision_source: str
    original_window: tuple[date, date]
    override_window: tuple[date, date]
    timestamp: datetime

    def __post_init__(self):
        if not self.reason.strip() or not self.decision_source.strip():
            raise PlanValidationError("override reason and decision_source are required")


@dataclass(frozen=True)
class AcquisitionPlan:
    security_code: str
    acquisition_mode: AcquisitionMode
    latest_existing_date: date | None
    request_start_date: date | None
    request_end_date: date
    expected_overlap_observations: int
    expected_new_observations: int | None
    full_refresh_reason: str | None
    override_flag: bool
    plan_status: PlanStatus
    override_evidence: OverrideEvidence | None = None


class AcquisitionPlanner:
    def __init__(self, config: GeneratorConfig):
        self.config = config

    def plan(self, security_code: str, history: SecurityHistory | None, request_end: date,
             *, full_refresh_reason: str | None = None, full_refresh_requested: bool = False,
             no_data_change: bool = False, override: OverrideEvidence | None = None,
             forced_overlap_start: date | None = None,
             expected_overlap_observations: int | None = None) -> AcquisitionPlan:
        if full_refresh_requested:
            if not full_refresh_reason or not full_refresh_reason.strip():
                raise PlanValidationError("FULL_REFRESH requires an explicit reason")
            return AcquisitionPlan(security_code, AcquisitionMode.FULL_REFRESH,
                history.latest_observation_date if history else None, None, request_end, 0, None,
                full_refresh_reason, False, PlanStatus.READY)
        if no_data_change:
            return AcquisitionPlan(security_code, AcquisitionMode.NO_DATA_CHANGE,
                history.latest_observation_date if history else None, None, request_end, 0, 0,
                None, False, PlanStatus.READY)
        if history is None or history.observation_count < self.config.overlap_observation_target:
            return AcquisitionPlan(security_code, AcquisitionMode.BACKFILL,
                history.latest_observation_date if history else None, None, request_end, 0,
                self.config.backfill_target, None, False, PlanStatus.READY)

        overlap = expected_overlap_observations or self.config.overlap_observation_target
        start = forced_overlap_start or history.observation_dates[-overlap]
        blocked = overlap > self.config.overlap_max_without_override and override is None
        return AcquisitionPlan(security_code, AcquisitionMode.OVERLAP,
            history.latest_observation_date, start, request_end, overlap, None, None,
            override is not None,
            PlanStatus.ACQUISITION_PLAN_BLOCKED if blocked else PlanStatus.READY, override)

    def plan_all(self, histories: dict[str, SecurityHistory], request_end: date, **kwargs):
        return [self.plan(code, history, request_end, **kwargs)
                for code, history in sorted(histories.items())]
