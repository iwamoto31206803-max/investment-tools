from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from urllib.parse import parse_qs, urlparse

import pytest

from r01_generator.acquisition.contract import DevelopmentContract
from r01_generator.acquisition.engine import AcquisitionEngine, WindowApproval
from r01_generator.acquisition.models import ResultStatus, canonical_hash, run_validation
from r01_generator.acquisition.provider import (
    HTTPResponse, ProviderError, ProviderSettings, TOKYO, TransientTransportError,
    UrllibTransport, YahooChartProvider, request_timestamps, ticker_for,
)
from r01_generator.acquisition_plan import (
    AcquisitionMode, AcquisitionPlanner, PlanStatus, RunDecision,
)
from r01_generator.cli import main
from r01_generator.config import GeneratorConfig
from r01_generator.package_reader import SecurityHistory, file_sha256

END = date(2026, 9, 24)
DAYS = (date(2026, 9, 17), date(2026, 9, 18))
CONFIG = GeneratorConfig(overlap_observation_target=2)
NOW = datetime(2026, 9, 25, tzinfo=timezone.utc)


def timestamp(day, hour=9):
    return int(datetime.combine(day, datetime.min.time(), TOKYO).replace(hour=hour).timestamp())


def body(days=DAYS, code="1001", adjusted=True):
    count = len(days)
    indicators = {"quote": [{"open": [100] * count, "high": [110] * count,
                   "low": [90] * count, "close": [105] * count, "volume": [1000] * count}]}
    if adjusted:
        indicators["adjclose"] = [{"adjclose": [102.5] * count}]
    return {"chart": {"error": None, "result": [{
        "meta": {"symbol": code + ".T", "exchangeTimezoneName": "Asia/Tokyo", "currency": "JPY"},
        "timestamp": [timestamp(day) for day in days], "indicators": indicators}]}}


def response(payload=None, status=200):
    return HTTPResponse(status, json.dumps(body() if payload is None else payload).encode())


class Transport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def get(self, url, *, timeout):
        self.calls.append((url, timeout))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def provider(replies, **kwargs):
    transport = Transport(replies)
    delays = []
    adapter = YahooChartProvider(transport, sleep=delays.append, clock=lambda: 0,
                                now=lambda: NOW, **kwargs)
    return adapter, transport, delays


def overlap(code="1001", days=DAYS):
    history = SecurityHistory(code, tuple(days))
    plan = AcquisitionPlanner(CONFIG).plan(code, history, END)
    return plan, {code: history}


def acquire(replies, plan=None, histories=None, **kwargs):
    if plan is None:
        plan, histories = overlap()
    adapter, transport, delays = provider(replies)
    result = AcquisitionEngine(adapter, CONFIG).run([plan], histories, **kwargs)[0]
    return result, transport, delays


def chart(payload):
    return payload["chart"]["result"][0]


def test_ready_plan_parsing_boundaries_timezone_and_lineage():
    result, transport, _ = acquire([response(body((*DAYS, END)))])
    assert result.status is ResultStatus.SUCCESS
    assert result.returned_overlap_count == result.expected_overlap_count == 2
    assert result.observations[-1].OBSERVATION_DATE == END  # future vs old R01 is allowed
    row = result.observations[0]
    assert row.CLOSE == Decimal("105") and row.ADJUSTED_CLOSE == Decimal("102.5")
    assert row.RETRIEVED_AT == NOW and row.SOURCE == "Yahoo Chart API"
    assert row.SECURITY_CODE == "1001" and row.VOLUME == Decimal("1000")
    assert result.response_metadata["yahoo_meta"]["currency"] == "JPY"
    query = parse_qs(urlparse(transport.calls[0][0]).query)
    assert query["period1"] == [str(timestamp(DAYS[0], 0))]
    assert query["period2"] == [str(timestamp(END + timedelta(days=1), 0))]
    assert query["interval"] == ["1d"] and query["includeAdjustedClose"] == ["true"]
    evidence = result.evidence()
    assert evidence["actual_start_date"] == DAYS[0] and evidence["returned_count"] == 3
    assert evidence["plan_hash"] == canonical_hash(asdict(result.plan))


