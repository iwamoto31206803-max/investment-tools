from __future__ import annotations

from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
from pathlib import Path
import posixpath
import sqlite3
import tempfile
import math
import xml.etree.ElementTree as ET
import zipfile

from .validation import PackageValidationError

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
SHARED_STRINGS_DISK_THRESHOLD = 4 * 1024 * 1024
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
REQUIRED_SHEETS = frozenset({"metadata", "source_registry", "stock_master", "universe",
                             "stock_daily", "quality_summary", "acquisition_log",
                             "corporate_actions"})


def xml_text(element) -> str:
    """OOXML plain/rich text, excluding phonetic annotations."""
    if element is None:
        return ""
    parts = []
    for child in element:
        if child.tag == f"{{{MAIN}}}t":
            parts.append(child.text or "")
        elif child.tag == f"{{{MAIN}}}r":
            parts.extend(node.text or "" for node in child.findall(f"{{{MAIN}}}t"))
    return "".join(parts)


class _DiskStrings:
    """Large shared-string tables stay on local disk, with a bounded hot cache."""

    def __init__(self):
        self.directory = tempfile.TemporaryDirectory(prefix="r01-strings-")
        self.db = sqlite3.connect(Path(self.directory.name) / "strings.sqlite")
        self.db.execute("CREATE TABLE strings (id INTEGER PRIMARY KEY, value TEXT)")
        self.cache = OrderedDict()
        self.count = 0

    def append(self, value):
        self.db.execute("INSERT INTO strings VALUES (?, ?)", (self.count, value))
        self.count += 1

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        if not 0 <= index < self.count:
            raise PackageValidationError("shared-string index out of bounds")
        if index not in self.cache:
            self.cache[index] = self.db.execute("SELECT value FROM strings WHERE id=?", (index,)).fetchone()[0]
            if len(self.cache) > 4096:
                self.cache.popitem(last=False)
        self.cache.move_to_end(index)
        return self.cache[index]

    def __del__(self):
        if hasattr(self, "db"):
            self.db.close()
        if hasattr(self, "directory"):
            self.directory.cleanup()


def cell_value(cell, shared):
    kind = cell.get("t", "n")
    value = cell.find(f"{{{MAIN}}}v")
    raw = (value.text or "") if value is not None else xml_text(cell.find(f"{{{MAIN}}}is"))
    if kind == "s":
        try:
            index = int(raw)
            if not 0 <= index < len(shared):
                raise ValueError
            return shared[index]
        except (ValueError, TypeError, IndexError) as exc:
            raise PackageValidationError("invalid shared-string index") from exc
    return raw


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
        self._shared_cache = None
        self.date_1904 = False
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
        # A writer may still borrow the string index; its last owner releases the disk cache.
        self._shared_cache = None

    def _safe_member(self, name: str) -> str:
        normalized = posixpath.normpath(name).lstrip("/")
        if normalized.startswith("../") or normalized not in self._zip.namelist():
            raise PackageValidationError(f"unsafe or missing ZIP component: {name}")
        return normalized

    def _resolve_sheet_paths(self) -> dict[str, str]:
        workbook = ET.parse(self._zip.open("xl/workbook.xml")).getroot()
        properties = workbook.find(f"{{{MAIN}}}workbookPr")
        epoch_flag = properties.get("date1904", "0") if properties is not None else "0"
        if epoch_flag not in {"0", "1", "false", "true"}:
            raise PackageValidationError("invalid workbook date1904 flag")
        self.date_1904 = epoch_flag in {"1", "true"}
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

    def _shared_strings(self):
        if self._shared_cache is not None:
            return self._shared_cache
        if "xl/sharedStrings.xml" not in self._zip.namelist():
            self._shared_cache = []
            return self._shared_cache
        info = self._zip.getinfo("xl/sharedStrings.xml")
        values = _DiskStrings() if info.file_size > SHARED_STRINGS_DISK_THRESHOLD else []
        with self._zip.open(info) as stream:
            root = None
            for event, elem in ET.iterparse(stream, events=("start", "end")):
                if root is None:
                    root = elem
                if event == "end" and elem.tag == f"{{{MAIN}}}si":
                    values.append(xml_text(elem))
                    root.remove(elem)
        self._shared_cache = values
        return values

    def _rows(self, sheet_name: str):
        shared = self._shared_strings()
        with self._zip.open(self.sheet_paths[sheet_name]) as stream:
            sheet_data = None
            for event, elem in ET.iterparse(stream, events=("start", "end")):
                if event == "start" and elem.tag == f"{{{MAIN}}}sheetData":
                    sheet_data = elem
                if event != "end" or elem.tag != f"{{{MAIN}}}row":
                    continue
                row = {}
                for cell in elem.findall(f"{{{MAIN}}}c"):
                    ref = cell.attrib.get("r", "")
                    col = "".join(c for c in ref if c.isalpha())
                    row[col] = cell_value(cell, shared)
                if sheet_data is not None:
                    sheet_data.remove(elem)
                yield row

    def read_metadata(self) -> dict[str, str]:
        result = {}
        for row in self._rows("metadata"):
            if row.get("A"):
                result[row["A"]] = row.get("B", "")
        return result

    @staticmethod
    def _excel_date(value: str, date_1904: bool = False) -> date:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            try:
                serial = float(value)
                if not math.isfinite(serial) or serial < 0 or (not date_1904 and int(serial) == 60):
                    raise ValueError("invalid Excel date")
                epoch = date(1904, 1, 1) if date_1904 else date(1899, 12, 30) if serial >= 61 else date(1899, 12, 31)
                return epoch + timedelta(days=serial)
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
                dates[code].add(self._excel_date(raw_date, self.date_1904))
        return {code: SecurityHistory(code, tuple(sorted(values))) for code, values in dates.items()}
