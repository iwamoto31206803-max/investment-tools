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


@dataclass(frozen=True)
class RunDecision:
    """Explicit run-level evidence used to select the NO_DATA_CHANGE fast path."""

    no_new_price_facts: bool
    universe_changed: bool = False
    corporate_action_refresh_required: bool = False
    other_fact_change: bool = False

    @property
    def permits_no_data_change(self) -> bool:
        return self.no_new_price_facts and not (
            self.universe_changed
            or self.corporate_action_refresh_required
            or self.other_fact_change
        )


class AcquisitionPlanner:
    def __init__(self, config: GeneratorConfig):
        self.config = config

    def plan(self, security_code: str, history: SecurityHistory | None, request_end: date,
             *, full_refresh_reason: str | None = None, full_refresh_requested: bool = False,
             override: OverrideEvidence | None = None, forced_overlap_start: date | None = None,
             expected_overlap_observations: int | None = None) -> AcquisitionPlan:
        if full_refresh_requested:
            if not full_refresh_reason or not full_refresh_reason.strip():
                raise PlanValidationError("FULL_REFRESH requires an explicit reason")
            return AcquisitionPlan(security_code, AcquisitionMode.FULL_REFRESH,
                history.latest_observation_date if history else None, None, request_end, 0, None,
                full_refresh_reason, False, PlanStatus.READY)
        if history is None or history.observation_count < self.config.overlap_observation_target:
            return AcquisitionPlan(security_code, AcquisitionMode.BACKFILL,
                history.latest_observation_date if history else None, None, request_end, 0,
                self.config.backfill_target, None, False, PlanStatus.READY)

        start = forced_overlap_start or history.observation_dates[-self.config.overlap_observation_target]
        if start > request_end:
            raise PlanValidationError("OVERLAP start must not follow request end")
        computed_overlap = sum(start <= observed <= request_end
                               for observed in history.observation_dates)
        if computed_overlap == 0:
            raise PlanValidationError("OVERLAP start must include existing observations")
        if (expected_overlap_observations is not None
                and expected_overlap_observations != computed_overlap):
            raise PlanValidationError(
                "caller-supplied overlap count does not match history-derived count: "
                f"{expected_overlap_observations} != {computed_overlap}"
            )
        blocked = (computed_overlap > self.config.overlap_max_without_override
                   and override is None)
        return AcquisitionPlan(security_code, AcquisitionMode.OVERLAP,
            history.latest_observation_date, start, request_end, computed_overlap, None, None,
            override is not None,
            PlanStatus.ACQUISITION_PLAN_BLOCKED if blocked else PlanStatus.READY, override)

    def plan_all(self, histories: dict[str, SecurityHistory | None], request_end: date, *,
                 run_decision: RunDecision | None = None,
                 full_refresh_reasons: dict[str, str] | None = None, **kwargs):
        full_refresh_reasons = full_refresh_reasons or {}
        if run_decision is not None:
            if not run_decision.permits_no_data_change:
                raise PlanValidationError("run evidence does not permit NO_DATA_CHANGE")
            if full_refresh_reasons:
                raise PlanValidationError("NO_DATA_CHANGE conflicts with FULL_REFRESH")
            insufficient = [code for code, item in histories.items()
                            if item is None or
                            item.observation_count < self.config.overlap_observation_target]
            if insufficient:
                raise PlanValidationError(
                    "NO_DATA_CHANGE conflicts with new/insufficient-history securities: "
                    f"{insufficient[:5]}"
                )
            return [AcquisitionPlan(code, AcquisitionMode.NO_DATA_CHANGE,
                    item.latest_observation_date, None, request_end, 0, 0, None, False,
                    PlanStatus.READY) for code, item in sorted(histories.items())]
        return [self.plan(code, history, request_end, **kwargs)
                if code not in full_refresh_reasons else
                self.plan(code, history, request_end, full_refresh_requested=True,
                          full_refresh_reason=full_refresh_reasons[code], **kwargs)
                for code, history in sorted(histories.items())]
