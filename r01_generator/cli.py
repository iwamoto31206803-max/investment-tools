from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import date
import json
from pathlib import Path
import subprocess

from .acquisition_plan import AcquisitionPlanner, PlanStatus
from .config import GeneratorConfig
from .context import RunContext
from .package_reader import R01PackageReader, file_sha256
from .acquisition import ENGINE_VERSION
from .acquisition.contract import DevelopmentContract
from .acquisition.engine import AcquisitionEngine
from .acquisition.models import canonical_hash, json_value, run_validation
from .acquisition.provider import ProviderSettings, UrllibTransport, YahooChartProvider
from .selective_writer import write_development_xlsx


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="R01 development planner and opt-in acquisition")
    result.add_argument("--input", required=True)
    result.add_argument("--config", required=True)
    mode = result.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="plan only (default); never contacts Yahoo")
    mode.add_argument("--acquire", action="store_true", help="explicit opt-in to live Yahoo acquisition")
    result.add_argument("--output-run-log", required=True)
    result.add_argument("--analysis-date", type=date.fromisoformat, required=True)
    result.add_argument("--run-id")
    result.add_argument("--snapshot-id")
    result.add_argument("--development-contract", help="input-bound JSON selection and window approvals")
    result.add_argument("--development-dir", help="new directory for development acquisition outputs")
    result.add_argument("--execution", choices=["sequential"], default="sequential")
    result.add_argument("--output-xlsx", help="opt-in development XLSX inside the new development directory")
    result.add_argument("--writer-source-id", help="existing source_registry ID for the acquired provider")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.output_xlsx and (not args.acquire or not args.writer_source_id):
        parser().error("--output-xlsx requires --acquire and an explicit --writer-source-id")
    input_path = Path(args.input).resolve()
    log_path = Path(args.output_run_log).resolve()
    protected = [input_path, Path(args.config).resolve()]
    if args.development_contract:
        protected.append(Path(args.development_contract).resolve())
    if any(log_path == path or (log_path.exists() and path.exists() and log_path.samefile(path))
           for path in protected):
        parser().error("run log must not overwrite an input, contract, or configuration")
    if args.acquire:
        if not args.development_contract or not args.development_dir:
            parser().error("--acquire requires --development-contract and --development-dir")
        development_dir = Path(args.development_dir).resolve()
        if args.output_xlsx:
            xlsx = Path(args.output_xlsx).resolve()
            if (xlsx.parent.resolve() != development_dir or xlsx == log_path or
                    xlsx.name == "observations.jsonl" or
                    xlsx.with_suffix(xlsx.suffix + ".writer.json") == log_path):
                parser().error("XLSX and writer sidecar need distinct paths in the development directory")
        if (development_dir.exists() or log_path.parent != development_dir or
                log_path.name == "observations.jsonl"):
            parser().error("acquisition requires a new development directory containing the run log")
        # A fresh exclusive directory prevents overwriting XLSX or prior run evidence.
        development_dir.mkdir(parents=True, exist_ok=False)
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
        contract = None
        if args.development_contract:
            contract = DevelopmentContract.load(args.development_contract, before, args.analysis_date)
            context.development_contract_hash = contract.sha256
            context.development_decision_source = contract.data["decision_source"]
            plans, histories, approvals, decision, mappings = contract.prepare(histories, config)
        else:
            plans = AcquisitionPlanner(config).plan_all(histories, args.analysis_date)
        context.plan_hash = canonical_hash([asdict(plan) for plan in plans])
        context.mode_counts = dict(Counter(p.acquisition_mode.value for p in plans))
        context.blocked_count = sum(p.plan_status is PlanStatus.ACQUISITION_PLAN_BLOCKED for p in plans)
        if args.acquire:
            settings = ProviderSettings(retry_limit=config.provider_retry_count)
            context.acquisition_engine_version = ENGINE_VERSION
            context.provider = "Yahoo Chart API"
            context.config_hash = canonical_hash({"generator": asdict(config), "provider": asdict(settings),
                                                  "contract_hash": contract.sha256,
                                                  "execution": args.execution})
            provider = YahooChartProvider(UrllibTransport(), settings=settings, mappings=mappings)
            results = AcquisitionEngine(provider, config).run(plans, histories, approvals=approvals,
                                                              run_decision=decision)
            context.acquisition_results = [result.evidence() for result in results]
            context.errors.extend(result.failure_reason for result in results if result.failure_reason)
            context.warnings.extend(warning for result in results for warning in result.warnings)
            context.validation_result = run_validation(results)
            output = development_dir / "observations.jsonl"
            with output.open("x", encoding="utf-8") as stream:
                for result in results:
                    for observation in result.observations:
                        record = asdict(observation)
                        record["LINEAGE"] = {"plan_hash": canonical_hash(asdict(result.plan)),
                            "acquisition_mode": result.plan.acquisition_mode,
                            "request_start_date": result.plan.request_start_date,
                            "request_end_date": result.plan.request_end_date,
                            "provider_status": result.status, "ticker": result.ticker}
                        stream.write(json.dumps(record, default=json_value, sort_keys=True) + "\n")
            context.normalized_output = str(output)
            context.normalized_output_sha256 = file_sha256(output)
            if args.output_xlsx:
                context.writer_result = write_development_xlsx(args.input, args.output_xlsx, results,
                    expected_input_sha256=before, source_ids={"Yahoo Chart API": args.writer_source_id},
                    config=config, run_evidence={"run_id": context.run_id,
                        "source_commit": context.source_commit, "config_hash": context.config_hash,
                        "development_contract_hash": context.development_contract_hash,
                        "normalized_output_sha256": context.normalized_output_sha256})
        else:
            context.validation_result = "BLOCKED" if context.blocked_count else "PASS"
        if file_sha256(args.input) != before:
            raise RuntimeError("input hash changed during read")
    except Exception as exc:
        context.errors.append(f"{type(exc).__name__}: {exc}")
        context.validation_result = "FAIL"
    finally:
        context.input_sha256_after = file_sha256(args.input)
        if context.input_sha256_after != before:
            context.validation_result = "FAIL"
            context.errors.append("input SHA-256 changed during execution")
        context.finish()
        context.write_json(args.output_run_log)
    return 0 if context.validation_result == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
