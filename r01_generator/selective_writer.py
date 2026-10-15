"""Development-only streaming stock_daily materialization; never edits its input."""
from __future__ import annotations

from dataclasses import asdict
from copy import copy, deepcopy
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import xml.etree.ElementTree as ET
from zipfile import ZipFile
from datetime import date
from urllib.parse import urlparse

from .acquisition.engine import validate_observations, validate_plan
from .acquisition.models import ResultStatus, canonical_hash, json_value
from .acquisition.normalization import DataQualityError
from .acquisition_plan import AcquisitionMode, RunDecision
from .config import GeneratorConfig
from .package_reader import MAIN, REQUIRED_SHEETS, R01PackageReader, file_sha256, cell_value
from .validation import PlanValidationError
from . import __version__


class WriterError(ValueError):
    pass


PRICES = ("OPEN", "HIGH", "LOW", "CLOSE", "ADJUSTED_CLOSE", "VOLUME")
DERIVED = ("ADJUSTED_OPEN", "ADJUSTED_HIGH", "ADJUSTED_LOW", "ADJUSTMENT_FACTOR",
           "RAW_OHLCV_COMPLETE", "ADJUSTED_CLOSE_COMPLETE", "ADJUSTED_OHLC_COMPLETE", "ROW_QUALITY")


def _require(condition, message):
    if not condition:
        raise WriterError(message)


def _rows(archive, member):
    # Remove each consumed row from sheetData so the root cannot retain historical cells.
    with archive.open(member) as stream:
        parser = ET.iterparse(stream, events=("start", "end", "start-ns"))
        root, sheet_data, namespaces = None, None, {}
        for event, element in parser:
            if event == "start-ns":
                _require(element[0] not in namespaces or namespaces[element[0]] == element[1],
                         "namespace prefix rebinding unsupported; refusing lossy serialization")
                namespaces[element[0]] = element[1]
            elif event == "start":
                if root is None:
                    root = element
                if element.tag == f"{{{MAIN}}}sheetData":
                    sheet_data = element
            elif element.tag == f"{{{MAIN}}}row":
                _require(sheet_data is not None, "row outside sheetData")
                yield element, root, namespaces
                sheet_data.remove(element)
        _require(sheet_data is not None, "stock_daily sheetData missing")


def _values(row, shared, *, allow_formulas=False):
    values = {}
    for cell in row.findall(f"{{{MAIN}}}c"):
        ref = cell.get("r", "")
        match = re.fullmatch(r"([A-Z]+)[1-9][0-9]*", ref)
        _require(match is not None, "explicit valid stock_daily cell references required")
        col = match[1]
        _require(col not in values, "duplicate cell column")
        _require(allow_formulas or cell.find(f"{{{MAIN}}}f") is None,
                 "stock_daily formula cells unsupported by update MVP")
        values[col] = cell_value(cell, shared)
    return values


def _columns(header):
    header = {col: value for col, value in header.items() if value.strip()}
    names = [value.strip().upper() for value in header.values()]
    _require(len(names) == len(set(names)) and all(names), "duplicate/empty stock_daily headers")
    columns = {value.strip().upper(): col for col, value in header.items()}
    dates = set(columns) & {"DATE", "OBSERVATION_DATE", "PRICE_DATE"}
    _require(len(dates) == 1, "exactly one existing date column required")
    date_name = next(iter(dates))
    _require(set((*PRICES, "SECURITY_CODE", "SOURCE_ID", "ACQUISITION_METHOD", "ACQUIRED_AT")) <= columns.keys(),
             "stock_daily normalized price and provenance columns required")
    return columns, date_name


def _key(values, columns, date_name, date_1904=False):
    code, raw = values.get(columns["SECURITY_CODE"], "").strip(), values.get(columns[date_name], "").strip()
    _require(code and raw, "stock_daily contains a missing security/date key")
    return code, R01PackageReader._excel_date(raw, date_1904)


def _fingerprint(digest, row):
    tail = row.tail
    row.tail = None  # whitespace outside a row is formatting, not historical data
    digest.update(ET.tostring(row, encoding="utf-8"))
    digest.update(b"\n")
    row.tail = tail


