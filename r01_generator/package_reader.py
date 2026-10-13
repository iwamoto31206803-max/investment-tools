from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
from pathlib import Path
import posixpath
import xml.etree.ElementTree as ET
import zipfile

from .validation import PackageValidationError

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
REQUIRED_SHEETS = frozenset({"metadata", "source_registry", "stock_master", "universe",
                             "stock_daily", "quality_summary", "acquisition_log",
                             "corporate_actions"})


@dataclass(frozen=True)
class ComponentInfo:
    path: str
    uncompressed_size: int
    compressed_size: int
    sha256: str


@dataclass(frozen=True)
class SecurityHistory:
    security_code: str
    observation_dates: tuple[date, ...]

    @property
    def observation_count(self) -> int:
        return len(self.observation_dates)

    @property
    def latest_observation_date(self) -> date | None:
        return self.observation_dates[-1] if self.observation_dates else None


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class R01PackageReader:
    """Read an XLSX as a ZIP/XML package, never as a workbook object."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        try:
            self._zip = zipfile.ZipFile(self.path, "r")
            if self._zip.testzip() is not None:
                raise PackageValidationError("corrupt ZIP member")
            self.sheet_paths = self._resolve_sheet_paths()
            missing = REQUIRED_SHEETS - self.sheet_paths.keys()
            if missing:
                raise PackageValidationError(f"missing required sheets: {sorted(missing)}")
        except (OSError, zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
            raise PackageValidationError(f"invalid xlsx package: {exc}") from exc

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        self._zip.close()

    def _safe_member(self, name: str) -> str:
        normalized = posixpath.normpath(name).lstrip("/")
        if normalized.startswith("../") or normalized not in self._zip.namelist():
            raise PackageValidationError(f"unsafe or missing ZIP component: {name}")
        return normalized

    def _resolve_sheet_paths(self) -> dict[str, str]:
        workbook = ET.parse(self._zip.open("xl/workbook.xml")).getroot()
        rels = ET.parse(self._zip.open("xl/_rels/workbook.xml.rels")).getroot()
        targets = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}
        result = {}
        for sheet in workbook.findall(f".//{{{MAIN}}}sheet"):
            rid = sheet.attrib.get(f"{{{DOC_REL}}}id")
            if rid not in targets:
                raise PackageValidationError(f"sheet relationship missing: {sheet.attrib['name']}")
            target = targets[rid]
            path = target.lstrip("/") if target.startswith("/") else posixpath.join("xl", target)
            result[sheet.attrib["name"]] = self._safe_member(path)
        return result

    def component_inventory(self) -> tuple[ComponentInfo, ...]:
        inventory = []
        for info in self._zip.infolist():
            digest = hashlib.sha256()
            with self._zip.open(info) as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            inventory.append(ComponentInfo(info.filename, info.file_size, info.compress_size,
                                           digest.hexdigest()))
        return tuple(inventory)

    def _shared_strings(self) -> list[str]:
        if "xl/sharedStrings.xml" not in self._zip.namelist():
            return []
        values = []
        with self._zip.open("xl/sharedStrings.xml") as stream:
            for _, elem in ET.iterparse(stream, events=("end",)):
                if elem.tag == f"{{{MAIN}}}si":
                    values.append("".join(t.text or "" for t in elem.iter(f"{{{MAIN}}}t")))
                    elem.clear()
        return values

    def _rows(self, sheet_name: str):
        shared = self._shared_strings()
        with self._zip.open(self.sheet_paths[sheet_name]) as stream:
            for _, elem in ET.iterparse(stream, events=("end",)):
                if elem.tag != f"{{{MAIN}}}row":
                    continue
                row = {}
                for cell in elem.findall(f"{{{MAIN}}}c"):
                    ref = cell.attrib.get("r", "")
                    col = "".join(c for c in ref if c.isalpha())
                    value = cell.find(f"{{{MAIN}}}v")
                    inline = cell.find(f".//{{{MAIN}}}t")
                    raw = value.text if value is not None else (inline.text if inline is not None else "")
                    if cell.attrib.get("t") == "s" and raw:
                        raw = shared[int(raw)]
                    row[col] = raw or ""
                elem.clear()
                yield row

    def read_metadata(self) -> dict[str, str]:
        result = {}
        for row in self._rows("metadata"):
            if row.get("A"):
                result[row["A"]] = row.get("B", "")
        return result

    @staticmethod
    def _excel_date(value: str) -> date:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                return date(1899, 12, 30) + timedelta(days=float(value))
            except (ValueError, OverflowError) as exc:
                raise PackageValidationError(f"invalid observation date: {value!r}") from exc

    def read_stock_daily_index(self) -> dict[str, SecurityHistory]:
        rows = self._rows("stock_daily")
        try:
            header = next(rows)
        except StopIteration:
            raise PackageValidationError("stock_daily is empty")
        names = {value.strip().upper(): col for col, value in header.items()}
        try:
            code_col = names["SECURITY_CODE"]
            date_col = next(names[n] for n in ("OBSERVATION_DATE", "DATE", "PRICE_DATE") if n in names)
        except (KeyError, StopIteration) as exc:
            raise PackageValidationError("stock_daily requires SECURITY_CODE and date columns") from exc
        dates: dict[str, set[date]] = defaultdict(set)
        for row in rows:
            code, raw_date = row.get(code_col, "").strip(), row.get(date_col, "").strip()
            if code and raw_date:
                dates[code].add(self._excel_date(raw_date))
        return {code: SecurityHistory(code, tuple(sorted(values))) for code, values in dates.items()}
