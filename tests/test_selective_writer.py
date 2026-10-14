from dataclasses import replace
from datetime import date
from decimal import Decimal
import json
from zipfile import ZipFile
import xml.etree.ElementTree as ET

import pytest

from scripts.generate_r01_synthetic_fixture import generate
from scripts.run_phase2a_synthetic_integration import MockYahooTransport, MOCK_RETRIEVED_AT
from r01_generator.acquisition.engine import AcquisitionEngine
from r01_generator.acquisition.models import ResultStatus
from r01_generator.acquisition.provider import YahooChartProvider
from r01_generator.acquisition_plan import AcquisitionMode, AcquisitionPlanner, RunDecision
from r01_generator.cli import main
from r01_generator.config import GeneratorConfig
from r01_generator.package_reader import MAIN, R01PackageReader, file_sha256
from r01_generator.selective_writer import WriterError, write_development_xlsx


@pytest.fixture
def fixture(tmp_path):
    path = tmp_path / "synthetic.xlsx"
    generate(path)
    return path


def acquired(path, *, codes=("8725",), decision=None, variant="success"):
    with R01PackageReader(path) as reader:
        histories = reader.read_stock_daily_index()
    selected = {code: histories[code] for code in codes}
    config = GeneratorConfig()
    plans = AcquisitionPlanner(config).plan_all(selected, date(2026, 9, 24), run_decision=decision)
    transport = MockYahooTransport(plans, selected, variant=variant)
    provider = YahooChartProvider(transport, sleep=lambda _: None, now=lambda: MOCK_RETRIEVED_AT)
    return AcquisitionEngine(provider, config).run(plans, selected, run_decision=decision)


def rows(path):
    with R01PackageReader(path) as reader:
        records = list(reader._rows("stock_daily"))
    header = records[0]
    records = [{name: row.get(column, "") for column, name in header.items()} for row in records[1:]]
    return {(row["SECURITY_CODE"], row["DATE"]): row for row in records}


def write(path, target, results, **kwargs):
    return write_development_xlsx(path, target, results, expected_input_sha256=file_sha256(path),
                                  source_ids={"Yahoo Chart API": "SYNTHETIC"}, **kwargs)


def test_overlap_replaces_and_inserts_preserving_other_security_and_components(fixture, tmp_path):
    before, original = file_sha256(fixture), rows(fixture)
    results = acquired(fixture)
    target = tmp_path / "development" / "updated.xlsx"
    report = write(fixture, target, results)
    updated = rows(target)
    assert len(original) == 370 and len(updated) == 396
    assert report["updated_securities"] == {"8725": {"replaced": 20, "inserted": 26}}
    keys = {(row.SECURITY_CODE, row.OBSERVATION_DATE.isoformat()) for row in results[0].observations}
    assert all(updated[key] == value for key, value in original.items() if key not in keys)
    for observation in results[0].observations:
        row = updated[(observation.SECURITY_CODE, observation.OBSERVATION_DATE.isoformat())]
        assert Decimal(row["CLOSE"]) == observation.CLOSE
        assert Decimal(row["ADJUSTED_CLOSE"]) == observation.ADJUSTED_CLOSE
        assert row["SOURCE_ID"] == "SYNTHETIC" and row["ACQUIRED_AT"] == MOCK_RETRIEVED_AT.isoformat()
        assert row["ADJUSTMENT_FACTOR"] == row["ADJUSTED_OHLC_COMPLETE"] == row["ROW_QUALITY"] == ""
    with ZipFile(fixture) as old, ZipFile(target) as new:
        assert old.namelist() == new.namelist()
        for name in old.namelist():
            if name != "xl/worksheets/sheet5.xml":
                assert old.read(name) == new.read(name)
    with R01PackageReader(target) as reader:
        assert len(reader.sheet_paths) == 8
        histories = reader.read_stock_daily_index()
        assert histories["8725"].observation_count == 211
        assert histories["7181"].observation_count == 185
    assert file_sha256(fixture) == before == report["input_sha256_after"]
    sidecar = json.loads(target.with_suffix(".xlsx.writer.json").read_text())
    assert sidecar["output_sha256"] == file_sha256(target)
    assert sidecar["acquisition_results"][0]["plan_hash"]


def test_no_data_change_copies_bytes_without_rewriting(fixture, tmp_path):
    results = acquired(fixture, decision=RunDecision(no_new_price_facts=True))
    target = tmp_path / "unchanged.xlsx"
    report = write(fixture, target, results)
    assert file_sha256(target) == file_sha256(fixture)
    assert report["input_row_count"] == report["output_row_count"] == 370
    assert report["updated_securities"] == {"8725": {"replaced": 0, "inserted": 0}}
    assert report["preserved_components"] == 12


@pytest.mark.parametrize("failure", ["partial", "empty_batch", "missing_row", "unsupported", "wrong_source"])
def test_rejected_batch_publishes_no_workbook(fixture, tmp_path, failure):
    results = acquired(fixture)
    source_ids = {"Yahoo Chart API": "SYNTHETIC"}
    if failure == "partial":
        results[0].status = ResultStatus.PARTIAL
    elif failure == "empty_batch":
        results = []
    elif failure == "missing_row":
        results[0].observations = results[0].observations[1:]
    elif failure == "unsupported":
        results[0].plan = replace(results[0].plan, acquisition_mode=AcquisitionMode.FULL_REFRESH)
    else:
        source_ids = {"Yahoo Chart API": "UNREGISTERED"}
    target = tmp_path / "rejected.xlsx"
    before = file_sha256(fixture)
    with pytest.raises(WriterError):
        write_development_xlsx(fixture, target, results, expected_input_sha256=before, source_ids=source_ids)
    assert not target.exists() and not target.with_suffix(".xlsx.writer.json").exists()
    assert file_sha256(fixture) == before