def test_tokyo_date_not_utc_date_and_exclusive_end():
    payload = body()
    chart(payload)["timestamp"][0] = timestamp(DAYS[0], 0)  # previous UTC calendar day
    result, _, _ = acquire([response(payload)])
    assert result.status is ResultStatus.SUCCESS
    assert result.observations[0].OBSERVATION_DATE == DAYS[0]
    assert request_timestamps(END, END) == (timestamp(END, 0), timestamp(END + timedelta(days=1), 0))


@pytest.mark.parametrize("mutate", [
    lambda p: replace(p, plan_status=PlanStatus.ACQUISITION_PLAN_BLOCKED),
    lambda p: replace(p, plan_status="READY"),
    lambda p: replace(p, acquisition_mode="OVERLAP"),
    lambda p: replace(p, request_start_date=END + timedelta(days=1)),
    lambda p: replace(p, request_start_date=None),
    lambda p: replace(p, request_end_date=DAYS[0] - timedelta(days=1)),
    lambda p: replace(p, expected_overlap_observations=1),
    lambda p: replace(p, expected_overlap_observations=True),
    lambda p: replace(p, latest_existing_date=None),
    lambda p: replace(p, override_flag=True),
    lambda p: replace(p, expected_new_observations=10),
    lambda p: replace(p, full_refresh_reason="wrong mode"),
])
def test_invalid_or_blocked_plan_zero_calls(mutate):
    plan, histories = overlap()
    result, transport, _ = acquire([], mutate(plan), histories)
    assert result.status is ResultStatus.REJECTED and not transport.calls


def test_batch_preflight_and_duplicate_plans_zero_calls():
    plan, histories = overlap()
    adapter, transport, _ = provider([])
    results = AcquisitionEngine(adapter, CONFIG).run([plan, plan], histories)
    assert all(r.status is ResultStatus.REJECTED for r in results)
    assert not transport.calls


def test_no_data_change_requires_run_evidence_and_makes_zero_calls():
    _, histories = overlap()
    decision = RunDecision(no_new_price_facts=True)
    plan = AcquisitionPlanner(CONFIG).plan_all(histories, END, run_decision=decision)[0]
    result, transport, _ = acquire([], plan, histories, run_decision=decision)
    assert result.status is ResultStatus.NO_DATA_CHANGE and not transport.calls
    assert run_validation([result]) == "PASS"
    result, transport, _ = acquire([], plan, histories)
    assert result.status is ResultStatus.REJECTED and not transport.calls


@pytest.mark.parametrize("status", [429, 500, 502, 503, 599])
def test_transient_http_retry_success_same_window(status):
    result, transport, delays = acquire([response(status=status), response()])
    assert result.status is ResultStatus.SUCCESS and result.retry_count == 1
    assert transport.calls[0] == transport.calls[1]
    assert delays and max(delays) <= 8


def test_transient_network_retries_and_exhaustion():
    result, transport, _ = acquire([TransientTransportError(), response()])
    assert result.status is ResultStatus.SUCCESS and result.retry_count == 1
    result, transport, delays = acquire([response(status=429)] * 4)
    assert result.status is ResultStatus.PROVIDER_ERROR and result.retry_count == 3
    assert result.response_status == 429 and len(transport.calls) == 4
    assert "fallback unavailable" in result.failure_reason
    assert delays == [1, 1, 2, 1, 4, 1]  # rate delay remains separately conservative


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_permanent_http_no_retry(status):
    result, transport, _ = acquire([response(status=status)])
    assert result.status is ResultStatus.PROVIDER_ERROR
    assert result.response_status == status and result.retry_count == 0 and len(transport.calls) == 1


@pytest.mark.parametrize("payload", [
    {}, {"chart": []}, {"chart": {"error": None, "result": []}},
    {"chart": {"error": {"description": "sensitive error text"}, "result": None}},
])
def test_malformed_or_provider_error_is_not_retried_or_leaked(payload):
    result, transport, _ = acquire([response(payload)])
    assert result.status is ResultStatus.PROVIDER_ERROR and len(transport.calls) == 1
    assert "sensitive" not in result.failure_reason


