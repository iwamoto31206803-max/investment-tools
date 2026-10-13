from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import zipfile

import pytest


SHEETS = ["metadata", "source_registry", "stock_master", "universe", "stock_daily",
          "quality_summary", "acquisition_log", "corporate_actions"]


def _sheet(rows):
    body = []
    for number, values in enumerate(rows, 1):
        cells = "".join(
            f'<c r="{column}{number}" t="inlineStr"><is><t>{value}</t></is></c>'
            for column, value in zip(("A", "B", "C", "D"), values)
        )
        body.append(f'<row r="{number}">{cells}</row>')
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetData>{"".join(body)}</sheetData></worksheet>')


def make_xlsx(path: Path, histories=None, missing=None):
    histories = histories or {"1001": [date(2026, 8, 3) + timedelta(days=i) for i in range(30)]}
    names = [name for name in SHEETS if name != missing]
    workbook_sheets, relationships = [], []
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, name in enumerate(names, 1):
            # Deliberately reverse physical numbering to test relationship resolution.
            physical = len(names) - index + 1
            workbook_sheets.append(f'<sheet name="{name}" sheetId="{index}" r:id="rId{index}"/>')
            relationships.append(
                f'<Relationship Id="rId{index}" Type="worksheet" Target="worksheets/custom{physical}.xml"/>'
            )
            if name == "metadata":
                rows = [["DATASET_NAME", "R01"], ["SPECIFICATION_VERSION", "1.0"],
                        ["UNKNOWN_FUTURE_FIELD", "retained"]]
            elif name == "stock_daily":
                rows = [["SECURITY_CODE", "OBSERVATION_DATE"]]
                rows += [[code, day.isoformat()] for code, days in histories.items() for day in days]
            else:
                rows = [["HEADER"]]
            archive.writestr(f"xl/worksheets/custom{physical}.xml", _sheet(rows))
        archive.writestr("xl/workbook.xml",
            '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
            + "".join(workbook_sheets) + "</sheets></workbook>")
        archive.writestr("xl/_rels/workbook.xml.rels",
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(relationships) + "</Relationships>")
    return path


@pytest.fixture
def xlsx(tmp_path):
    return make_xlsx(tmp_path / "r01.xlsx")
