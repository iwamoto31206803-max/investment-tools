"""Offline development integration runner for the synthetic 8725/7181 fixture.

Never constructs a live HTTP transport. Socket/DNS access is prohibited while
running. Writes only a new development directory, never the input XLSX.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
import xml.etree.ElementTree as ET
from zipfile import ZipFile

# Permit the documented direct script invocation without requiring installation.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.generate_r01_synthetic_fixture import CODES, HEADERS
from r01_generator.acquisition.engine import AcquisitionEngine
from r01_generator.acquisition.models import canonical_hash, json_value, run_validation
from r01_generator.acquisition.provider import HTTPResponse, TOKYO, YahooChartProvider, request_timestamps
from r01_generator.acquisition_plan import AcquisitionPlanner, PlanStatus, RunDecision
from r01_generator.cli import main as generator_main
from r01_generator.config import GeneratorConfig
from r01_generator.package_reader import MAIN, REQUIRED_SHEETS, R01PackageReader, file_sha256
from r01_generator.validation import PlanValidationError

ANALYSIS_DATE = date(2026, 9, 24)
MOCK_RETRIEVED_AT = datetime(2026, 9, 25, tzinfo=timezone.utc)
# Deliberate mock gaps; not an authoritative exchange-calendar implementation.
MOCK_CLOSED_DATES = frozenset({date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23)})


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, default=json_value, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def _forbid_network(*args, **kwargs):
    raise AssertionError("synthetic integration prohibits live network access")


def validate_fixture(path: Path) -> tuple[dict, dict]:
    """Validate generated package structure and observable Reader contract, not formal approval."""
    before = file_sha256(path)
    with ZipFile(path) as archive:
        _require(archive.testzip() is None, "synthetic ZIP integrity failure")
        member_count = len(archive.namelist())
        with R01PackageReader(path) as reader:
            _require(set(reader.sheet_paths) == REQUIRED_SHEETS, "exactly eight required sheets expected")
            _require(reader.read_metadata().get("FIXTURE_KIND") == "SYNTHETIC",
                     "runner accepts only explicitly marked synthetic fixtures")
            row_counts = {}
            for name, member in reader.sheet_paths.items():
                root = ET.fromstring(archive.read(member))
                rows = root.findall(f".//{{{MAIN}}}row")
                headers = [cell.find(f".//{{{MAIN}}}t").text for cell in rows[0]]
                _require(headers == HEADERS[name].split(), f"synthetic header mismatch: {name}")
                row_counts[name] = len(rows) - 1
            inventory = reader.component_inventory()
            histories = reader.read_stock_daily_index()
    _require(set(histories) == set(CODES), "synthetic 8725/7181 history selection required")
    for code, history in histories.items():
        _require(history.observation_count == 185, f"expected 185 synthetic observations: {code}")
        _require(history.observation_dates[0] == date(2025, 12, 1) and
                 history.latest_observation_date == date(2026, 8, 14), "unexpected synthetic date span")
    _require(row_counts["stock_daily"] == 370, "expected 370 synthetic stock_daily rows")
    _require(file_sha256(path) == before, "Reader changed input fixture")
    return histories, {"input_sha256": before, "sheets": sorted(REQUIRED_SHEETS),
                       "zip_member_count": member_count, "row_counts": row_counts,
                       "component_inventory": [asdict(item) for item in inventory]}


def mock_days(plan, history) -> tuple[date, ...]:
    """Old overlap plus explicitly artificial new weekday bars and calendar gaps."""
    days = [day for day in history.observation_dates
            if plan.request_start_date <= day <= plan.request_end_date]
    day = history.latest_observation_date + timedelta(days=1)
    while day <= plan.request_end_date:
        if day.weekday() < 5 and day not in MOCK_CLOSED_DATES:
            days.append(day)
        day += timedelta(days=1)
    return tuple(days)


def mock_response(code: str, days: tuple[date, ...]) -> HTTPResponse:
    count = len(days)
    prices = [1000 + i for i in range(count)]
    # Integer prices avoid incidental binary floating-point fixture rounding.
    payload = {"chart": {"error": None, "result": [{
        "meta": {"symbol": code + ".T", "exchangeTimezoneName": "Asia/Tokyo",
                 "currency": "JPY", "fixtureKind": "SYNTHETIC_MOCK_NOT_MARKET_DATA"},
        "timestamp": [int(datetime.combine(day, time(9), TOKYO).timestamp()) for day in days],
        "indicators": {
            "quote": [{"open": prices, "high": [p + 5 for p in prices],
                       "low": [p - 5 for p in prices], "close": [p + 1 for p in prices],
                       "volume": [100000] * count}],
            "adjclose": [{"adjclose": prices}],
        },
    }]}}
    return HTTPResponse(200, json.dumps(payload).encode())


class MockYahooTransport:
    """Exact-request fixture transport; cannot delegate to a real network implementation."""

    def __init__(self, plans, histories, *, variant="success"):
        self.plans = {plan.security_code: plan for plan in plans}
        self.histories, self.variant = histories, variant
        self.calls = []
        self.attempts = {}

    def get(self, url: str, *, timeout: float) -> HTTPResponse:
        parsed = urlparse(url)
        _require(parsed.scheme == "https" and parsed.netloc == "query1.finance.yahoo.com",
                 "unexpected mock provider destination")
        code = parsed.path.rsplit("/", 1)[-1].removesuffix(".T")
        _require(parsed.path == f"/v8/finance/chart/{code}.T" and code in self.plans,
                 "unexpected synthetic ticker")
        plan = self.plans[code]
        first, last = request_timestamps(plan.request_start_date, plan.request_end_date)
        _require(parse_qs(parsed.query) == {"period1": [str(first)], "period2": [str(last)],
                  "interval": ["1d"], "includeAdjustedClose": ["true"]},
                 "request differs from approved plan or end semantics")
        self.calls.append({"security_code": code, "period1": first, "period2_exclusive": last,
                           "timeout_seconds": timeout})
        self.attempts[code] = self.attempts.get(code, 0) + 1
        if self.variant == "retry" and code == "7181" and self.attempts[code] == 1:
            return HTTPResponse(503, b"")
        if self.variant == "one_provider_failure" and code == "8725":
            return HTTPResponse(404, b"")
        days = mock_days(plan, self.histories[code])
        if self.variant == "empty":
            days = ()
        elif self.variant == "missing_overlap" and code == "7181":
            days = days[1:]
        elif self.variant == "duplicate" and code == "7181":
            days = (days[0], *days)
        elif self.variant == "outside_bounds" and code == "7181":
            days = (plan.request_start_date - timedelta(days=1), *days)
        return mock_response(code, days)


def _run_case(plans, histories, config, output, *, variant="success", decision=None):
    transport = MockYahooTransport(plans, histories, variant=variant)
    delays = []
    provider = YahooChartProvider(transport, sleep=delays.append, clock=lambda: 0,
                                 now=lambda: MOCK_RETRIEVED_AT)
    results = AcquisitionEngine(provider, config).run(plans, histories, run_decision=decision)
    evidence = {"synthetic_only": True, "live_network_calls": 0,
                "validation_result": run_validation(results), "provider_calls": transport.calls,
                "delays_seconds": delays, "results": [result.evidence() for result in results]}
    _write_json(output, evidence)
    return evidence


def run_integration(input_path: Path, development_dir: Path) -> dict:
    """Run the real Reader/Planner/Engine/CLI against a synthetic fixture offline."""
    # Guard also applies outside pytest, including potential accidental transport regressions.
    with patch("socket.socket.connect", _forbid_network), \
         patch("socket.socket.connect_ex", _forbid_network), \
         patch("socket.create_connection", _forbid_network), \
         patch("socket.getaddrinfo", _forbid_network):
        return _run_integration(input_path, development_dir)


def _run_integration(input_path: Path, development_dir: Path) -> dict:
    development_dir = development_dir.resolve()
    _require(not development_dir.exists(), "integration output directory must be new")
    development_dir.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc)
    histories, fixture = validate_fixture(input_path)
    config = GeneratorConfig.load("config/r01_generator.default.toml")
    planner = AcquisitionPlanner(config)
    plans = planner.plan_all(histories, ANALYSIS_DATE)
    for plan in plans:
        _require(plan.plan_status is PlanStatus.READY and
                 plan.request_start_date == date(2026, 7, 20) and
                 plan.expected_overlap_observations == 20, "unexpected regular OVERLAP plan")
    _write_json(development_dir / "plans.json", [asdict(plan) for plan in plans])

    cases = {}
    for variant in ("success", "retry", "missing_overlap", "duplicate", "outside_bounds",
                    "empty", "one_provider_failure"):
        cases[variant] = _run_case(plans, histories, config, development_dir / f"{variant}.json",
                                  variant=variant)
    expected = {"success": "PASS", "retry": "PASS", "missing_overlap": "PARTIAL",
                "duplicate": "FAIL", "outside_bounds": "FAIL", "empty": "NO_DATA",
                "one_provider_failure": "FAIL"}
    for variant, outcome in expected.items():
        _require(cases[variant]["validation_result"] == outcome, f"wrong outcome for {variant}")
    for result in cases["success"]["results"]:
        _require(result["returned_count"] == result["normalized_count"] == 46 and
                 result["expected_overlap_count"] == result["returned_overlap_count"] == 20,
                 "expected 20 old plus 26 artificial new observations")

    blocked = [planner.plan(code, history, ANALYSIS_DATE,
                            forced_overlap_start=history.observation_dates[-96])
               for code, history in sorted(histories.items())]
    _require(all(plan.expected_overlap_observations == 96 and
                 plan.plan_status is PlanStatus.ACQUISITION_PLAN_BLOCKED for plan in blocked),
             "96-observation sanity gate did not block")
    cases["blocked_96"] = _run_case(blocked, histories, config, development_dir / "blocked_96.json")
    _require(not cases["blocked_96"]["provider_calls"], "blocked plans reached provider")
    false_count_rejected = []
    for code, history in sorted(histories.items()):
        try:
            planner.plan(code, history, ANALYSIS_DATE,
                         forced_overlap_start=history.observation_dates[-96],
                         expected_overlap_observations=20)
        except PlanValidationError:
            false_count_rejected.append(code)
    _require(set(false_count_rejected) == set(CODES), "caller undercount bypassed sanity gate")
    decision = RunDecision(no_new_price_facts=True)
    unchanged = planner.plan_all(histories, ANALYSIS_DATE, run_decision=decision)
    cases["no_data_change"] = _run_case(unchanged, histories, config,
                                      development_dir / "no_data_change.json", decision=decision)
    _require(not cases["no_data_change"]["provider_calls"], "NO_DATA_CHANGE reached provider")

    contract = {"schema_version": 1, "input_sha256": fixture["input_sha256"],
                "analysis_date": ANALYSIS_DATE.isoformat(),
                "decision_source": "synthetic-integration-development-only",
                "securities": [{"security_code": code} for code in CODES]}
    contract_path = development_dir / "development-contract.json"
    _write_json(contract_path, contract)
    transport = MockYahooTransport(plans, histories)
    delays = []
    def injected_provider(transport, **kwargs):
        return YahooChartProvider(transport, sleep=delays.append, clock=lambda: 0,
                                  now=lambda: MOCK_RETRIEVED_AT, **kwargs)
    cli_dir = development_dir / "mock-cli"
    with patch("r01_generator.cli.UrllibTransport", return_value=transport), \
         patch("r01_generator.cli.YahooChartProvider", injected_provider):
        exit_code = generator_main(["--input", str(input_path), "--config",
            "config/r01_generator.default.toml", "--acquire", "--analysis-date", ANALYSIS_DATE.isoformat(),
            "--development-contract", str(contract_path), "--development-dir", str(cli_dir),
            "--output-run-log", str(cli_dir / "run.json")])
    evidence = json.loads((cli_dir / "run.json").read_text())
    records = [json.loads(line) for line in (cli_dir / "observations.jsonl").read_text().splitlines()]
    _require(exit_code == 0 and evidence["validation_result"] == "PASS" and len(records) == 92,
             "mock CLI acquisition integration failed")
    _require(evidence["input_sha256"] == evidence["input_sha256_after"] == fixture["input_sha256"],
             "CLI input hash changed")
    _require(all(record["LINEAGE"]["provider_status"] == "SUCCESS" for record in records),
             "unexpected CLI row lineage")
    _write_json(development_dir / "mock-cli-transport.json", {"synthetic_only": True,
                "transport": "MockYahooTransport", "live_network_calls": 0,
                "provider_calls": transport.calls, "delays_seconds": delays})
    final_sha = file_sha256(input_path)
    _require(final_sha == fixture["input_sha256"], "integration changed input fixture")
    source_commit = subprocess.run(["git", "rev-parse", "HEAD"], check=True,
                                   capture_output=True, text=True).stdout.strip()
    summary = {"synthetic_only": True, "production_approved": False,
        "live_network_calls": 0, "source_commit": source_commit,
        "execution_timestamp": started.isoformat(), "analysis_date": ANALYSIS_DATE,
        "fixture": fixture, "input_sha256_after": final_sha, "config_hash": config.sha256,
        "plan_hash": canonical_hash([asdict(plan) for plan in plans]),
        "false_overlap_count_rejected": false_count_rejected,
        "histories": {code: {"count": item.observation_count,
                       "first_date": item.observation_dates[0], "last_date": item.latest_observation_date}
                      for code, item in sorted(histories.items())},
        "cases": {name: {"validation_result": case["validation_result"],
                          "provider_call_count": len(case["provider_calls"]),
                          "results": [{"security_code": result["security_code"],
                                       "status": result["provider_status"],
                                       "returned_count": result["returned_count"],
                                       "normalized_count": result["normalized_count"],
                                       "retry_count": result["retry_count"]}
                                      for result in case["results"]]}
                  for name, case in cases.items()},
        "mock_cli": {"exit_code": exit_code, "validation_result": evidence["validation_result"],
                     "normalized_count": len(records), "provider_call_count": len(transport.calls),
                     "normalized_output_sha256": evidence["normalized_output_sha256"]},
        "script_sha256": {name: file_sha256(Path(__file__).parent / name)
                          for name in ("generate_r01_synthetic_fixture.py",
                                       "run_phase2a_synthetic_integration.py")}}
    _write_json(development_dir / "summary.json", summary)
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="generated synthetic XLSX only")
    parser.add_argument("--development-dir", type=Path, required=True, help="new offline evidence directory")
    args = parser.parse_args(argv)
    # The checked-in config is resolved from the documented repository working directory.
    result = run_integration(args.input, args.development_dir)
    print(f"Synthetic integration PASS: {len(result['fixture']['sheets'])} sheets, "
          f"92 mock CLI rows; live network calls=0; evidence={args.development_dir / 'summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