@pytest.mark.parametrize("mutation", [
    lambda p: chart(p).pop("meta"),
    lambda p: chart(p)["meta"].update(symbol="9999.T"),
    lambda p: chart(p)["meta"].update(exchangeTimezoneName="UTC"),
    lambda p: chart(p)["indicators"]["quote"][0].update(open=[1]),
    lambda p: chart(p)["indicators"].update(quote=[]),
    lambda p: chart(p)["indicators"].update(adjclose=[{}]),
    lambda p: chart(p).update(timestamp=[True, timestamp(DAYS[1])]),
])
def test_malformed_chart_schema(mutation):
    payload = body()
    mutation(payload)
    result, transport, _ = acquire([response(payload)])
    assert result.status is ResultStatus.PROVIDER_ERROR and len(transport.calls) == 1


@pytest.mark.parametrize("adjusted", [True, False])
def test_missing_volume_and_adjusted_are_never_fabricated(adjusted):
    payload = body(adjusted=adjusted)
    chart(payload)["indicators"]["quote"][0].pop("volume")
    result, _, _ = acquire([response(payload)])
    assert result.status is ResultStatus.SUCCESS
    assert all(row.VOLUME is None for row in result.observations)
    assert all((row.ADJUSTED_CLOSE == Decimal("102.5") if adjusted
                else row.ADJUSTED_CLOSE is None) for row in result.observations)
    assert result.warnings


@pytest.mark.parametrize("field,value", [
    ("open", 111), ("low", 106), ("close", -1), ("volume", -1),
    ("volume", 1.25), ("high", float("nan")), ("low", float("inf")),
    ("close", "105"), ("open", True),
])
def test_invalid_ohlcv_is_quality_failure(field, value):
    payload = body()
    chart(payload)["indicators"]["quote"][0][field][0] = value
    result, transport, _ = acquire([response(payload)])
    assert result.status is ResultStatus.DATA_QUALITY_ERROR
    assert not result.observations and len(transport.calls) == 1


@pytest.mark.parametrize("days", [(DAYS[0], DAYS[0]), tuple(reversed(DAYS)),
                                   (DAYS[0] - timedelta(days=1), DAYS[1]),
                                   (*DAYS, END + timedelta(days=1))])
def test_duplicates_unordered_and_out_of_bounds_rejected(days):
    result, _, _ = acquire([response(body(days))])
    assert result.status is ResultStatus.DATA_QUALITY_ERROR and not result.observations


def test_market_holidays_weekends_not_synthesized_and_empty_distinct():
    # Respect/Autumn holidays and weekend gaps: no calendar row synthesis.
    result, _, _ = acquire([response(body((*DAYS, END)))])
    assert [r.OBSERVATION_DATE for r in result.observations] == [*DAYS, END]
    result, _, _ = acquire([response(body(()))])
    assert result.status is ResultStatus.NO_DATA and run_validation([result]) == "NO_DATA"
    assert result.returned_overlap_count == 0 and not result.observations


def test_missing_overlap_and_missing_required_prices_are_partial():
    result, _, _ = acquire([response(body((DAYS[1], END)))])
    assert result.status is ResultStatus.PARTIAL and run_validation([result]) == "PARTIAL"
    payload = body()
    chart(payload)["indicators"]["quote"][0]["open"][0] = None
    result, _, _ = acquire([response(payload)])
    assert result.status is ResultStatus.PARTIAL and result.observations[0].OPEN is None


def test_decimal_precision_preserved_from_json():
    reply = response()
    reply = replace(reply, body=reply.body.replace(b'102.5', b'102.1234567890123456789'))
    result, _, _ = acquire([reply])
    assert result.observations[0].ADJUSTED_CLOSE == Decimal("102.1234567890123456789")


@pytest.mark.parametrize("count,expected", [(0, ResultStatus.NO_DATA), (2, ResultStatus.PARTIAL),
                                            (150, ResultStatus.SUCCESS)])
def test_backfill_target(count, expected):
    plan = AcquisitionPlanner(CONFIG).plan("1001", None, END)
    start = END - timedelta(days=300)
    plan = replace(plan, request_start_date=start)
    days = tuple(start + timedelta(days=i) for i in range(count))
    approval = WindowApproval("1001", start, END, "development-ticket")
    result, _, _ = acquire([response(body(days))], plan, {"1001": None}, approvals={"1001": approval})
    assert result.status is expected and result.target_achieved is (count >= 150)


