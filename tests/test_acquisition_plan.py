from datetime import date, datetime, timedelta, timezone

import pytest

from r01_generator.acquisition_plan import (AcquisitionMode, AcquisitionPlanner,
    OverrideEvidence, PlanStatus, RunDecision)
from r01_generator.config import GeneratorConfig
from r01_generator.package_reader import SecurityHistory
from r01_generator.validation import PlanValidationError


def history(code="1001", count=40, start=date(2026, 7, 1)):
    # Weekdays are the observed sequence; no calendar-day arithmetic is used by planner.
    days, current = [], start
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return SecurityHistory(code, tuple(days))


def test_overlap_uses_twentieth_actual_observation():
    item = history()
    plan = AcquisitionPlanner(GeneratorConfig()).plan("1001", item, date(2026, 9, 18))
    assert plan.acquisition_mode is AcquisitionMode.OVERLAP
    assert plan.request_start_date == item.observation_dates[-20]
    assert plan.expected_overlap_observations == 20
    assert plan.plan_status is PlanStatus.READY


def test_new_and_short_security_backfill_but_sufficient_does_not():
    planner = AcquisitionPlanner(GeneratorConfig())
    assert planner.plan("new", None, date(2026, 9, 18)).acquisition_mode is AcquisitionMode.BACKFILL
    assert planner.plan("short", history(count=19), date(2026, 9, 18)).expected_new_observations == 150
    assert planner.plan("old", history(count=20), date(2026, 9, 18)).acquisition_mode is AcquisitionMode.OVERLAP


def test_full_refresh_requires_reason():
    planner = AcquisitionPlanner(GeneratorConfig())
    with pytest.raises(PlanValidationError, match="explicit reason"):
        planner.plan("1001", history(), date(2026, 9, 18), full_refresh_requested=True)
    plan = planner.plan("1001", history(), date(2026, 9, 18),
                        full_refresh_requested=True, full_refresh_reason="source correction")
    assert plan.acquisition_mode is AcquisitionMode.FULL_REFRESH


def test_no_data_change_is_run_level_and_rejects_contradictions():
    planner = AcquisitionPlanner(GeneratorConfig())
    histories = {"1001": history()}
    plans = planner.plan_all(histories, date(2026, 9, 18),
                             run_decision=RunDecision(no_new_price_facts=True))
    assert [plan.acquisition_mode for plan in plans] == [AcquisitionMode.NO_DATA_CHANGE]

    with pytest.raises(PlanValidationError, match="FULL_REFRESH"):
        planner.plan_all(histories, date(2026, 9, 18),
                         run_decision=RunDecision(no_new_price_facts=True),
                         full_refresh_reasons={"1001": "source correction"})
    with pytest.raises(PlanValidationError, match="insufficient-history"):
        planner.plan_all({"new": None}, date(2026, 9, 18),
                         run_decision=RunDecision(no_new_price_facts=True))
    with pytest.raises(PlanValidationError, match="does not permit"):
        planner.plan_all(histories, date(2026, 9, 18),
                         run_decision=RunDecision(no_new_price_facts=True,
                                                  universe_changed=True))


def test_excessive_overlap_blocked_and_valid_override_ready():
    planner = AcquisitionPlanner(GeneratorConfig())
    end, start = date(2026, 9, 18), date(2026, 5, 1)
    item = history(count=96, start=start)
    blocked = planner.plan("1001", item, end, forced_overlap_start=start,
                           expected_overlap_observations=96)
    assert blocked.plan_status is PlanStatus.ACQUISITION_PLAN_BLOCKED
    evidence = OverrideEvidence("approved investigation", "change-42", (start, end),
                                (start, end), datetime.now(timezone.utc))
    ready = planner.plan("1001", item, end, forced_overlap_start=start,
                         expected_overlap_observations=96, override=evidence)
    assert ready.plan_status is PlanStatus.READY and ready.override_flag


def test_old_forced_start_computes_overlap_and_cannot_be_underreported():
    planner = AcquisitionPlanner(GeneratorConfig())
    item = history(count=96, start=date(2026, 5, 1))
    end = item.latest_observation_date
    blocked = planner.plan("1001", item, end, forced_overlap_start=date(2026, 5, 1))
    assert blocked.expected_overlap_observations == 96
    assert blocked.plan_status is PlanStatus.ACQUISITION_PLAN_BLOCKED
    with pytest.raises(PlanValidationError, match="does not match"):
        planner.plan("1001", item, end, forced_overlap_start=date(2026, 5, 1),
                     expected_overlap_observations=20)


def test_override_requires_evidence_fields():
    with pytest.raises(PlanValidationError):
        OverrideEvidence("", "ticket", (date.today(), date.today()),
                         (date.today(), date.today()), datetime.now(timezone.utc))
