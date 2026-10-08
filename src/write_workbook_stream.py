from __future__ import annotations

import argparse
import json
import math
import os
import re
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr


INVALID_XML = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")


def column_name(index: int) -> str:
    value = index + 1
    result = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def xml_text(value: object) -> str:
    return escape(INVALID_XML.sub("", str(value)))


def excel_date_serial(value: object) -> float | None:
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})", str(value or ""))
    if not match:
        return None
    moment = datetime(*(int(part) for part in match.groups()), tzinfo=timezone.utc)
    epoch = datetime(1899, 12, 30, tzinfo=timezone.utc)
    return (moment - epoch).total_seconds() / 86400


def public_value(field: str, value: object) -> object:
    if value is None:
        return None
    if field == "ingested_at":
        return excel_date_serial(value)
    # PostgreSQL json/jsonb values are represented as native lists/dicts by the
    # database mapping layer.  Excel must receive valid JSON text rather than
    # Python's repr (which uses single quotes and cannot be imported as JSON).
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


def cell_xml(reference: str, value: object, style: int = 0) -> str:
    style_attr = f' s="{style}"' if style else ""
    if value is None:
        return f'<c r="{reference}"{style_attr}/>' if style else ""
    if isinstance(value, bool):
        return f'<c r="{reference}" t="b"{style_attr}><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return f'<c r="{reference}"{style_attr}><v>{value}</v></c>'
    text = xml_text(value)
    preserve = ' xml:space="preserve"' if text[:1].isspace() or text[-1:].isspace() else ""
    return f'<c r="{reference}" t="inlineStr"{style_attr}><is><t{preserve}>{text}</t></is></c>'


def range_groups(rows: list[int]) -> list[str]:
    groups: list[tuple[int, int]] = []
    for row in sorted(set(rows)):
        if not groups or row > groups[-1][1] + 1:
            groups.append((row, row))
        else:
            groups[-1] = (groups[-1][0], row)
    return [str(start) if start == end else f"{start}:{end}" for start, end in groups]


def english_sort_text(value: object) -> str:
    """Approximate JS localeCompare('en') for normalized SSOT identifiers.

    The legacy writer's English collation places the underscore separator
    before a following digit (ADC_VREF before ADC0_8, TIM1_CH1 before
    TIM16_CH1). SSOT identifiers are ASCII after normalization, so mapping the
    separator to a space reproduces that stable ordering without locale state.
    """
    return str(value or "").casefold().replace("_", " ")


def write_sheet(archive, sheet_index, sheet_name, fields, rows, row_map) -> None:
    widths = {
        "chip_id": 18, "series": 12, "core": 12, "package_types": 22, "package_type": 18, "pin_name": 14,
        "function_name": 20, "af_code": 14, "default_function": 28, "constraints": 28,
        "peripheral_type": 18, "features": 28, "param_name": 18, "unit": 10,
        "conditions": 28, "memory_type": 18, "organization": 24, "max_frequency_mhz": 22,
        "pdf_source_file": 42, "evidence_pages": 24, "ingested_by": 16, "ingested_at": 22,
        "is_reviewed": 14, "updated_at": 22, "source_doc_name": 34, "source_page": 16,
        "submitter": 18, "reviewer": 18, "audit_status": 14,
    }
    last_col = column_name(len(fields) - 1)
    last_row = len(rows) + 1
    with archive.open(f"xl/worksheets/sheet{sheet_index}.xml", "w") as output:
        def emit(text: str) -> None:
            output.write(text.encode("utf-8"))

        emit('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>')
        emit('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">')
        emit(f'<dimension ref="A1:{last_col}{last_row}"/>')
        emit('<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>')
        emit('<sheetFormatPr defaultRowHeight="15"/><cols>')
        for index, field in enumerate(fields, 1):
            emit(f'<col min="{index}" max="{index}" width="{widths.get(field, 14)}" customWidth="1"/>')
        emit('</cols><sheetData>')
        header_cells = "".join(cell_xml(f"{column_name(index)}1", field, 1) for index, field in enumerate(fields))
        emit(f'<row r="1" ht="20" customHeight="1">{header_cells}</row>')
        date_column = fields.index("ingested_at") if "ingested_at" in fields else -1
        for row_number, row in enumerate(rows, 2):
            stripe_style = 3 if row_number % 2 == 0 else 0
            cells = "".join(
                cell_xml(
                    f"{column_name(column)}{row_number}",
                    public_value(field, row.get(field)),
                    (4 if stripe_style else 2)
                    if column == date_column and row.get(field) is not None
                    else stripe_style,
                )
                for column, field in enumerate(fields)
            )
            emit(f'<row r="{row_number}">{cells}</row>')
            for evidence_id in row.get("_evidence_ids", []):
                row_map[evidence_id][sheet_name].append(row_number)
        emit('</sheetData>')
        emit('<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>')
        emit('<tableParts count="1"><tablePart r:id="rId1"/></tableParts></worksheet>')


