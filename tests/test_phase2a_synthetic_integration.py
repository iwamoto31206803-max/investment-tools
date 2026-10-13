"""Integration of the supplied synthetic generator with unchanged production-facing readers.

Every provider response is artificial. No formal package or live Yahoo endpoint is used.
"""
from datetime import date
import json
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from scripts.generate_r01_synthetic_fixture import generate
from scripts.run_phase2a_synthetic_integration import (
    MOCK_CLOSED_DATES, run_integration, validate_fixture,
)
from r01_generator.package_reader import file_sha256


@pytest.fixture(scope="module")
def integration(tmp_path_factory):
    root = tmp_path_factory.mktemp("phase2a-synthetic")
    fixture = root / "R01_SYNTHETIC.xlsx"
    generate(fixture)
    output = root / "development-evidence"
    summary = run_integration(fixture, output)
    return fixture, output, summary


def test_generated_eight_sheet_package_reader_contract(integration):
    fixture, _, summary = integration
    assert len(summary["fixture"]["sheets"]) == 8
    assert summary["fixture"]["zip_member_count"] == 12
    assert summary["fixture"]["row_counts"] == {
        "metadata": 2, "source_registry": 1, "stock_master": 2, "universe": 2,
        "stock_daily": 370, "quality_summary": 2, "acquisition_log": 2, "corporate_actions": 0,
    }
    assert set(summary["histories"]) == {"8725", "7181"}
    for history in summary["histories"].values():
        assert history == {"count": 185, "first_date": date(2025, 12, 1),
                           "last_date": date(2026, 8, 14)}
    assert file_sha256(fixture) == summary["fixture"]["input_sha256"] == summary["input_sha256_after"]


def test_security_specific_planner_and_exact_overlap(integration):
    _, output, summary = integration
    plans = json.loads((output / "plans.json").read_text())
    assert {plan["security_code"] for plan in plans} == {"8725", "7181"}
    for plan in plans:
        assert plan["plan_status"] == "READY" and plan["acquisition_mode"] == "OVERLAP"
        assert plan["request_start_date"] == "2026-07-20"
        assert plan["request_end_date"] == "2026-09-24"
        assert plan["expected_overlap_observations"] == 20
    assert summary["plan_hash"] and summary["config_hash"]


def test_96_observation_gate_and_false_undercount_zero_calls(integration):
    _, output, summary = integration
    blocked = json.loads((output / "blocked_96.json").read_text())
    assert blocked["validation_result"] == "FAIL" and blocked["provider_calls"] == []
    for result in blocked["results"]:
        assert result["provider_status"] == "REJECTED"
        assert result["plan"]["expected_overlap_observations"] == 96
        assert result["plan"]["plan_status"] == "ACQUISITION_PLAN_BLOCKED"
    assert summary["false_overlap_count_rejected"] == ["7181", "8725"]


def test_reader_planner_engine_success_and_known_history_coverage(integration):
    _, output, summary = integration
    success = json.loads((output / "success.json").read_text())
    assert success["validation_result"] == "PASS" and len(success["provider_calls"]) == 2
    for result in success["results"]:
        assert result["provider_status"] == "SUCCESS"
        assert result["returned_count"] == result["normalized_count"] == 46
        assert result["expected_overlap_count"] == result["returned_overlap_count"] == 20
        assert result["actual_end_date"] == "2026-09-24"  # beyond previous R01 date
        assert result["response_metadata"]["yahoo_meta"]["fixtureKind"].startswith("SYNTHETIC")
    assert summary["live_network_calls"] == 0 and summary["synthetic_only"] is True


