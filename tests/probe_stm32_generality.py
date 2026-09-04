from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from core.pipeline import run_extraction  # noqa: E402


def source_name(value: object) -> str:
    return str(value or "").replace("\\", "/").rsplit("/", 1)[-1]


def compare_case(case: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    filename = case["file"]
    result: dict[str, Any] = {
        "file": filename,
        "series_group": case["series_group"],
        "topology": case["topology"],
        "status": "PASS",
        "problems": [],
    }
    extraction_error = next(
        (item for item in payload.get("errors", []) if source_name(item.get("source_file")) == filename),
        None,
    )
    if extraction_error:
        result["status"] = "FAIL"
        result["problems"].append({"kind": "parse_error", "detail": extraction_error["error"]})
        return result

    chip_id = case["expected_chip_id"]
    chips = [row for row in payload["data"]["chips"] if row.get("chip_id") == chip_id]
    if len(chips) != 1:
        result["status"] = "FAIL"
        result["problems"].append({"kind": "chip_row_count", "expected": 1, "actual": len(chips)})
        return result
    chip = chips[0]

    for field, expected in case.get("expected", {}).items():
        actual = chip.get(field)
        if actual != expected:
            result["problems"].append(
                {"kind": "field_mismatch", "field": field, "expected": expected, "actual": actual}
            )
    for field in case.get("required_nonempty", []):
        if chip.get(field) in (None, "", []):
            result["problems"].append({"kind": "blank_required_field", "field": field})

    actual_peripherals = {
        row["peripheral_type"]: row["instance_count"]
        for row in payload["data"]["peripherals"]
        if row.get("chip_id") == chip_id
    }
    for peripheral, expected in case.get("expected_peripherals", {}).items():
        actual = actual_peripherals.get(peripheral)
        if actual != expected:
            result["problems"].append(
                {
                    "kind": "peripheral_mismatch",
                    "peripheral": peripheral,
                    "expected": expected,
                    "actual": actual,
                }
            )
    if result["problems"]:
        result["status"] = "FAIL"
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe STM32 extraction across representative document topologies.")
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--cases", type=Path, default=ROOT / "tests" / "stm32_generality_cases.json")
    parser.add_argument("--profile", type=Path, default=ROOT / "config" / "profiles" / "stm32_v1.json")
    parser.add_argument("--schema", type=Path, default=ROOT / "config" / "schema.json")
    args = parser.parse_args()

    manifest = json.loads(args.cases.read_text(encoding="utf-8"))
    cases = manifest["cases"]
    missing = [case["file"] for case in cases if not (args.source_dir / case["file"]).is_file()]
    if missing:
        raise SystemExit(f"Missing STM32 probe PDFs: {missing}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="stm32-generality-") as temporary:
        input_dir = Path(temporary)
        for case in cases:
            shutil.copy2(args.source_dir / case["file"], input_dir / case["file"])
        payload = run_extraction(
            input_dir=input_dir,
            output_dir=args.output_dir,
            schema_path=args.schema,
            profile_paths=[args.profile],
            ingested_by="generality-probe",
            ingested_at="2000-01-01T00:00:00+00:00",
        )

    results = [compare_case(case, payload) for case in cases]
    passed = sum(item["status"] == "PASS" for item in results)
    report = {
        "version": manifest["version"],
        "cases": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "pass_rate": passed / len(results) if results else 0,
        "results": results,
    }
    report_path = args.output_dir / "generality_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
