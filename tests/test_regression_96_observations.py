from datetime import date, timedelta

from r01_generator.acquisition_plan import AcquisitionPlanner, PlanStatus
from r01_generator.config import GeneratorConfig
from r01_generator.package_reader import SecurityHistory


def test_665_security_specific_windows_and_bad_96_observation_window_is_blocked():
    days = tuple(date(2026, 1, 1) + timedelta(days=i) for i in range(180))
    histories = {f"{i:04d}": SecurityHistory(f"{i:04d}", days[i % 7: i % 7 + 100])
                 for i in range(665)}
    planner = AcquisitionPlanner(GeneratorConfig())
    plans = planner.plan_all(histories, date(2026, 9, 18))
    assert len(plans) == 665
    assert all(plan.expected_overlap_observations == 20 for plan in plans)
    assert all(plan.request_start_date != date(2026, 5, 1) for plan in plans)
    assert len({plan.request_start_date for plan in plans}) > 1

    bad = planner.plan("0000", histories["0000"], date(2026, 9, 18),
                       forced_overlap_start=date(2026, 5, 1), expected_overlap_observations=96)
    assert bad.plan_status is PlanStatus.ACQUISITION_PLAN_BLOCKED
