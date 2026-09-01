from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
HYPERLINK_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
ET.register_namespace("", NS_MAIN)
ET.register_namespace("r", NS_REL)


def anchor(sheet: str, row: dict, keys: list[str]) -> str:
    canonical = json.dumps([sheet, *(row.get(key) for key in keys)], ensure_ascii=False, separators=(",", ":"))
    return "row-" + hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:16]


def column_name(index: int) -> str:
    result = ""
    value = index + 1
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def relationship_path(sheet_path: str) -> str:
    part = PurePosixPath(sheet_path)
    return str(part.parent / "_rels" / f"{part.name}.rels")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workbook", type=Path, required=True)
    parser.add_argument("--extracted", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    args = parser.parse_args()

    extracted = json.loads(args.extracted.read_text(encoding="utf-8"))
    schema = json.loads(args.schema.read_text(encoding="utf-8"))
    with zipfile.ZipFile(args.workbook, "r") as source:
        entries = {name: source.read(name) for name in source.namelist()}

    workbook_xml = ET.fromstring(entries["xl/workbook.xml"])
    workbook_rels = ET.fromstring(entries["xl/_rels/workbook.xml.rels"])
    rel_targets = {rel.attrib["Id"]: rel.attrib["Target"] for rel in workbook_rels}
    sheet_paths: dict[str, str] = {}
    for sheet in workbook_xml.find(f"{{{NS_MAIN}}}sheets"):
        rel_id = sheet.attrib[f"{{{NS_REL}}}id"]
        target = rel_targets[rel_id].lstrip("/")
        sheet_paths[sheet.attrib["name"]] = target if target.startswith("xl/") else f"xl/{target}"

    link_count = 0
    for sheet_name, config in schema["sheets"].items():
        sheet_path = sheet_paths[sheet_name]
        root = ET.fromstring(entries[sheet_path])
        rel_path = relationship_path(sheet_path)
        rel_root = ET.fromstring(entries[rel_path]) if rel_path in entries else ET.Element(f"{{{NS_PKG}}}Relationships")
        existing_ids = {rel.attrib.get("Id", "") for rel in rel_root}
        next_id = 1

        old_links = root.find(f"{{{NS_MAIN}}}hyperlinks")
        if old_links is not None:
            root.remove(old_links)
        for rel in list(rel_root):
            if rel.attrib.get("Type") == HYPERLINK_REL:
                rel_root.remove(rel)

        rows = extracted.get("data", {}).get(sheet_name, [])
        rows = sorted(rows, key=lambda row: "|".join(str(row.get(field) or "") for field in config["key"]))
        link_column = config["fields"].index(config["evidence_link_field"])
        hyperlinks = ET.Element(f"{{{NS_MAIN}}}hyperlinks")
        while f"rId{next_id}" in existing_ids:
            next_id += 1
        rel_id = f"rId{next_id}"
        ET.SubElement(rel_root, f"{{{NS_PKG}}}Relationship", {
            "Id": rel_id,
            "Type": HYPERLINK_REL,
            "Target": "evidence/index.html",
            "TargetMode": "External",
        })
        for row_index, row in enumerate(rows, start=2):
            ET.SubElement(hyperlinks, f"{{{NS_MAIN}}}hyperlink", {
                "ref": f"{column_name(link_column)}{row_index}",
                f"{{{NS_REL}}}id": rel_id,
                "location": anchor(sheet_name, row, config["key"]),
            })
            link_count += 1

        trailing = {"printOptions", "pageMargins", "pageSetup", "headerFooter", "rowBreaks", "colBreaks", "customProperties", "cellWatches", "ignoredErrors", "smartTags", "drawing", "legacyDrawing", "legacyDrawingHF", "picture", "oleObjects", "controls", "webPublishItems", "tableParts", "extLst"}
        insert_at = len(root)
        for index, child in enumerate(root):
            if child.tag.rsplit("}", 1)[-1] in trailing:
                insert_at = index
                break
        root.insert(insert_at, hyperlinks)
        entries[sheet_path] = ET.tostring(root, encoding="utf-8", xml_declaration=True)
        entries[rel_path] = ET.tostring(rel_root, encoding="utf-8", xml_declaration=True)

    temp_fd, temp_name = tempfile.mkstemp(suffix=".xlsx", dir=args.workbook.parent)
    os.close(temp_fd)
    try:
        with zipfile.ZipFile(temp_name, "w", zipfile.ZIP_DEFLATED) as target:
            for name, content in entries.items():
                target.writestr(name, content)
        shutil.move(temp_name, args.workbook)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    print(json.dumps({"workbook": str(args.workbook), "hyperlinks": link_count}, ensure_ascii=False))


if __name__ == "__main__":
    main()
