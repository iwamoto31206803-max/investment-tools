from datetime import date
import hashlib

import pytest

from r01_generator.package_reader import REQUIRED_SHEETS, R01PackageReader, file_sha256
from r01_generator.validation import PackageValidationError
from conftest import make_xlsx


def test_valid_eight_sheets_relationship_resolution_and_metadata(xlsx):
    with R01PackageReader(xlsx) as reader:
        assert set(reader.sheet_paths) == REQUIRED_SHEETS
        assert reader.sheet_paths["metadata"] != "xl/worksheets/sheet1.xml"
        assert reader.read_metadata() == {
            "DATASET_NAME": "R01", "SPECIFICATION_VERSION": "1.0",
            "UNKNOWN_FUTURE_FIELD": "retained",
        }


def test_inventory_hash_and_input_preservation(xlsx):
    before = file_sha256(xlsx)
    with R01PackageReader(xlsx) as reader:
        inventory = {item.path: item for item in reader.component_inventory()}
        member = inventory[reader.sheet_paths["metadata"]]
        with reader._zip.open(member.path) as stream:
            assert member.sha256 == hashlib.sha256(stream.read()).hexdigest()
        assert member.uncompressed_size > 0 and member.compressed_size > 0
        index = reader.read_stock_daily_index()
    assert file_sha256(xlsx) == before
    assert index["1001"].observation_count == 30
    assert index["1001"].latest_observation_date == date(2026, 9, 1)


def test_missing_sheet_and_corrupt_fail(tmp_path):
    with pytest.raises(PackageValidationError, match="missing required"):
        R01PackageReader(make_xlsx(tmp_path / "missing.xlsx", missing="universe"))
    bad = tmp_path / "bad.xlsx"
    bad.write_bytes(b"not a zip")
    with pytest.raises(PackageValidationError, match="invalid xlsx"):
        R01PackageReader(bad)