def test_unbounded_backfill_and_missing_window_approval_zero_calls():
    plan = AcquisitionPlanner(CONFIG).plan("1001", None, END)
    result, transport, _ = acquire([], plan, {"1001": None})
    assert result.status is ResultStatus.REJECTED and not transport.calls
    result, transport, _ = acquire([], replace(plan, request_start_date=DAYS[0]), {"1001": None})
    assert result.status is ResultStatus.REJECTED and not transport.calls


def test_full_refresh_explicit_reason_and_window():
    plan = AcquisitionPlanner(CONFIG).plan("1001", None, END, full_refresh_requested=True,
                                          full_refresh_reason="development source correction")
    plan = replace(plan, request_start_date=DAYS[0])
    approvals = {"1001": WindowApproval("1001", DAYS[0], END, "ticket")}
    result, _, _ = acquire([response()], plan, {"1001": None}, approvals=approvals)
    assert result.status is ResultStatus.SUCCESS
    result, transport, _ = acquire([], replace(plan, full_refresh_reason=""),
                                   {"1001": None}, approvals=approvals)
    assert result.status is ResultStatus.REJECTED and not transport.calls


def test_partial_batch_provider_failure_does_not_become_pass():
    plan1, histories1 = overlap()
    plan2, histories2 = overlap("1002")
    adapter, transport, _ = provider([response(), response(status=404)])
    results = AcquisitionEngine(adapter, CONFIG).run([plan1, plan2], histories1 | histories2)
    assert [r.status for r in results] == [ResultStatus.SUCCESS, ResultStatus.PROVIDER_ERROR]
    assert run_validation(results) == "FAIL" and len(transport.calls) == 2


@pytest.mark.parametrize("code,mapping", [("new", {}), ("1001", {"1001": "AAPL"}),
                                         ("1001", {"1001": "1001.T/../"})])
def test_invalid_security_mapping_no_calls(code, mapping):
    with pytest.raises(ProviderError):
        ticker_for(code, mapping)
    if mapping:
        with pytest.raises(ProviderError):
            YahooChartProvider(Transport([]), mappings=mapping)


def test_explicit_valid_mapping_and_alphanumeric_jp_code():
    assert ticker_for("130A", {}) == "130A.T"
    assert ticker_for("1001", {"1001": "1002.T"}) == "1002.T"
    plan, _ = overlap()
    adapter, transport, _ = provider([response(body(code="1002"))], mappings={"1001": "1002.T"})
    payload = adapter.fetch(plan)
    assert payload.ticker == "1002.T" and "/1002.T?" in transport.calls[0][0]


def contract_data(xlsx, **kwargs):
    return {"schema_version": 1, "input_sha256": file_sha256(xlsx),
            "analysis_date": END.isoformat(), "decision_source": "dev-ticket-2A",
            "securities": [{"security_code": "1001"}], **kwargs}


def write_contract(tmp_path, data):
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(data))
    return path


def cli_args(xlsx, output, contract=None, directory=None):
    args = ["--input", str(xlsx), "--config", "config/r01_generator.default.toml",
            "--analysis-date", END.isoformat(), "--output-run-log", str(output)]
    if contract:
        args += ["--development-contract", str(contract)]
    if directory:
        args += ["--acquire", "--development-dir", str(directory)]
    return args


