from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
import subprocess

from .acquisition_plan import AcquisitionPlanner, PlanStatus
from .config import GeneratorConfig
from .context import RunContext
from .package_reader import R01PackageReader, file_sha256


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="R01 Phase 1 development dry-run planner")
    result.add_argument("--input", required=True)
    result.add_argument("--config", required=True)
    result.add_argument("--dry-run", action="store_true", required=True)
    result.add_argument("--output-run-log", required=True)
    result.add_argument("--analysis-date", type=date.fromisoformat, required=True)
    result.add_argument("--run-id")
    result.add_argument("--snapshot-id")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    config = GeneratorConfig.load(args.config)
    before = file_sha256(args.input)
    context = RunContext(args.input, before, config.sha256, args.analysis_date,
                         **({"run_id": args.run_id} if args.run_id else {}),
                         snapshot_id=args.snapshot_id)
    try:
        context.source_commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False
        ).stdout.strip() or None
        with R01PackageReader(args.input) as reader:
            histories = reader.read_stock_daily_index()
        plans = AcquisitionPlanner(config).plan_all(histories, args.analysis_date)
        context.mode_counts = dict(Counter(p.acquisition_mode.value for p in plans))
        context.blocked_count = sum(p.plan_status is PlanStatus.ACQUISITION_PLAN_BLOCKED for p in plans)
        if file_sha256(args.input) != before:
            raise RuntimeError("input hash changed during read")
        context.validation_result = "BLOCKED" if context.blocked_count else "PASS"
    except Exception as exc:
        context.errors.append(f"{type(exc).__name__}: {exc}")
        context.validation_result = "FAIL"
    finally:
        context.finish()
        context.write_json(args.output_run_log)
    return 0 if context.validation_result == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
