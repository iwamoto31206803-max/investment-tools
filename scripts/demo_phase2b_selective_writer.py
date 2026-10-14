"""Run the real development CLI through a mocked provider and produce a new R01 XLSX."""
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys
from unittest.mock import patch

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.generate_r01_synthetic_fixture import generate
from scripts.run_phase2a_synthetic_integration import (
    MockYahooTransport, MOCK_RETRIEVED_AT, _forbid_network,
)
from r01_generator.acquisition.provider import YahooChartProvider
from r01_generator.acquisition_plan import AcquisitionPlanner
from r01_generator.cli import main as generator_main
from r01_generator.config import GeneratorConfig
from r01_generator.package_reader import R01PackageReader, file_sha256


def demo(directory: Path):
    directory.mkdir(parents=True, exist_ok=False)
    source = directory / "synthetic-input.xlsx"
    generate(source)
    config = GeneratorConfig()
    with R01PackageReader(source) as reader:
        histories = reader.read_stock_daily_index()
    plans = AcquisitionPlanner(config).plan_all({"8725": histories["8725"]}, date(2026, 9, 24))
    contract = directory / "contract.json"
    declaration = {"schema_version": 1, "input_sha256": file_sha256(source),
                   "analysis_date": "2026-09-24", "decision_source": "synthetic-selective-writer-demo",
                   "securities": [{"security_code": "8725"}]}
    contract.write_text(json.dumps(declaration))
    summaries = {}
    def injected(transport, **kwargs):
        return YahooChartProvider(transport, sleep=lambda _: None, now=lambda: MOCK_RETRIEVED_AT, **kwargs)
    for name in ("overlap", "no_data_change"):
        if name == "no_data_change":
            declaration["run_decision"] = {"no_new_price_facts": True}
            contract.write_text(json.dumps(declaration))
        transport = MockYahooTransport(plans, histories)
        output = directory / name
        with patch("r01_generator.cli.UrllibTransport", return_value=transport), \
             patch("r01_generator.cli.YahooChartProvider", injected), \
             patch("socket.socket.connect", _forbid_network), \
             patch("socket.create_connection", _forbid_network), \
             patch("socket.getaddrinfo", _forbid_network):
            status = generator_main(["--input", str(source), "--config", "config/r01_generator.default.toml",
                "--acquire", "--development-contract", str(contract), "--analysis-date", "2026-09-24",
                "--development-dir", str(output), "--output-run-log", str(output / "run.json"),
                "--output-xlsx", str(output / "updated.xlsx"), "--writer-source-id", "SYNTHETIC"])
        if status != 0:
            raise RuntimeError(f"demo failed; inspect {output / 'run.json'}")
        evidence = json.loads((output / "run.json").read_text())
        result = evidence["writer_result"]
        summaries[name] = {key: result[key] for key in
                          ("input_sha256", "input_sha256_after", "output_sha256", "input_row_count",
                           "output_row_count", "updated_securities", "supported_modes", "unsupported_modes")}
        summaries[name]["mock_provider_calls"] = len(transport.calls)
        summaries[name]["elapsed_seconds"] = evidence["elapsed_seconds"]
        summaries[name]["process_peak_rss_mb"] = evidence["peak_rss_mb"]
    report = {"synthetic_only": True, "live_provider_calls": 0, "results": summaries}
    (directory / "demo-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, indent=2))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--development-dir", type=Path, required=True, help="new development directory")
    demo(parser.parse_args().development_dir)