def test_mock_cli_separate_normalized_output_and_unchanged_xlsx(integration):
    fixture, output, summary = integration
    run = json.loads((output / "mock-cli" / "run.json").read_text())
    rows = [json.loads(line) for line in (output / "mock-cli" / "observations.jsonl").read_text().splitlines()]
    assert run["validation_result"] == "PASS" and len(rows) == 92
    assert run["input_sha256"] == run["input_sha256_after"] == file_sha256(fixture)
    assert run["source_commit"] and run["development_contract_hash"] and run["plan_hash"]
    assert run["normalized_output_sha256"] == file_sha256(output / "mock-cli" / "observations.jsonl")
    for code in ("8725", "7181"):
        selected = [row for row in rows if row["SECURITY_CODE"] == code]
        assert len(selected) == 46
        assert all(row["LINEAGE"]["provider_status"] == "SUCCESS" for row in selected)
        assert all(isinstance(row["CLOSE"], str) and isinstance(row["ADJUSTED_CLOSE"], str) for row in selected)
        assert all(date.fromisoformat(row["OBSERVATION_DATE"]) not in MOCK_CLOSED_DATES for row in selected)
    assert summary["mock_cli"]["provider_call_count"] == 2


@pytest.mark.parametrize("case,status,accepted", [
    ("missing_overlap", "PARTIAL", 45),
    ("duplicate", "DATA_QUALITY_ERROR", 0),
    ("outside_bounds", "DATA_QUALITY_ERROR", 0),
    ("one_provider_failure", "PROVIDER_ERROR", 0),
])
def test_incomplete_or_invalid_security_never_becomes_pass(integration, case, status, accepted):
    _, output, summary = integration
    evidence = json.loads((output / f"{case}.json").read_text())
    assert evidence["validation_result"] != "PASS"
    failed_code = "8725" if case == "one_provider_failure" else "7181"
    failed = next(item for item in evidence["results"] if item["security_code"] == failed_code)
    assert failed["provider_status"] == status and failed["normalized_count"] == accepted
    assert summary["cases"][case]["validation_result"] != "PASS"


def test_empty_response_and_no_data_change_distinct(integration):
    _, output, _ = integration
    empty = json.loads((output / "empty.json").read_text())
    unchanged = json.loads((output / "no_data_change.json").read_text())
    assert empty["validation_result"] == "NO_DATA" and len(empty["provider_calls"]) == 2
    assert all(result["provider_status"] == "NO_DATA" for result in empty["results"])
    assert unchanged["validation_result"] == "PASS" and unchanged["provider_calls"] == []
    assert all(result["provider_status"] == "NO_DATA_CHANGE" for result in unchanged["results"])


def test_transient_retry_keeps_exact_security_window(integration):
    _, output, _ = integration
    retry = json.loads((output / "retry.json").read_text())
    assert retry["validation_result"] == "PASS" and len(retry["provider_calls"]) == 3
    calls = [item for item in retry["provider_calls"] if item["security_code"] == "7181"]
    assert len(calls) == 2 and calls[0] == calls[1]
    assert next(item for item in retry["results"] if item["security_code"] == "7181")["retry_count"] == 1


def test_runner_rejects_unmarked_fixture_without_relaxing_reader(tmp_path):
    path = tmp_path / "unmarked.xlsx"
    generate(path)
    with ZipFile(path) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    members["xl/worksheets/sheet1.xml"] = members["xl/worksheets/sheet1.xml"].replace(b"SYNTHETIC", b"UNMARKED")
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    before = file_sha256(path)
    with pytest.raises(ValueError, match="explicitly marked synthetic"):
        validate_fixture(path)
    assert file_sha256(path) == before


def test_runner_preserves_existing_development_outputs(integration, tmp_path):
    fixture, _, _ = integration
    output = tmp_path / "existing-output"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("preserve")
    before = file_sha256(fixture)
    with pytest.raises(ValueError, match="directory must be new"):
        run_integration(fixture, output)
    assert marker.read_text() == "preserve" and file_sha256(fixture) == before


def test_runner_prohibits_network_even_outside_pytest_guard(integration, tmp_path, monkeypatch):
    import socket
    fixture, _, _ = integration
    def accidental_network(*args, **kwargs):
        socket.create_connection(("query1.finance.yahoo.com", 443))
    monkeypatch.setattr("scripts.run_phase2a_synthetic_integration.validate_fixture", accidental_network)
    with pytest.raises(AssertionError, match="synthetic integration prohibits live network"):
        run_integration(fixture, tmp_path / "guard-check")
