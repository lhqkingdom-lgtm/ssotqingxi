from __future__ import annotations

import argparse
import json
import re
import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
HYPERLINK_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"


def rel_path(sheet_path: str) -> str:
    part = PurePosixPath(sheet_path)
    return str(part.parent / "_rels" / f"{part.name}.rels")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workbook", type=Path, required=True)
    parser.add_argument("--schema", type=Path, required=True)
    parser.add_argument("--extracted", type=Path, required=True)
    parser.add_argument("--viewer", type=Path, required=True)
    args = parser.parse_args()
    schema = json.loads(args.schema.read_text(encoding="utf-8"))
    extracted = json.loads(args.extracted.read_text(encoding="utf-8"))
    expected = sum(len(extracted.get("data", {}).get(sheet, [])) for sheet in schema["sheets"])
    errors: list[dict] = []
    count = 0
    target_anchors: set[str] = set()
    with zipfile.ZipFile(args.workbook, "r") as archive:
        if archive.testzip() is not None:
            errors.append({"kind": "corrupt_zip"})
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        workbook_rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rel_targets = {rel.attrib["Id"]: rel.attrib["Target"] for rel in workbook_rels}
        sheet_paths = {}
        for sheet in workbook.find(f"{{{NS_MAIN}}}sheets"):
            target = rel_targets[sheet.attrib[f"{{{NS_REL}}}id"]].lstrip("/")
            sheet_paths[sheet.attrib["name"]] = target if target.startswith("xl/") else f"xl/{target}"
        for sheet_name, config in schema["sheets"].items():
            sheet_path = sheet_paths[sheet_name]
            sheet = ET.fromstring(archive.read(sheet_path))
            relationships = ET.fromstring(archive.read(rel_path(sheet_path)))
            targets = {rel.attrib["Id"]: rel.attrib.get("Target", "") for rel in relationships if rel.attrib.get("Type") == HYPERLINK_REL}
            allowed_column = config["fields"].index(config["evidence_link_field"])
            allowed_letters = ""
            value = allowed_column + 1
            while value:
                value, remainder = divmod(value - 1, 26)
                allowed_letters = chr(65 + remainder) + allowed_letters
            links = sheet.find(f"{{{NS_MAIN}}}hyperlinks")
            for link in list(links) if links is not None else []:
                cell = link.attrib.get("ref", "")
                target = targets.get(link.attrib.get(f"{{{NS_REL}}}id", ""), "")
                location = link.attrib.get("location", "")
                if not cell.startswith(allowed_letters) or target != "evidence/index.html" or not re.fullmatch(r"row-[0-9a-f]{16}", location):
                    errors.append({"sheet": sheet_name, "cell": cell, "target": target, "location": location})
                else:
                    target_anchors.add(location)
                count += 1
    if count != expected:
        errors.append({"kind": "link_count", "expected": expected, "actual": count})
    viewer_text = args.viewer.read_text(encoding="utf-8")
    missing_anchors = sorted(anchor for anchor in target_anchors if f'"_anchor":"{anchor}"' not in viewer_text)
    if missing_anchors:
        errors.append({"kind": "missing_viewer_anchors", "count": len(missing_anchors), "examples": missing_anchors[:10]})
    print(json.dumps({"hyperlinks": count, "errors": errors}, ensure_ascii=False))
    raise SystemExit(2 if errors else 0)


if __name__ == "__main__":
    main()