def static_parts(sheet_names: list[str], *, include_theme: bool = False) -> dict[str, str]:
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, len(sheet_names) + 1)
    )
    table_overrides = "".join(
        f'<Override PartName="/xl/tables/table{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml"/>'
        for index in range(1, len(sheet_names) + 1)
    )
    sheets = "".join(
        f'<sheet name={quoteattr(name)} sheetId="{index}" r:id="rId{index}"/>'
        for index, name in enumerate(sheet_names, 1)
    )
    relationships = "".join(
        f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, len(sheet_names) + 1)
    )
    style_id = len(sheet_names) + 1
    theme_override = (
        '<Override PartName="/xl/theme/theme1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>'
        if include_theme else ""
    )
    theme_relationship = (
        f'<Relationship Id="rId{style_id + 1}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" '
        'Target="theme/theme1.xml"/>'
        if include_theme else ""
    )
    return {
        "[Content_Types].xml": f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>{theme_override}{overrides}{table_overrides}<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/><Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/></Types>''',
        "_rels/.rels": '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/></Relationships>''',
        "xl/workbook.xml": f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><bookViews><workbookView/></bookViews><sheets>{sheets}</sheets><calcPr calcId="191029"/></workbook>''',
        "xl/_rels/workbook.xml.rels": f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{relationships}<Relationship Id="rId{style_id}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>{theme_relationship}</Relationships>''',
        "xl/styles.xml": '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy-mm-dd hh:mm:ss"/></numFmts><fonts count="2"><font><sz val="10"/><name val="Calibri"/><family val="2"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="10"/><name val="Calibri"/><family val="2"/></font></fonts><fills count="4"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/><bgColor indexed="64"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFD9E7F5"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="5"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf><xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/><xf numFmtId="0" fontId="0" fillId="3" borderId="0" xfId="0" applyFill="1"/><xf numFmtId="164" fontId="0" fillId="3" borderId="0" xfId="0" applyNumberFormat="1" applyFill="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles><dxfs count="0"/><tableStyles count="0" defaultPivotStyle="PivotStyleLight16"/></styleSheet>''',
        "docProps/core.xml": f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"><dc:creator>SSOT</dc:creator><cp:lastModifiedBy>SSOT</cp:lastModifiedBy><dcterms:created xsi:type="dcterms:W3CDTF">{created_at}</dcterms:created></cp:coreProperties>''',
        "docProps/app.xml": f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"><Application>SSOT</Application><TitlesOfParts><vt:vector size="{len(sheet_names)}" baseType="lpstr">{"".join(f"<vt:lpstr>{xml_text(name)}</vt:lpstr>" for name in sheet_names)}</vt:vector></TitlesOfParts></Properties>''',
    }


def table_parts(sheet_index: int, sheet_name: str, fields: list[str], row_count: int) -> dict[str, str]:
    last_col = column_name(len(fields) - 1)
    last_row = max(1, row_count + 1)
    reference = f"A1:{last_col}{last_row}"
    table_name = re.sub(r"[^A-Za-z0-9_]", "_", sheet_name) + "Table"
    columns = "".join(
        f'<tableColumn id="{index}" name={quoteattr(field)}/>'
        for index, field in enumerate(fields, 1)
    )
    return {
        f"xl/tables/table{sheet_index}.xml": f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><table xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" id="{sheet_index}" name={quoteattr(table_name)} displayName={quoteattr(table_name)} ref="{reference}" headerRowCount="1"><autoFilter ref="{reference}"/><tableColumns count="{len(fields)}">{columns}</tableColumns></table>''',
        f"xl/worksheets/_rels/sheet{sheet_index}.xml.rels": f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/table" Target="../tables/table{sheet_index}.xml"/></Relationships>''',
    }


def peak_rss_mb() -> int | None:
    try:
        import psutil
        info = psutil.Process().memory_info()
        value = getattr(info, "peak_wset", None) or getattr(info, "rss", None)
        return round(value / 1024 / 1024) if value else None
    except Exception:
        if os.name != "nt":
            return None
        try:
            import ctypes
            from ctypes import wintypes

            class ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            get_current_process = ctypes.windll.kernel32.GetCurrentProcess
            get_current_process.restype = wintypes.HANDLE
            get_process_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
            get_process_memory_info.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
            get_process_memory_info.restype = wintypes.BOOL
            handle = get_current_process()
            if get_process_memory_info(handle, ctypes.byref(counters), counters.cb):
                return round(counters.PeakWorkingSetSize / 1024 / 1024)
        except Exception:
            pass
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Stream SSOT data to XLSX without retaining worksheet cells in memory.")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--template", required=True, type=Path)
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--row-map", required=True, type=Path)
    parser.add_argument("--chunk-rows", type=int, default=5000)
    args = parser.parse_args()

    with args.input.open(encoding="utf-8") as handle:
        extracted = json.load(handle)
    with args.schema.open(encoding="utf-8") as handle:
        schema = json.load(handle)
    sheet_names = list(schema["sheets"])
    row_map = defaultdict(lambda: defaultdict(list))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    theme = None
    with zipfile.ZipFile(args.template) as template_archive:
        if "xl/theme/theme1.xml" in template_archive.namelist():
            theme = template_archive.read("xl/theme/theme1.xml")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
        for name, content in static_parts(sheet_names, include_theme=theme is not None).items():
            archive.writestr(name, content)
        if theme is not None:
            archive.writestr("xl/theme/theme1.xml", theme)
        for index, sheet_name in enumerate(sheet_names, 1):
            config = schema["sheets"][sheet_name]
            rows = list(extracted.get("data", {}).get(sheet_name, []))
            rows.sort(key=lambda row: tuple(english_sort_text(row.get(field)) for field in config["key"]))
            write_sheet(archive, index, sheet_name, config["fields"], rows, row_map)
            for name, content in table_parts(index, sheet_name, config["fields"], len(rows)).items():
                archive.writestr(name, content)
    os.replace(temporary, args.output)

    compact_map = {
        evidence_id: {
            sheet_name: {"count": len(rows), "ranges": range_groups(rows)}
            for sheet_name, rows in sheets.items()
        }
        for evidence_id, sheets in row_map.items()
    }
    args.row_map.write_text(
        json.dumps({"row_map": compact_map, "write_conflicts": []}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({
        "output": str(args.output), "sheets": len(sheet_names), "formulasWritten": 0,
        "writeConflicts": 0, "writer": "streaming-ooxml", "peakRssMb": peak_rss_mb(),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
