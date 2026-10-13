from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date

from ..acquisition_plan import AcquisitionMode, AcquisitionPlan, PlanStatus, RunDecision
from ..config import GeneratorConfig
from ..package_reader import SecurityHistory
from ..validation import PlanValidationError
from .models import AcquisitionResult, Observation, ResultStatus
from .normalization import DataQualityError, normalize
from .provider import AcquisitionProvider, ProviderError, request_timestamps


@dataclass(frozen=True)
class WindowApproval:
    """Development contract materializes Phase 1's unbounded history plans."""

    security_code: str
    start: date
    end: date
    decision_source: str


def validate_plan(plan: AcquisitionPlan, history: SecurityHistory | None,
                  config: GeneratorConfig, approval: WindowApproval | None = None,
                  run_decision: RunDecision | None = None) -> None:
    if plan.plan_status is not PlanStatus.READY:
        raise PlanValidationError("only READY plans can be acquired")
    if not isinstance(plan.acquisition_mode, AcquisitionMode):
        raise PlanValidationError("unsupported acquisition mode")
    if type(plan.request_end_date) is not date:
        raise PlanValidationError("request end date required")
    if not isinstance(plan.security_code, str) or not plan.security_code:
        raise PlanValidationError("security code required")
    if history is not None:
        if (history.security_code != plan.security_code or
                any(type(day) is not date for day in history.observation_dates) or
                tuple(sorted(set(history.observation_dates))) != history.observation_dates):
            raise PlanValidationError("inconsistent existing history")
    latest = history.latest_observation_date if history else None
    if plan.latest_existing_date != latest:
        raise PlanValidationError("plan latest date does not match history")
    if latest is not None and latest > plan.request_end_date:
        raise PlanValidationError("analysis date precedes existing history")
    if (type(plan.expected_overlap_observations) is not int or
            plan.expected_overlap_observations < 0 or
            (plan.expected_new_observations is not None and
             (type(plan.expected_new_observations) is not int or plan.expected_new_observations < 0))):
        raise PlanValidationError("invalid expected observation counts")
    if type(plan.override_flag) is not bool or plan.override_flag != (plan.override_evidence is not None):
        raise PlanValidationError("override flag and evidence disagree")
    if plan.acquisition_mode is AcquisitionMode.NO_DATA_CHANGE:
        if (not isinstance(run_decision, RunDecision) or
                any(type(value) is not bool for value in vars(run_decision).values()) or
                not run_decision.permits_no_data_change or history is None or
                history.observation_count < config.overlap_observation_target or
                plan.request_start_date is not None or plan.expected_overlap_observations != 0 or
                plan.expected_new_observations != 0 or plan.override_flag or plan.full_refresh_reason):
            raise PlanValidationError("NO_DATA_CHANGE requires consistent run evidence and history")
        return
    try:
        request_timestamps(plan.request_start_date, plan.request_end_date)
    except ProviderError as exc:
        raise PlanValidationError(str(exc)) from exc
    if plan.acquisition_mode is AcquisitionMode.OVERLAP:
        if history is None or history.observation_count < config.overlap_observation_target:
            raise PlanValidationError("OVERLAP requires sufficient existing history")
        expected = sum(plan.request_start_date <= day <= plan.request_end_date
                       for day in history.observation_dates)
        if not expected or plan.expected_overlap_observations != expected:
            raise PlanValidationError("OVERLAP expected count differs from existing history")
        if plan.expected_new_observations is not None or plan.full_refresh_reason:
            raise PlanValidationError("inconsistent OVERLAP evidence")
        if expected > config.overlap_max_without_override and not plan.override_flag:
            raise PlanValidationError("OVERLAP exceeds sanity gate without override")
        if plan.override_evidence is not None:
            evidence = plan.override_evidence
            if (not evidence.reason.strip() or not evidence.decision_source.strip() or
                    evidence.override_window != (plan.request_start_date, plan.request_end_date) or
                    evidence.original_window[0] > evidence.original_window[1] or
                    evidence.timestamp.tzinfo is None or evidence.timestamp.utcoffset() is None):
                raise PlanValidationError("inconsistent override evidence")
    else:
        if (approval is None or not approval.decision_source.strip() or
                (approval.security_code, approval.start, approval.end) !=
                (plan.security_code, plan.request_start_date, plan.request_end_date)):
            raise PlanValidationError("explicit development window approval required")
        if plan.override_flag or plan.expected_overlap_observations != 0:
            raise PlanValidationError("unexpected overlap evidence for history acquisition")
        if latest is not None and plan.request_start_date > latest:
            raise PlanValidationError("history acquisition start excludes existing history")
        if plan.acquisition_mode is AcquisitionMode.BACKFILL:
            if (plan.expected_new_observations != config.backfill_target or plan.full_refresh_reason or
                    (history is not None and history.observation_count >= config.overlap_observation_target)):
                raise PlanValidationError("inconsistent BACKFILL evidence")
        elif (not plan.full_refresh_reason or not plan.full_refresh_reason.strip() or
              plan.expected_new_observations is not None):
            raise PlanValidationError("FULL_REFRESH requires an explicit reason")


