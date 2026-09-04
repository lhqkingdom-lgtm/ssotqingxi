from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from pypdf import PdfReader


ROOT = Path(__file__).resolve().parents[1]


EXCLUDED_NAME_PARTS = (
    "eval",
    "disco",
    "nucleo",
    "starter",
    "board",
    "-dk",
    "_dk",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def candidate_rank(path: Path, family: str) -> tuple[int, int, str]:
    stem = path.stem.upper()
    family_upper = family.upper()
    suffix = stem[len(family_upper) :] if stem.startswith(family_upper) else stem
    if re.fullmatch(r"_[A-Z][A-Z0-9]", suffix):
        shape = 0
    elif re.fullmatch(r"_[A-Z]", suffix):
        shape = 1
    elif re.fullmatch(r"_[A-Z][A-Z0-9].*", suffix):
        shape = 2
    else:
        shape = 3
    return shape, len(stem), stem


def select_samples(folder: Path, count: int) -> list[Path]:
    candidates = [
        path
        for path in folder.rglob("*.pdf")
        if not any(part in path.stem.casefold() for part in EXCLUDED_NAME_PARTS)
    ]
    candidates.sort(key=lambda path: candidate_rank(path, folder.name))
    selected: list[Path] = []
    seen_hashes: set[str] = set()
    for path in candidates:
        fingerprint = sha256(path)
        if fingerprint in seen_hashes:
            continue
        seen_hashes.add(fingerprint)
        selected.append(path)
        if len(selected) == count:
            break
    return selected


def source_name(value: object) -> str:
    return str(value or "").replace("\\", "/").rsplit("/", 1)[-1]


def summarize_payload(payload: dict[str, Any], sample: Path, elapsed: float, page_count: int) -> dict[str, Any]:
    errors = [
        item
        for item in payload.get("errors", [])
        if source_name(item.get("source_file")).casefold() == sample.name.casefold()
    ]
    document = next(
        (
            item
            for item in payload.get("documents", [])
            if source_name(item.get("source_file")).casefold() == sample.name.casefold()
        ),
        None,
    )
    if errors and page_count < 20 and all(
        "no matrix matched" in str(item.get("error")) or "document family is unknown" in str(item.get("error"))
        for item in errors
    ):
        status = "not_full_datasheet"
    elif document:
        status = str(document.get("status") or "failed")
    elif errors and all("document family is unknown" in str(item.get("error")) for item in errors):
        status = "not_datasheet_or_unsupported"
    else:
        status = "failed"
    data = payload.get("data", {})
    model_ids = [row.get("chip_id") for row in data.get("chips", []) if row.get("chip_id")]
    model_record_counts = {
        model: {
            sheet: sum(row.get("chip_id") == model for row in rows)
            for sheet, rows in data.items()
        }
        for model in model_ids
    }
    chip_rows = [
        {
            key: value
            for key, value in row.items()
            if not key.startswith("_")
            and key not in {"pdf_source_file", "evidence_pages", "ingested_by", "ingested_at", "is_reviewed"}
        }
        for row in data.get("chips", [])
    ]
    return {
        "series_folder": sample.parents[1].name if sample.parent.name.casefold() == "alldatasheet" else sample.parent.name,
        "file": sample.name,
        "source_path": str(sample),
        "sha256": sha256(sample),
        "status": status,
        "page_count": page_count,
        "elapsed_seconds": round(elapsed, 2),
        "models": list((document or {}).get("models", [])),
        "sections": dict((document or {}).get("sections", {})),
        "review_items": list((document or {}).get("review_items", [])),
        "records": {name: len(rows) for name, rows in data.items()},
        "model_record_counts": model_record_counts,
        "chip_rows": chip_rows,
        "hard_validation_issues": len(payload.get("validation_issues", [])),
        "conflicts": len(payload.get("conflicts", [])),
        "errors": [str(item.get("error")) for item in errors],
    }


def run_sample(sample: Path, profile: Path, schema: Path) -> dict[str, Any]:
    page_count = len(PdfReader(str(sample)).pages)
    with tempfile.TemporaryDirectory(prefix="ssot-folder-coverage-") as temporary:
        case_root = Path(temporary)
        input_dir = case_root / "input"
        output_dir = case_root / "output"
        input_dir.mkdir()
        output_dir.mkdir()
        shutil.copy2(sample, input_dir / sample.name)
        command = [
            sys.executable,
            str(ROOT / "src" / "extract.py"),
            "--input",
            str(input_dir),
            "--output-dir",
            str(output_dir),
            "--schema",
            str(schema),
            "--profile",
            str(profile),
            "--ingested-by",
            "folder-coverage",
            "--allow-partial",
        ]
        started = time.perf_counter()
        completed = subprocess.run(command, text=True, capture_output=True, encoding="utf-8", errors="replace")
        elapsed = time.perf_counter() - started
        extracted = output_dir / "extracted.json"
        if not extracted.is_file():
            return {
                "series_folder": sample.parent.parent.name,
                "file": sample.name,
                "source_path": str(sample),
                "sha256": sha256(sample),
                "status": "runner_failed",
                "page_count": page_count,
                "elapsed_seconds": round(elapsed, 2),
                "models": [],
                "sections": {},
                "review_items": [],
                "records": {},
                "hard_validation_issues": 0,
                "conflicts": 0,
                "errors": [completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"],
            }
        payload = json.loads(extracted.read_text(encoding="utf-8"))
        return summarize_payload(payload, sample, elapsed, page_count)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serially probe one or two PDFs from each STM32 series folder.")
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--samples-per-folder", type=int, choices=(1, 2), default=1)
    parser.add_argument("--folders", nargs="*", help="Only run these series folders.")
    parser.add_argument("--sample-path", action="append", type=Path, help="Run an explicit supplemental PDF.")
    parser.add_argument("--resume", type=Path, help="Merge rerun results into an existing report.")
    parser.add_argument("--profile", type=Path, default=ROOT / "config" / "profiles" / "stm32_v1.json")
    parser.add_argument("--schema", type=Path, default=ROOT / "config" / "schema.json")
    args = parser.parse_args()

    all_folders = [folder for folder in sorted(args.source_root.iterdir()) if folder.is_dir() and folder.name != "STM32"]
    requested = set(args.folders or ())
    folders = [folder for folder in all_folders if not requested or folder.name in requested]
    if args.sample_path:
        selections = [
            (sample.parent.parent if sample.parent.name.casefold() == "alldatasheet" else sample.parent, [sample])
            for sample in args.sample_path
        ]
        missing: list[str] = []
    else:
        selections = [(folder, select_samples(folder, args.samples_per_folder)) for folder in folders]
        missing = [folder.name for folder, samples in selections if len(samples) < args.samples_per_folder]
    results: list[dict[str, Any]] = []
    if args.resume and args.resume.is_file():
        results = list(json.loads(args.resume.read_text(encoding="utf-8")).get("results", []))
    total = sum(len(samples) for _, samples in selections)
    sequence = 0
    for folder, samples in selections:
        for sample in samples:
            sequence += 1
            print(f"[{sequence}/{total}] {folder.name}: {sample.name}", flush=True)
            result = run_sample(sample, args.profile.resolve(), args.schema.resolve())
            results = [
                item
                for item in results
                if not (
                    item.get("series_folder") == result["series_folder"]
                    and item.get("file") == result["file"]
                )
            ]
            results.append(result)
            counts = result["records"]
            print(
                f"  status={result['status']} models={len(result['models'])} "
                f"pins={counts.get('pin_mux', 0)} errors={len(result['errors'])} "
                f"elapsed={result['elapsed_seconds']}s",
                flush=True,
            )

    results.sort(key=lambda item: (item["series_folder"], item["file"]))
    supported = sum(item["status"] in {"complete", "partial"} for item in results)
    complete = sum(item["status"] == "complete" for item in results)
    non_datasheets = sum(item["status"] in {"not_full_datasheet", "not_datasheet_or_unsupported"} for item in results)
    datasheet_samples = len(results) - non_datasheets
    report = {
        "version": 1,
        "source_root": str(args.source_root.resolve()),
        "samples_per_folder": args.samples_per_folder,
        "series_folders": len(all_folders),
        "samples": len(results),
        "missing_sample_folders": missing,
        "supported_complete_or_partial": supported,
        "complete": complete,
        "partial": sum(item["status"] == "partial" for item in results),
        "not_full_datasheet": sum(item["status"] == "not_full_datasheet" for item in results),
        "not_datasheet_or_unsupported": sum(item["status"] == "not_datasheet_or_unsupported" for item in results),
        "failed": sum(item["status"] in {"failed", "runner_failed"} for item in results),
        "supported_rate": supported / len(results) if results else 0,
        "datasheet_supported_rate": supported / datasheet_samples if datasheet_samples else 0,
        "results": results,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "results"}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
