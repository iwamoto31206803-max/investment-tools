"""Narrow, lossless range extension for a single plain stock_daily Excel Table."""
import posixpath
import re
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
from xml.parsers import expat

from .package_reader import MAIN, DOC_REL, PKG_REL


def extend_stock_table(archive, worksheet, root, columns, old_last_row, new_last_row, require):
    """Return (part name, original bytes, updated bytes), or None for no table.

    Only actual ref attribute bytes are replaced. All other table bytes, including
    namespace declarations, column definitions and table style, remain untouched.
    """
    parts = root.find(f"{{{MAIN}}}tableParts")
    if parts is None:
        return None
    require(parts.get("count") == "1" and len(parts) == 1 and
            parts[0].tag == f"{{{MAIN}}}tablePart", "only one stock_daily table is supported")
    relationship_id = parts[0].get(f"{{{DOC_REL}}}id")
    rel_part = posixpath.join(posixpath.dirname(worksheet), "_rels", posixpath.basename(worksheet) + ".rels")
    require(rel_part in archive.namelist(), "stock_daily table relationships missing")
    relationships = ET.fromstring(archive.read(rel_part))
    require(relationships.tag == f"{{{PKG_REL}}}Relationships", "invalid worksheet relationship root")
    ids = [rel.get("Id") for rel in relationships]
    require(all(ids) and len(ids) == len(set(ids)), "duplicate/missing worksheet relationship IDs")
    matching = [rel for rel in relationships if rel.get("Id") == relationship_id]
    require(len(matching) == 1, "stock_daily table relationship not found")
    rel = matching[0]
    require(rel.tag == f"{{{PKG_REL}}}Relationship" and rel.get("Type") == DOC_REL + "/table" and
            rel.get("TargetMode", "Internal") == "Internal", "external/unsupported table relationship")
    target = rel.get("Target", "")
    uri = urlsplit(target)
    require(target and not any((uri.scheme, uri.netloc, uri.query, uri.fragment)) and
            "\\" not in target and "%" not in target, "unsafe table relationship target")
    member = posixpath.normpath(target.lstrip("/") if target.startswith("/") else
                               posixpath.join(posixpath.dirname(worksheet), target))
    require(member.startswith("xl/") and member in archive.namelist() and member != worksheet,
            "missing/unsafe table part")
    require(archive.getinfo(member).file_size <= 1024 * 1024, "table definition exceeds supported size")
    original = archive.read(member)
    require(b"<!DOCTYPE" not in original, "table DTD unsupported")
    table = ET.fromstring(original)
    require(all(node.tag.startswith(f"{{{MAIN}}}") for node in table.iter()),
            "table extension elements unsupported")
    require(table.tag == f"{{{MAIN}}}table" and table.get("id", "").isdigit() and
            int(table.get("id")) > 0 and table.get("name") and table.get("displayName"),
            "invalid table identity")
    require(table.get("headerRowCount", "1") == "1" and table.get("totalsRowCount", "0") == "0" and
            table.get("tableType", "worksheet") == "worksheet" and "connectionId" not in table.attrib,
            "totals/query/XML tables unsupported")
    for tag in ("extLst", "sortState", "calculatedColumnFormula", "totalsRowFormula", "xmlColumnPr"):
        require(table.find(f".//{{{MAIN}}}{tag}") is None, f"table {tag} unsupported")
    require(all(child.tag in {f"{{{MAIN}}}{tag}" for tag in
                ("autoFilter", "tableColumns", "tableStyleInfo")} for child in table),
            "unsupported table structure")
    table_columns = table.find(f"{{{MAIN}}}tableColumns")
    require(len(table.findall(f"{{{MAIN}}}tableColumns")) == 1 and
            len(table.findall(f"{{{MAIN}}}tableStyleInfo")) <= 1, "duplicate table definition elements")
    ordered = sorted(columns, key=lambda name: (len(columns[name]), columns[name]))
    require(table_columns is not None and len(table_columns) == len(columns) and
            table_columns.get("count") == str(len(columns)), "table column count differs from worksheet")
    column_ids = [col.get("id") for col in table_columns]
    require(all(value and value.isdigit() and int(value) > 0 for value in column_ids) and
            len({int(value) for value in column_ids}) == len(column_ids), "invalid table column IDs")
    require(all(col.tag == f"{{{MAIN}}}tableColumn" for col in table_columns) and
            all(len(col) == 0 for col in table_columns) and
            [col.get("name", "").strip().upper() for col in table_columns] == ordered,
            "table columns differ from stock_daily headers")
    # Scope is a full-width, single-header table over every existing data row.
    for index, name in enumerate(ordered, 1):
        value, column = index, ""
        while value:
            value, remainder = divmod(value - 1, 26)
            column = chr(65 + remainder) + column
        require(columns[name] == column, "table requires contiguous stock_daily columns from A")
    last_column = columns[ordered[-1]]
    old_ref = f"A1:{last_column}{old_last_row}"
    new_ref = f"A1:{last_column}{new_last_row}"
    require(table.get("ref") == old_ref, "table range must cover all stock_daily rows")
    filters = table.findall(f"{{{MAIN}}}autoFilter")
    require(len(filters) <= 1 and all(node.get("ref") == old_ref for node in filters),
            "table autoFilter range mismatch")
    if old_ref == new_ref:
        return member, original, original
    # Expat provides offsets for real start tags, avoiding accidental edits to
    # comments, embedded attribute text, or similarly named nested elements.
    parser = expat.ParserCreate(namespace_separator="}")
    edits, depth = [], 0
    def start(name, attrs):
        nonlocal depth
        if depth == 0 or (depth == 1 and name == MAIN + "}autoFilter"):
            begin = parser.CurrentByteIndex
            tag = re.match(rb'''<(?:[^>"']|"[^"]*"|'[^']*')*>''', original[begin:])
            require(tag is not None, "unsupported table start tag")
            ref = re.search(rb'''(?<![\w:.-])ref\s*=\s*(["'])(.*?)\1''', tag[0])
            require(ref is not None and ref[2] == old_ref.encode("ascii"), "unsupported table ref encoding")
            edits.append((begin + ref.start(2), begin + ref.end(2)))
        depth += 1
    def end(name):
        nonlocal depth
        depth -= 1
    parser.StartElementHandler, parser.EndElementHandler = start, end
    parser.Parse(original, True)
    require(len(edits) == 1 + len(filters), "unexpected table range structure")
    updated = original
    for begin, end in reversed(edits):
        updated = updated[:begin] + new_ref.encode("ascii") + updated[end:]
    # Reparse the exact bytes that will be published.
    reopened = ET.fromstring(updated)
    require(reopened.get("ref") == new_ref and
            all(node.get("ref") == new_ref for node in reopened.findall(f"{{{MAIN}}}autoFilter")),
            "updated table range validation failed")
    return member, original, updated