def _set_cell(row, column, value, numeric=False, cell_type=None):
    ref = column + row.get("r")
    cell = next((c for c in row if c.get("r") == ref), None)
    if cell is None:
        cell = ET.SubElement(row, f"{{{MAIN}}}c", {"r": ref})
    for child in list(cell):
        cell.remove(child)
    cell.attrib.pop("t", None)
    if value is not None:
        if numeric or cell_type == "d":
            cell.set("t", cell_type or "n")
            ET.SubElement(cell, f"{{{MAIN}}}v").text = str(value)
        else:
            cell.set("t", "inlineStr")
            ET.SubElement(ET.SubElement(cell, f"{{{MAIN}}}is"), f"{{{MAIN}}}t").text = str(value)
    # Keep valid column order even when inserting cells absent in a sparse old row.
    row[:] = sorted(row, key=lambda c: (len(re.sub(r"[0-9]", "", c.get("r", ""))), c.get("r", "")))


def _materialize(row, observation, columns, date_name, source_id, *, new=False, template=None, date_1904=False):
    if new:
        _set_cell(row, columns["SECURITY_CODE"], observation.SECURITY_CODE)
        date_cell = next((cell for cell in template if cell.get("r", "").rstrip("0123456789") == columns[date_name]), None)
        kind = date_cell.get("t", "n") if date_cell is not None else "inlineStr"
        value = observation.OBSERVATION_DATE.isoformat()
        if kind == "n":
            epoch = date(1904, 1, 1) if date_1904 else date(1899, 12, 30)
            value = (observation.OBSERVATION_DATE - epoch).days
            if not date_1904 and observation.OBSERVATION_DATE < date(1900, 3, 1):
                value -= 1
        _set_cell(row, columns[date_name], value, numeric=kind == "n", cell_type=kind if kind == "d" else None)
    for name in PRICES:
        _set_cell(row, columns[name], getattr(observation, name), numeric=True)
    for name, value in {"SOURCE_ID": source_id, "ACQUISITION_METHOD": observation.SOURCE,
                        "ACQUIRED_AT": observation.RETRIEVED_AT.isoformat()}.items():
        _set_cell(row, columns[name], value)
    for name in DERIVED:
        if name in columns:
            _set_cell(row, columns[name], None)
    if "DATA_QUALITY_NOTE" in columns:
        _set_cell(row, columns["DATA_QUALITY_NOTE"], "Development update; formal quality not recalculated")
    row.attrib.pop("spans", None)
    return row


def _new_row(template, number):
    row = ET.Element(f"{{{MAIN}}}row", {"r": str(number), **{
        key: value for key, value in template.attrib.items() if key in {"s", "customFormat", "ht", "customHeight"}}})
    for cell in template.findall(f"{{{MAIN}}}c"):
        column = cell.get("r", "").rstrip("0123456789")
        attrs = {"r": f"{column}{number}"}
        if "s" in cell.attrib:
            attrs["s"] = cell.get("s")
        ET.SubElement(row, f"{{{MAIN}}}c", attrs)
    return row


def _register_namespaces(namespaces):
    for prefix, uri in namespaces.items():
        # ElementTree reserves ns0... internally; retain the original binding in
        # the worksheet envelope, but serialize expanded names with a legal alias.
        ET.register_namespace("r01_" + prefix if re.fullmatch(r"ns[0-9]+", prefix) else prefix, uri)


def _unique(database, table, key):
    try:
        database.execute(f"INSERT INTO {table} VALUES (?, ?)", (key[0], key[1].isoformat()))
    except sqlite3.IntegrityError as exc:
        raise WriterError("duplicate stock_daily security/date key") from exc