def validate_observations(rows: list[Observation], plan: AcquisitionPlan) -> bool:
    """Return whether required OHLC is missing; reject invalid data without sorting it away."""
    previous = None
    incomplete = False
    for row in rows:
        if row.SECURITY_CODE != plan.security_code:
            raise DataQualityError("security lineage mismatch")
        if not plan.request_start_date <= row.OBSERVATION_DATE <= plan.request_end_date:
            raise DataQualityError("observation outside approved request bounds")
        if previous is not None and row.OBSERVATION_DATE <= previous:
            raise DataQualityError("duplicate or unordered security/date key")
        previous = row.OBSERVATION_DATE
        prices = (row.OPEN, row.HIGH, row.LOW, row.CLOSE)
        for value in (*prices, row.ADJUSTED_CLOSE, row.VOLUME):
            if value is not None and (not value.is_finite() or value < 0):
                raise DataQualityError("invalid negative or non-finite OHLCV")
        if row.VOLUME is not None and row.VOLUME != row.VOLUME.to_integral_value():
            raise DataQualityError("volume must be a non-negative integer")
        incomplete |= any(value is None for value in prices)
        if row.HIGH is not None and any(value > row.HIGH for value in prices if value is not None):
            raise DataQualityError("OHLC exceeds HIGH")
        if row.LOW is not None and any(value < row.LOW for value in prices if value is not None):
            raise DataQualityError("OHLC below LOW")
    return incomplete


class AcquisitionEngine:
    """Sequential execution; preflight every plan before any provider call."""

    def __init__(self, provider: AcquisitionProvider, config: GeneratorConfig):
        self.provider, self.config = provider, config

    def run(self, plans: list[AcquisitionPlan], histories: dict[str, SecurityHistory | None], *,
            approvals: dict[str, WindowApproval] | None = None,
            run_decision: RunDecision | None = None) -> list[AcquisitionResult]:
        approvals = approvals or {}
        seen, rejected = set(), {}
        for index, plan in enumerate(plans):
            try:
                if plan.security_code in seen:
                    raise PlanValidationError("duplicate security plan")
                seen.add(plan.security_code)
                validate_plan(plan, histories.get(plan.security_code), self.config,
                              approvals.get(plan.security_code), run_decision)
            except (PlanValidationError, ValueError, TypeError, AttributeError):
                rejected[index] = "plan preflight rejected: invalid state, bounds, or required evidence"
        if rejected:
            return [AcquisitionResult(plan, ResultStatus.REJECTED,
                    failure_reason=rejected.get(index, "batch aborted by invalid plan; zero provider calls"))
                    for index, plan in enumerate(plans)]
        results = [self._acquire(plan, histories.get(plan.security_code)) for plan in plans]
        for result in results:
            approval = approvals.get(result.plan.security_code)
            if approval:
                result.admission_evidence["window_approval"] = asdict(approval)
            if run_decision:
                result.admission_evidence["run_decision"] = asdict(run_decision)
        return results

    def _acquire(self, plan: AcquisitionPlan, history: SecurityHistory | None) -> AcquisitionResult:
        result = AcquisitionResult(plan, ResultStatus.NO_DATA_CHANGE)
        if plan.acquisition_mode is AcquisitionMode.NO_DATA_CHANGE:
            result.provider_returned_count = 0
            return result
        try:
            payload = self.provider.fetch(plan)
            result.ticker = payload.ticker
            result.response_status = payload.response_status
            result.retry_count = payload.retry_count
            result.retrieved_at = payload.retrieved_at
            result.response_metadata = payload.metadata
            timestamps = payload.chart.get("timestamp")
            if isinstance(timestamps, list):
                result.provider_returned_count = len(timestamps)
            elif timestamps is None:
                result.provider_returned_count = 0
            rows, warnings = normalize(payload, plan.security_code)
            result.warnings.extend(warnings)
            dates = [row.OBSERVATION_DATE for row in rows]
            result.actual_start_date = min(dates) if dates else None
            result.actual_end_date = max(dates) if dates else None
            incomplete = validate_observations(rows, plan)
            result.observations = rows
            result.status = ResultStatus.SUCCESS if rows else ResultStatus.NO_DATA
            if incomplete:
                result.status = ResultStatus.PARTIAL
                result.warnings.append("required OHLC values missing")
            if plan.acquisition_mode in {AcquisitionMode.OVERLAP, AcquisitionMode.FULL_REFRESH} and history:
                expected = {day for day in history.observation_dates
                            if plan.request_start_date <= day <= plan.request_end_date}
                returned = {row.OBSERVATION_DATE for row in rows}
                result.expected_overlap_count = len(expected)
                result.returned_overlap_count = len(expected & returned)
                if expected - returned:
                    result.warnings.append("provider omitted existing history observations in request window")
                    if rows:
                        result.status = ResultStatus.PARTIAL
            elif plan.acquisition_mode is AcquisitionMode.BACKFILL:
                result.target_achieved = len(rows) >= self.config.backfill_target
                if not result.target_achieved:
                    result.warnings.append(f"BACKFILL target shortfall: {len(rows)}/{self.config.backfill_target}")
                    if rows:
                        result.status = ResultStatus.PARTIAL
            return result
        except ProviderError as exc:
            result.status = ResultStatus.PROVIDER_ERROR
            result.failure_reason = str(exc)
            if result.response_status is None:
                result.response_status = exc.response_status
                result.retry_count = exc.retry_count
                result.response_metadata = exc.metadata
                result.retrieved_at = exc.retrieved_at
        except DataQualityError as exc:
            result.status = ResultStatus.DATA_QUALITY_ERROR
            result.failure_reason = str(exc)
        return result