def test_output_and_input_hash_protection(fixture, tmp_path):
    results = acquired(fixture)
    target = tmp_path / "output.xlsx"
    with pytest.raises(WriterError, match="SHA-256"):
        write_development_xlsx(fixture, target, results, expected_input_sha256="0" * 64,
                               source_ids={"Yahoo Chart API": "SYNTHETIC"})
    with pytest.raises(WriterError):
        write(fixture, fixture, results)
    target.write_text("existing user file")
    with pytest.raises(WriterError):
        write(fixture, target, results)
    assert target.read_text() == "existing user file"


def test_existing_duplicate_keys_fail_closed(fixture, tmp_path):
    results = acquired(fixture)
    with ZipFile(fixture) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    root = ET.fromstring(contents["xl/worksheets/sheet5.xml"])
    data = root.find(f"{{{MAIN}}}sheetData")
    duplicate = ET.fromstring(ET.tostring(data[1]))
    duplicate.set("r", "372")
    for cell in duplicate:
        cell.set("r", cell.get("r").rstrip("0123456789") + "372")
    data.append(duplicate)
    contents["xl/worksheets/sheet5.xml"] = ET.tostring(root)
    with ZipFile(fixture, "w") as archive:
        for name, content in contents.items():
            archive.writestr(name, content)
    target = tmp_path / "duplicate.xlsx"
    with pytest.raises(WriterError, match="duplicate stock_daily"):
        write(fixture, target, results)
    assert not target.exists()


def test_cli_reader_planner_acquisition_writer_reopens_output(fixture, tmp_path, monkeypatch):
    results = acquired(fixture)
    with R01PackageReader(fixture) as reader:
        histories = reader.read_stock_daily_index()
    plans = [results[0].plan]
    transport = MockYahooTransport(plans, histories)
    monkeypatch.setattr("r01_generator.cli.UrllibTransport", lambda: transport)
    def injected(transport, **kwargs):
        return YahooChartProvider(transport, sleep=lambda _: None, now=lambda: MOCK_RETRIEVED_AT, **kwargs)
    monkeypatch.setattr("r01_generator.cli.YahooChartProvider", injected)
    contract = tmp_path / "contract.json"
    contract.write_text(json.dumps({"schema_version": 1, "input_sha256": file_sha256(fixture),
        "analysis_date": "2026-09-24", "decision_source": "synthetic-writer-test",
        "securities": [{"security_code": "8725"}]}))
    directory = tmp_path / "pipeline"
    target = directory / "updated.xlsx"
    assert main(["--input", str(fixture), "--config", "config/r01_generator.default.toml", "--acquire",
        "--analysis-date", "2026-09-24", "--development-contract", str(contract),
        "--development-dir", str(directory), "--output-run-log", str(directory / "run.json"),
        "--output-xlsx", str(target), "--writer-source-id", "SYNTHETIC"]) == 0
    evidence = json.loads((directory / "run.json").read_text())
    assert evidence["writer_result"]["output_sha256"] == file_sha256(target)
    assert evidence["validation_result"] == "PASS" and len(transport.calls) == 1


def test_formatted_xml_dimension_and_filter_extend(fixture, tmp_path):
    with ZipFile(fixture) as archive:
        contents = {name: archive.read(name) for name in archive.namelist()}
    root = ET.fromstring(contents["xl/worksheets/sheet5.xml"])
    root.insert(0, ET.Element(f"{{{MAIN}}}dimension", {"ref": "A1:T371"}))
    ET.SubElement(root, f"{{{MAIN}}}autoFilter", {"ref": "B1:T371"})
    ET.indent(root)
    contents["xl/worksheets/sheet5.xml"] = ET.tostring(root)
    with ZipFile(fixture, "w") as archive:
        for name, content in contents.items():
            archive.writestr(name, content)
    results = acquired(fixture)
    output = tmp_path / "formatted-output.xlsx"
    write(fixture, output, results)
    with ZipFile(output) as archive:
        root = ET.fromstring(archive.read("xl/worksheets/sheet5.xml"))
        assert root.find(f"{{{MAIN}}}dimension").get("ref") == "A1:T397"
        assert root.find(f"{{{MAIN}}}autoFilter").get("ref") == "B1:T397"
    assert len(rows(output)) == 396


def test_cli_rejects_resolved_xlsx_log_alias_before_acquisition(fixture, tmp_path):
    directory = tmp_path / "alias-run"
    with pytest.raises(SystemExit):
        main(["--input", str(fixture), "--config", "config/r01_generator.default.toml", "--acquire",
            "--analysis-date", "2026-09-24", "--development-contract", str(tmp_path / "contract.json"),
            "--development-dir", str(directory), "--output-run-log", str(directory / "run.json"),
            "--output-xlsx", str(directory / ".." / directory.name / "run.json"),
            "--writer-source-id", "SYNTHETIC"])
    assert not directory.exists()