def test_default_cli_and_dry_run_never_network(xlsx, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("default CLI constructed live transport")
    monkeypatch.setattr("r01_generator.cli.UrllibTransport", forbidden)
    for extra in ([], ["--dry-run"]):
        output = tmp_path / "dry.json"
        assert main(cli_args(xlsx, output) + extra) == 0
        assert json.loads(output.read_text())["validation_result"] == "PASS"


def test_acquire_cli_preserves_input_and_writes_separate_evidence(xlsx, tmp_path, monkeypatch):
    from conftest import make_xlsx
    # Use 20 weekday observations to exercise default config's READY overlap.
    days = tuple(date(2026, 8, 3) + timedelta(days=i) for i in range(30))
    make_xlsx(xlsx, {"1001": days})
    before = file_sha256(xlsx)
    contract = write_contract(tmp_path, contract_data(xlsx))
    directory = tmp_path / "development-run"
    transport = Transport([response(body(days[-20:]))])
    monkeypatch.setattr("r01_generator.cli.UrllibTransport", lambda: transport)
    assert main(cli_args(xlsx, directory / "run.json", contract, directory)) == 0
    evidence = json.loads((directory / "run.json").read_text())
    assert file_sha256(xlsx) == before == evidence["input_sha256_after"]
    assert evidence["provider"] == "Yahoo Chart API"
    assert evidence["plan_hash"] and evidence["development_contract_hash"]
    assert evidence["acquisition_engine_version"] == "0.2.0-dev"
    assert evidence["source_commit"] and evidence["config_hash"]
    assert evidence["validation_result"] == "PASS" and evidence["peak_rss_mb"] > 0
    assert len(transport.calls) == 1
    rows = [json.loads(line) for line in (directory / "observations.jsonl").read_text().splitlines()]
    assert len(rows) == 20 and rows[0]["CLOSE"] == "105"
    assert rows[0]["LINEAGE"]["provider_status"] == "SUCCESS"


@pytest.mark.parametrize("change", [
    {"input_sha256": "0" * 64}, {"analysis_date": "2026-01-01"},
    {"decision_source": ""}, {"schema_version": True}, {"unknown": 1},
    {"securities": []}, {"securities": [{"security_code": "1001"}] * 2},
    {"run_decision": {"no_new_price_facts": "true"}},
])
def test_contract_rejects_missing_or_inconsistent_evidence(xlsx, tmp_path, change):
    path = write_contract(tmp_path, contract_data(xlsx, **change))
    with pytest.raises((ValueError, ProviderError)):
        DevelopmentContract.load(path, file_sha256(xlsx), END)


def test_new_security_contract_requires_explicit_start(xlsx, tmp_path):
    data = contract_data(xlsx, securities=[{"security_code": "130A"}])
    path = write_contract(tmp_path, data)
    contract = DevelopmentContract.load(path, file_sha256(xlsx), END)
    with pytest.raises(ValueError, match="explicit approved start"):
        contract.prepare({}, CONFIG)
    data["securities"][0]["request_start_date"] = "2025-01-01"
    contract = DevelopmentContract.load(write_contract(tmp_path, data), file_sha256(xlsx), END)
    plans, histories, approvals, _, _ = contract.prepare({}, CONFIG)
    assert plans[0].acquisition_mode is AcquisitionMode.BACKFILL
    assert plans[0].request_start_date == date(2025, 1, 1) and histories == {"130A": None}
    assert approvals["130A"].decision_source == "dev-ticket-2A"


def test_cli_partial_failure_nonzero_and_no_output_overwrite(xlsx, tmp_path, monkeypatch):
    contract = write_contract(tmp_path, contract_data(xlsx))
    directory = tmp_path / "failed-run"
    transport = Transport([response(status=404)])
    monkeypatch.setattr("r01_generator.cli.UrllibTransport", lambda: transport)
    assert main(cli_args(xlsx, directory / "run.json", contract, directory)) == 2
    assert json.loads((directory / "run.json").read_text())["validation_result"] == "FAIL"
    with pytest.raises(SystemExit):
        main(cli_args(xlsx, directory / "run.json", contract, directory))
    before = file_sha256(xlsx)
    with pytest.raises(SystemExit):
        main(cli_args(xlsx, xlsx))
    alias = tmp_path / "alias.json"
    alias.hardlink_to(xlsx)
    with pytest.raises(SystemExit):
        main(cli_args(xlsx, alias))
    assert file_sha256(xlsx) == before


def test_cli_acquire_missing_contract_or_conflicting_flags_no_network(xlsx, tmp_path):
    with pytest.raises(SystemExit):
        main(cli_args(xlsx, tmp_path / "run.json") + ["--acquire"])
    with pytest.raises(SystemExit):
        main(cli_args(xlsx, tmp_path / "run.json") + ["--acquire", "--dry-run"])


def test_test_suite_network_guard():
    import socket
    with pytest.raises(AssertionError, match="live network"):
        socket.create_connection(("query1.finance.yahoo.com", 443))


def test_provider_defence_in_depth_rejects_blocked_and_unbounded_without_calls():
    plan, _ = overlap()
    adapter, transport, _ = provider([])
    for invalid in (replace(plan, plan_status=PlanStatus.ACQUISITION_PLAN_BLOCKED),
                    replace(plan, request_start_date=None),
                    replace(plan, acquisition_mode=AcquisitionMode.NO_DATA_CHANGE)):
        with pytest.raises(ProviderError):
            adapter.fetch(invalid)
    assert not transport.calls


@pytest.mark.parametrize("options", [{"retry_limit": -1}, {"retry_limit": 11},
                                      {"retry_limit": True}, {"min_request_interval_seconds": 0},
                                      {"timeout_seconds": float("nan")}])
def test_provider_settings_reject_unbounded_or_invalid_policy(options):
    with pytest.raises(ValueError):
        ProviderSettings(**options)


def test_actual_transport_classifies_transient_network_and_tls_failure(monkeypatch):
    import ssl
    from http.client import IncompleteRead
    from urllib.error import URLError

    def fail(error):
        def failing(*args, **kwargs):
            raise error
        monkeypatch.setattr("r01_generator.acquisition.provider.open_http", failing)

    fail(URLError(TimeoutError()))
    with pytest.raises(TransientTransportError):
        UrllibTransport().get("https://query1.finance.yahoo.com/test", timeout=1)
    fail(IncompleteRead(b"partial"))
    with pytest.raises(TransientTransportError):
        UrllibTransport().get("https://query1.finance.yahoo.com/test", timeout=1)
    fail(URLError(ssl.SSLCertVerificationError("certificate failure")))
    with pytest.raises(ProviderError, match="TLS certificate verification"):
        UrllibTransport().get("https://query1.finance.yahoo.com/test", timeout=1)


def test_empty_quote_object_is_valid_no_data_response():
    payload = body(())
    chart(payload)["indicators"] = {"quote": [{}]}
    chart(payload).pop("timestamp")
    result, transport, _ = acquire([response(payload)])
    assert result.status is ResultStatus.NO_DATA and len(transport.calls) == 1


def test_full_refresh_missing_existing_history_is_partial():
    _, histories = overlap()
    plan = AcquisitionPlanner(CONFIG).plan("1001", histories["1001"], END,
        full_refresh_requested=True, full_refresh_reason="development correction")
    plan = replace(plan, request_start_date=DAYS[0])
    approval = WindowApproval("1001", DAYS[0], END, "ticket")
    result, _, _ = acquire([response(body((DAYS[1],)))], plan, histories,
                          approvals={"1001": approval})
    assert result.status is ResultStatus.PARTIAL and result.returned_overlap_count == 1


def test_inconsistent_override_evidence_cannot_bypass_gate():
    from r01_generator.acquisition_plan import OverrideEvidence
    days = tuple(date(2026, 1, 1) + timedelta(days=i) for i in range(96))
    history = SecurityHistory("1001", days)
    evidence = OverrideEvidence("dev investigation", "ticket", (days[0], END),
                                (days[0], END), NOW)
    plan = AcquisitionPlanner(CONFIG).plan("1001", history, END,
        forced_overlap_start=days[0], override=evidence)
    result, transport, _ = acquire([response(body(days))], plan, {"1001": history})
    assert result.status is ResultStatus.SUCCESS and len(transport.calls) == 1
    bad = replace(evidence, override_window=(days[1], END))
    result, transport, _ = acquire([], replace(plan, override_evidence=bad), {"1001": history})
    assert result.status is ResultStatus.REJECTED and not transport.calls


def test_contract_overlap_start_is_not_silently_changed(xlsx, tmp_path):
    _, histories = overlap()
    data = contract_data(xlsx, securities=[{"security_code": "1001", "request_start_date": "2026-01-01"}])
    contract = DevelopmentContract.load(write_contract(tmp_path, data), file_sha256(xlsx), END)
    with pytest.raises(ValueError, match="cannot override planner start"):
        contract.prepare(histories, CONFIG)


def test_no_data_change_contract_cli_zero_calls(xlsx, tmp_path, monkeypatch):
    data = contract_data(xlsx, run_decision={"no_new_price_facts": True})
    contract = write_contract(tmp_path, data)
    transport = Transport([])
    monkeypatch.setattr("r01_generator.cli.UrllibTransport", lambda: transport)
    directory = tmp_path / "unchanged-run"
    assert main(cli_args(xlsx, directory / "run.json", contract, directory)) == 0
    assert not transport.calls and (directory / "observations.jsonl").read_text() == ""
    evidence = json.loads((directory / "run.json").read_text())
    assert evidence["acquisition_results"][0]["provider_status"] == "NO_DATA_CHANGE"


def test_contract_cli_invalid_hash_zero_calls_and_failure_evidence(xlsx, tmp_path, monkeypatch):
    contract = write_contract(tmp_path, contract_data(xlsx, input_sha256="0" * 64))
    transport = Transport([])
    monkeypatch.setattr("r01_generator.cli.UrllibTransport", lambda: transport)
    directory = tmp_path / "bad-contract-run"
    assert main(cli_args(xlsx, directory / "run.json", contract, directory)) == 2
    assert not transport.calls
    evidence = json.loads((directory / "run.json").read_text())
    assert evidence["validation_result"] == "FAIL" and evidence["input_sha256_after"] == file_sha256(xlsx)


def test_rate_limiting_between_securities_is_sequential():
    plan1, histories1 = overlap()
    plan2, histories2 = overlap("1002")
    adapter, transport, delays = provider([response(), response(body(code="1002"))])
    results = AcquisitionEngine(adapter, CONFIG).run([plan1, plan2], histories1 | histories2)
    assert all(result.status is ResultStatus.SUCCESS for result in results)
    assert len(transport.calls) == 2 and delays == [1]


def test_bounded_exponential_backoff_and_custom_retry_limit():
    settings = ProviderSettings(retry_limit=4, backoff_initial_seconds=2, backoff_max_seconds=3)
    adapter, transport, delays = provider([response(status=503)] * 5, settings=settings)
    plan, histories = overlap()
    result = AcquisitionEngine(adapter, CONFIG).run([plan], histories)[0]
    assert result.status is ResultStatus.PROVIDER_ERROR and result.retry_count == 4
    assert len(transport.calls) == 5 and delays[::2] == [2, 3, 3, 3]


def test_redirects_do_not_contact_another_host(monkeypatch):
    from urllib.error import HTTPError
    from r01_generator.acquisition.provider import NoRedirect
    from io import BytesIO
    assert NoRedirect().redirect_request(None, None, 302, "redirect", {},
                                         "https://other-provider.invalid") is None

    def redirect(*args, **kwargs):
        raise HTTPError("https://query1.finance.yahoo.com/", 302, "redirect",
                        {"Location": "https://other-provider.invalid"}, BytesIO(b"private"))

    monkeypatch.setattr("r01_generator.acquisition.provider.open_http", redirect)
    adapter = YahooChartProvider(UrllibTransport())
    plan, histories = overlap()
    result = AcquisitionEngine(adapter, CONFIG).run([plan], histories)[0]
    assert result.status is ResultStatus.PROVIDER_ERROR and result.response_status == 302
    assert result.retry_count == 0 and "private" not in result.failure_reason


def test_quality_error_evidence_retains_raw_count_but_emits_no_invalid_rows():
    result, _, _ = acquire([response(body((DAYS[0], DAYS[0])))])
    evidence = result.evidence()
    assert evidence["provider_status"] == ResultStatus.DATA_QUALITY_ERROR
    assert evidence["returned_count"] == 2 and evidence["normalized_count"] == 0
    assert evidence["actual_start_date"] == DAYS[0] and not result.observations


def test_bound_approval_and_run_decision_are_saved_in_result_evidence():
    plan = AcquisitionPlanner(CONFIG).plan("1001", None, END)
    plan = replace(plan, request_start_date=DAYS[0])
    approval = WindowApproval("1001", DAYS[0], END, "ticket")
    result, _, _ = acquire([response()], plan, {"1001": None}, approvals={"1001": approval})
    assert result.evidence()["admission_evidence"]["window_approval"] == asdict(approval)
    _, histories = overlap()
    decision = RunDecision(no_new_price_facts=True)
    plan = AcquisitionPlanner(CONFIG).plan_all(histories, END, run_decision=decision)[0]
    result, _, _ = acquire([], plan, histories, run_decision=decision)
    assert result.evidence()["admission_evidence"]["run_decision"] == asdict(decision)


def test_injected_standard_network_timeouts_are_retried():
    result, transport, _ = acquire([TimeoutError(), ConnectionError(), response()])
    assert result.status is ResultStatus.SUCCESS and result.retry_count == 2
    assert len(transport.calls) == 3