def write_development_xlsx(input_path, output_path, results, *, expected_input_sha256,
                           source_ids, config=None, run_evidence=None):
    """Consume complete typed results, validate, stage, reopen, then publish a new XLSX + sidecar.

    source_ids maps provider names to existing source_registry IDs. Modes beyond
    OVERLAP and NO_DATA_CHANGE deliberately fail closed in this MVP.
    """
    config = config or GeneratorConfig()
    _require(not Path(output_path).is_symlink(), "writer output must not be a symlink")
    input_path, output_path = Path(input_path).resolve(), Path(output_path).resolve()
    report_path = output_path.with_suffix(output_path.suffix + ".writer.json")
    _require(not output_path.exists() and not output_path.is_symlink() and
             not report_path.exists() and not report_path.is_symlink(), "writer outputs must be new paths")
    _require(output_path.resolve() != input_path and report_path.resolve() != input_path,
             "writer cannot modify input in place")
    before = file_sha256(input_path)
    _require(before == expected_input_sha256, "writer input SHA-256 does not match acquisition input")
    _require(bool(results), "empty acquisition batch is not a validated NO_DATA_CHANGE decision")
    updates, seen_codes, source_mapping = {}, set(), {}
    with R01PackageReader(input_path) as reader:
        _require(set(reader.sheet_paths) == REQUIRED_SHEETS, "exactly eight required R01 sheets required")
        histories = reader.read_stock_daily_index()
        registry = iter(reader._rows("source_registry"))
        header = {col: value.strip().upper() for col, value in next(registry, {}).items() if value.strip()}
        _require("SOURCE_ID" in header.values() and len(set(header.values())) == len(header),
                 "source_registry requires unique headers including SOURCE_ID")
        records = {}
        for row in registry:
            entry = {name: row.get(col, "").strip() for col, name in header.items()}
            if not any(entry.values()):
                continue
            source_id = entry.get("SOURCE_ID", "")
            _require(source_id and source_id not in records, "missing/duplicate source_registry SOURCE_ID")
            records[source_id] = entry
        synthetic = reader.read_metadata().get("FIXTURE_KIND") == "SYNTHETIC"
        date_1904 = reader.date_1904
        shared = reader._shared_strings()
        member = reader.sheet_paths["stock_daily"]
        for result in results:
            plan = result.plan
            _require(plan.security_code not in seen_codes, "duplicate acquisition security")
            seen_codes.add(plan.security_code)
            _require(plan.acquisition_mode in {AcquisitionMode.OVERLAP, AcquisitionMode.NO_DATA_CHANGE},
                     "writer MVP supports only OVERLAP and NO_DATA_CHANGE")
            decision = result.admission_evidence.get("run_decision")
            try:
                validate_plan(plan, histories.get(plan.security_code), config,
                              run_decision=RunDecision(**decision) if decision is not None else None)
            except (PlanValidationError, TypeError, ValueError) as exc:
                raise WriterError("writer rejected plan admission evidence") from exc
            _require(not result.failure_reason, "acquisition failure cannot be written")
            if plan.acquisition_mode is AcquisitionMode.NO_DATA_CHANGE:
                _require(result.status is ResultStatus.NO_DATA_CHANGE and not result.observations,
                         "inconsistent NO_DATA_CHANGE result")
                continue
            _require(result.status is ResultStatus.SUCCESS and result.response_status == 200,
                     "failed/incomplete acquisition batch cannot be written")
            _require(result.observations and result.provider_returned_count == len(result.observations),
                     "missing/incomplete normalized observations")
            for row in result.observations:
                _require(all(value is None or isinstance(value, Decimal) for value in
                             (getattr(row, name) for name in PRICES)), "typed Decimal observations required")
            try:
                _require(not validate_observations(result.observations, plan), "required OHLC missing")
            except DataQualityError as exc:
                raise WriterError("writer rejected normalized data quality") from exc
            expected = {day for day in histories[plan.security_code].observation_dates
                        if plan.request_start_date <= day <= plan.request_end_date}
            _require(expected <= {row.OBSERVATION_DATE for row in result.observations},
                     "normalized batch omitted existing overlap")
            selected = source_ids.get(result.provider)
            _require(selected in records, "explicit existing SOURCE_ID mapping required")
            entry = records[selected]
            yahoo_name = "YAHOO" in re.split(r"[^A-Z0-9]+", selected.upper() + " " + entry.get("SOURCE_NAME", "").upper())
            host = urlparse(entry.get("SOURCE_URL", "")).hostname or ""
            yahoo_url = host == "yahoo.com" or host.endswith(".yahoo.com")
            _require(result.provider == "Yahoo Chart API" and (yahoo_name or yahoo_url or
                     (synthetic and selected == "SYNTHETIC")), "SOURCE_ID does not identify the acquired provider")
            source_mapping[result.provider] = entry
            for row in result.observations:
                _require(row.RETRIEVED_AT is not None and row.SOURCE == result.provider and
                         row.RETRIEVED_AT == result.retrieved_at and row.RETRIEVED_AT.tzinfo is not None and
                         row.RETRIEVED_AT.utcoffset() is not None, "normalized retrieval lineage mismatch")
                updates[(row.SECURITY_CODE, row.OBSERVATION_DATE)] = (row, source_ids[result.provider])
        del histories  # date indexes are not retained during historical row materialization
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".r01-writer-", dir=output_path.parent) as scratch:
        scratch = Path(scratch)
        db = sqlite3.connect(scratch / "keys.sqlite")
        try:
            for table in ("input_keys", "output_keys"):
                db.execute(f"CREATE TABLE {table} (code TEXT, day TEXT, PRIMARY KEY (code, day)) WITHOUT ROWID")
            matched, unaffected_before = set(), hashlib.sha256()
            templates = {}
            counts = {code: {"replaced": 0, "inserted": 0} for code in seen_codes}
            root = None
            max_row, input_count = 0, 0
            with ZipFile(input_path) as archive:
                _require(len(archive.namelist()) == len(set(archive.namelist())), "duplicate ZIP member names")
                iterator = _rows(archive, member)
                header_row, _, namespaces = next(iterator)
                _register_namespaces(namespaces)
                columns, date_name = _columns(_values(header_row, shared))
                _require(header_row.get("r") == "1", "stock_daily header must be row 1")
                max_row = 1
                for row, root, namespaces in iterator:
                    number = int(row.get("r", "0"))
                    _require(number > max_row, "stock_daily row numbers must increase")
                    max_row = number
                    values = _values(row, shared, allow_formulas=not updates)
                    key = _key(values, columns, date_name, date_1904)
                    _unique(db, "input_keys", key)
                    input_count += 1
                    if key[0] in seen_codes and (key[0] not in templates or key[1] > templates[key[0]][0]):
                        templates[key[0]] = (key[1], deepcopy(row))
                    if key in updates:
                        matched.add(key)
                        counts[key[0]]["replaced"] += 1
                    else:
                        _fingerprint(unaffected_before, row)
                _require(root is not None, "stock_daily historical rows required")
                for tag in ("tableParts", "mergeCells", "extLst"):
                    _require(not updates or root.find(f"{{{MAIN}}}{tag}") is None,
                             f"stock_daily {tag} unsupported by selective MVP")
                inserted = sorted(set(updates) - matched)
                for key in inserted:
                    counts[key[0]]["inserted"] += 1
                staged = scratch / "output.xlsx"
                if not updates:
                    shutil.copyfile(input_path, staged)  # byte-identical NO_DATA_CHANGE; no recompression
                else:
                    row_file = scratch / "rows.xml"
                    _register_namespaces(namespaces)
                    with row_file.open("wb") as stream:
                        for index, (row, _, _) in enumerate(_rows(archive, member)):
                            if index:
                                key = _key(_values(row, shared), columns, date_name, date_1904)
                                if key in updates:
                                    row = _materialize(row, updates[key][0], columns, date_name, updates[key][1])
                            stream.write(ET.tostring(row, encoding="utf-8"))
                        for offset, key in enumerate(inserted, 1):
                            template = templates[key[0]][1]
                            row = _new_row(template, max_row + offset)
                            row = _materialize(row, updates[key][0], columns, date_name, updates[key][1],
                                               new=True, template=template, date_1904=date_1904)
                            stream.write(ET.tostring(row, encoding="utf-8"))
                    last_col = max(columns.values(), key=lambda col: (len(col), col))
                    dimension = root.find(f"{{{MAIN}}}dimension")
                    if dimension is not None:
                        bounds = dimension.get("ref", "A1").split(":")
                        end = re.fullmatch(r"([A-Z]+)([1-9][0-9]*)", bounds[-1])
                        _require(end is not None, "invalid stock_daily dimension")
                        end_col = max((last_col, end[1]), key=lambda col: (len(col), col))
                        end_row = max(max_row + len(inserted), int(end[2]))
                        dimension.set("ref", f"{bounds[0]}:{end_col}{end_row}")
                    auto_filter = root.find(f"{{{MAIN}}}autoFilter")
                    if auto_filter is not None and inserted:
                        bounds = re.fullmatch(r"([A-Z]+[1-9][0-9]*:)([A-Z]+)([1-9][0-9]*)", auto_filter.get("ref", ""))
                        _require(bounds is not None, "unsupported stock_daily autoFilter range")
                        if int(bounds[3]) == max_row:
                            auto_filter.set("ref", f"{bounds[1]}{bounds[2]}{max_row + len(inserted)}")
                    skeleton = ET.tostring(root, encoding="utf-8")
                    root.find(f"{{{MAIN}}}sheetData").text = None
                    # Retain namespace bindings used by compatibility attribute values.
                    for prefix, uri in namespaces.items():
                        attr = "xmlns" + (":" + prefix if prefix else "")
                        if not re.search(rb'\b' + attr.encode() + rb'=', skeleton):
                            root.set(attr, uri)
                    skeleton = ET.tostring(root, encoding="utf-8", xml_declaration=True)
                    marker = re.search(rb'<(?:[\w.-]+:)?sheetData\s*/>', skeleton)
                    _require(marker is not None, "empty stock_daily skeleton not found")
                    empty = marker[0].decode()
                    name = empty[1:].split()[0]
                    with ZipFile(staged, "w") as target:
                        target.comment = archive.comment
                        for info in archive.infolist():
                            with target.open(copy(info), "w") as destination:
                                if info.filename != member:
                                    with archive.open(info) as source:
                                        shutil.copyfileobj(source, destination, length=1024 * 1024)
                                else:
                                    destination.write(skeleton[:marker.start()] + f"<{name}>".encode())
                                    with row_file.open("rb") as source:
                                        shutil.copyfileobj(source, destination, length=1024 * 1024)
                                    destination.write(f"</{name}>".encode() + skeleton[marker.end():])
            # Reopen through the existing Reader and verify all untouched ZIP components.
            with R01PackageReader(staged) as reopened:
                _require(set(reopened.sheet_paths) == REQUIRED_SHEETS, "output eight-sheet integrity failure")
                reopened.read_stock_daily_index()
            unaffected_after, output_count, written = hashlib.sha256(), 0, set()
            with ZipFile(input_path) as old, ZipFile(staged) as new:
                _require(old.namelist() == new.namelist(), "output ZIP membership changed")
                for name in old.namelist():
                    if name != member:
                        with old.open(name) as a, new.open(name) as b:
                            while chunk := a.read(1024 * 1024):
                                _require(chunk == b.read(len(chunk)), "unaffected ZIP component changed")
                            _require(not b.read(1), "unaffected ZIP component grew")
                for index, (row, _, _) in enumerate(_rows(new, member)):
                    values = _values(row, shared, allow_formulas=not updates)
                    if not index:
                        _require(_columns(values) == (columns, date_name), "output schema changed")
                        continue
                    key = _key(values, columns, date_name, date_1904)
                    _unique(db, "output_keys", key)
                    output_count += 1
                    if key in updates:
                        observation, source_id = updates[key]
                        for field in PRICES:
                            raw, expected = values.get(columns[field], ""), getattr(observation, field)
                            _require((not raw if expected is None else Decimal(raw) == expected),
                                     "updated row does not match normalized acquisition")
                        for field, expected in {"SOURCE_ID": source_id, "ACQUISITION_METHOD": observation.SOURCE,
                                                 "ACQUIRED_AT": observation.RETRIEVED_AT.isoformat()}.items():
                            _require(values.get(columns[field]) == expected, "updated row lineage mismatch")
                        written.add(key)
                    else:
                        _fingerprint(unaffected_after, row)
            _require(written == set(updates) and output_count == input_count + len(set(updates) - matched),
                     "output row count/key integrity failure")
            _require(unaffected_before.digest() == unaffected_after.digest(), "unaffected historical row changed")
            _require(file_sha256(input_path) == before, "input workbook changed during materialization")
            report = {"development_only": True, "production_approved": False,
                      "weekly_update_ready": False,
                      "generator_version": __version__, "writer_version": "0.3-dev",
                      "writer_config_hash": canonical_hash({"source_ids": source_ids,
                          "generator_config_hash": config.sha256, "writer_version": "0.3-dev"}),
                      "run_evidence": run_evidence or {},
                      "source_mapping": source_mapping,
                      "quality_summary": {"status": "STALE" if updates else "UNCHANGED",
                          "recalculated": False, "affected_securities": sorted({key[0] for key in updates})},
                      "input_sha256": before, "input_sha256_after": file_sha256(input_path),
                      "output_sha256": file_sha256(staged), "output_path": str(output_path),
                      "input_row_count": input_count, "output_row_count": output_count,
                      "updated_securities": counts, "preserved_components": len(old.namelist()) - (1 if updates else 0),
                      "supported_modes": ["OVERLAP", "NO_DATA_CHANGE"],
                      "unsupported_modes": ["BACKFILL", "FULL_REFRESH"],
                      "warnings": ["Formal quality_summary and derived business fields are not recalculated"],
                      "acquisition_results": [result.evidence() for result in results],
                      "plan_hash": canonical_hash([asdict(result.plan) for result in results])}
            temporary_report = scratch / "report.json"
            temporary_report.write_text(json.dumps(report, default=json_value, indent=2, sort_keys=True) + "\n")
            os.link(temporary_report, report_path)  # exclusive publication; never overwrite prior outputs
            try:
                os.link(staged, output_path)
            except BaseException:
                report_path.unlink()
                raise
            return report
        finally:
            db.close()
