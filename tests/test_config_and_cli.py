import json

from r01_generator.cli import main
from r01_generator.config import GeneratorConfig


def test_config_hash_is_stable(tmp_path):
    first = GeneratorConfig()
    assert first.sha256 == GeneratorConfig().sha256
    assert len(first.sha256) == 64


def test_cli_writes_machine_readable_run_evidence(xlsx, tmp_path):
    output = tmp_path / "run.json"
    result = main(["--input", str(xlsx), "--config", "config/r01_generator.default.toml",
                   "--dry-run", "--output-run-log", str(output),
                   "--analysis-date", "2026-09-18", "--run-id", "test-run",
                   "--snapshot-id", "test-snapshot"])
    evidence = json.loads(output.read_text())
    assert result == 0
    assert evidence["generator_version"] == "0.1.0-dev"
    assert evidence["validation_result"] == "PASS"
    assert evidence["mode_counts"] == {"OVERLAP": 1}
    assert evidence["input_sha256"] and evidence["peak_rss_mb"] > 0
