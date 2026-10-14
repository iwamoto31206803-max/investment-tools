"""Generate a deterministic, entirely synthetic eight-sheet R01 XLSX fixture.

Only column names/sheet names reflect the accepted R01 schema. No real prices,
names, source URLs, account information or accepted workbook bytes are included.
No third-party packages or network access required.
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

HEADERS = {
  "metadata": "FIELD VALUE DESCRIPTION",
  "source_registry": "SOURCE_ID SOURCE_NAME SOURCE_URL DATA_DEFINITION ADJUSTMENT_DEFINITION SUPPORTED_FIELDS KNOWN_LIMITATIONS RATE_LIMIT APPROVED_STATUS",
  "stock_master": "SECURITY_CODE STOCK_NAME SECTOR_ID SECTOR_NAME MARKET SECURITY_TYPE DOMESTIC_COMMON_STOCK_FLAG TRADING_UNIT TRADING_UNIT_SOURCE TRADING_UNIT_AS_OF_DATE LISTING_STATUS ACTIVE_FLAG MASTER_AS_OF_DATE MASTER_SOURCE DATA_QUALITY_NOTE",
  "universe": "AS_OF_DATE SECURITY_CODE PRIMARY_UNIVERSE_REASON ADDITIONAL_UNIVERSE_REASONS SOURCE_SECTOR_ID SOURCE_SECTOR_NAME ACQUISITION_MODE PRE_SCREEN_STATUS PRE_SCREEN_REASON ACTIVE_FLAG FIRST_INCLUDED_DATE EXCEPTION_REASON DATA_QUALITY_NOTE",
  "stock_daily": "DATE SECURITY_CODE OPEN HIGH LOW CLOSE ADJUSTED_OPEN ADJUSTED_HIGH ADJUSTED_LOW ADJUSTED_CLOSE VOLUME ADJUSTMENT_FACTOR SOURCE_ID ACQUISITION_METHOD ACQUIRED_AT RAW_OHLCV_COMPLETE ADJUSTED_CLOSE_COMPLETE ADJUSTED_OHLC_COMPLETE ROW_QUALITY DATA_QUALITY_NOTE",
  "quality_summary": "AS_OF_DATE SECURITY_CODE STOCK_NAME SECTOR_ID SECTOR_NAME PRIMARY_UNIVERSE_REASON ACTIVE_FLAG COMMON_END_DATE SECURITY_LAST_VALID_DATE END_DATE_LAG_TRADING_DAYS VALID_OBSERVATION_COUNT PRICE_HISTORY_READY RETURN_1W_READY RETURN_1M_READY RETURN_3M_READY LIQUIDITY_20D_READY CORPORATE_ACTION_READY STOCK_MASTER_READY TRADEABILITY_INPUT_READY POSITION_CAPACITY_INPUT_READY ADJUSTMENT_STATUS SERIES_QUALITY T04_FULL_READY T04_NOT_READY_REASON DATA_QUALITY_NOTE",
  "acquisition_log": "ACQUISITION_RUN_ID REQUESTED_AT SECURITY_CODE SOURCE_ID ACQUISITION_METHOD REQUEST_START_DATE REQUEST_END_DATE HTTP_STATUS RETURNED_ROW_COUNT ATTEMPT_NUMBER RETRY_REASON FALLBACK_USED FALLBACK_SOURCE_ID FINAL_STATUS ERROR_MESSAGE REQUEST_TIMESTAMP RESPONSE_ROW_COUNT FALLBACK_REASON",
  "corporate_actions": "SECURITY_CODE ACTION_TYPE EX_DATE EFFECTIVE_DATE SPLIT_RATIO SOURCE_ID SOURCE_URL VERIFICATION_STATUS HISTORY_REFRESH_REQUIRED HISTORY_REFRESH_COMPLETED_AT DATA_QUALITY_NOTE"
}
SHEETS = list(HEADERS)
CODES = ("8725", "7181")


def make_rows():
    rows = {name: [] for name in SHEETS}
    def add(sheet, **values):
        rows[sheet].append([str(values.get(col, "")) for col in HEADERS[sheet].split()])
    add("metadata", FIELD="FIXTURE_KIND", VALUE="SYNTHETIC", DESCRIPTION="Never use for investment")
    add("metadata", FIELD="REFERENCE_SCHEMA", VALUE="R01_20260924", DESCRIPTION="Headers only")
    add("source_registry", SOURCE_ID="SYNTHETIC", SOURCE_NAME="Deterministic synthetic generator",
        APPROVED_STATUS="Development only", DATA_DEFINITION="Not actual market data")
    dates = []
    day = date(2025, 12, 1)
    while len(dates) < 185:
        if day.weekday() < 5:
            dates.append(day)
        day += timedelta(days=1)
    for k, code in enumerate(CODES):
        add("stock_master", SECURITY_CODE=code, STOCK_NAME="Synthetic " + code,
            SECTOR_ID="TEST", SECTOR_NAME="Synthetic", ACTIVE_FLAG="Yes",
            MASTER_SOURCE="SYNTHETIC", DATA_QUALITY_NOTE="Artificial")
        add("universe", SECURITY_CODE=code, AS_OF_DATE="2026-09-24",
            PRIMARY_UNIVERSE_REASON="Synthetic test", ACQUISITION_MODE="Overlap",
            ACTIVE_FLAG="Yes")
        add("quality_summary", SECURITY_CODE=code, STOCK_NAME="Synthetic " + code,
            AS_OF_DATE="2026-09-24", VALID_OBSERVATION_COUNT=185,
            T04_FULL_READY="No", T04_NOT_READY_REASON="Synthetic fixture")
        add("acquisition_log", ACQUISITION_RUN_ID="SYNTHETIC-001",
            SECURITY_CODE=code, SOURCE_ID="SYNTHETIC",
            ACQUISITION_METHOD="DETERMINISTIC_GENERATION",
            RETURNED_ROW_COUNT=185, FINAL_STATUS="SYNTHETIC")
        for i, day in enumerate(dates):
            close = round(800 + k * 350 + i * 1.13 + i % 9 * .23, 2)
            add("stock_daily", DATE=day.isoformat(), SECURITY_CODE=code,
                OPEN=round(close-1.5, 2), HIGH=round(close+3, 2),
                LOW=round(close-4, 2), CLOSE=close, ADJUSTED_CLOSE=close,
                VOLUME=100000+k*10000+i*100, ADJUSTMENT_FACTOR=1,
                SOURCE_ID="SYNTHETIC", ACQUISITION_METHOD="DETERMINISTIC_GENERATION",
                RAW_OHLCV_COMPLETE="Yes", ADJUSTED_CLOSE_COMPLETE="Yes",
                ROW_QUALITY="SYNTHETIC", DATA_QUALITY_NOTE="Artificial prices")
    return rows


def worksheet_xml(headers, rows):
    def cell(column, rownum, value):
        # All values are deliberately stored as inline strings to avoid locale
        # and spreadsheet date-serial ambiguity in this schema-level fixture.
        ref = f"{column}{rownum}"
        return f'<c r="{ref}" t="inlineStr"><is><t>{escape(value)}</t></is></c>'
    def excel_col(index):
        result = ""
        while index:
            index, remainder = divmod(index-1, 26)
            result = chr(65+remainder)+result
        return result
    records = [headers.split()] + rows
    parts = []
    for i, values in enumerate(records, 1):
        cells = "".join(cell(excel_col(j), i, v) for j, v in enumerate(values, 1))
        parts.append(f'<row r="{i}">{cells}</row>')
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData>' + "".join(parts) + '</sheetData></worksheet>')


def generate(path: Path):
    rows = make_rows()
    path.parent.mkdir(parents=True, exist_ok=True)
    names = "".join(
        f'<sheet name="{name}" sheetId="{i}" r:id="rId{i}"/>'
        for i, name in enumerate(SHEETS, 1))
    workbook = ('<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets>' + names + '</sheets></workbook>')
    rels = "".join(
        f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/'
        f'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
        for i in range(1, len(SHEETS)+1))
    relationships = ('<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + rels + '</Relationships>')
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(1, len(SHEETS)+1))
    types = ('<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        + overrides + '</Types>')
    package_rels = ('<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/></Relationships>')
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", types)
        archive.writestr("_rels/.rels", package_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships)
        for i, name in enumerate(SHEETS, 1):
            archive.writestr(f"xl/worksheets/sheet{i}.xml",
                worksheet_xml(HEADERS[name], rows[name]))
    with ZipFile(path) as archive:
        assert archive.testzip() is None
    print(f"Generated {path}: {len(SHEETS)} sheets, "
          f"{len(rows['stock_daily'])} synthetic daily rows")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path,
                        default=Path("tmp/R01_Integration_Synthetic_Fixture.xlsx"))
    args = parser.parse_args()
    generate(args.output)
