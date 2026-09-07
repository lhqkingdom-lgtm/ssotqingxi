from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


SEVERE_KINDS = {
    "wrong_model_or_package",
    "wrong_physical_pin_identity",
    "channel_as_instance",
    "absolute_as_operating",
    "pll_or_oscillator_as_cpu",
    "unit_order_of_magnitude",
    "contradicts_evidence",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def wilson_lower_bound(successes: int, total: int, z: float = 1.959963984540054) -> float:
    if total <= 0:
        return 0.0
    proportion = successes / total
    denominator = 1 + z * z / total
    centre = proportion + z * z / (2 * total)
    margin = z * math.sqrt((proportion * (1 - proportion) + z * z / (4 * total)) / total)
    return (centre - margin) / denominator


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _known_hashes(paths: Iterable[Path]) -> set[str]:
    hashes: set[str] = set()
    for path in paths:
        data = _read_json(path)
        sets = data.get("sets", data)
        for config in sets.values():
            for item in config.get("files", []):
                if item.get("sha256"):
                    hashes.add(item["sha256"])
    return hashes


def freeze_code_snapshot(root: Path, output_path: Path, includes: Iterable[str]) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for include in includes:
        path = root / include
        candidates = path.rglob("*") if path.is_dir() else [path]
        for candidate in candidates:
            if candidate.is_file() and "__pycache__" not in candidate.parts:
                files.append({"path": candidate.relative_to(root).as_posix(), "sha256": sha256(candidate)})
    files.sort(key=lambda item: item["path"])
    combined = hashlib.sha256()
    for item in files:
        combined.update(item["path"].encode("utf-8"))
        combined.update(b"\0")
        combined.update(item["sha256"].encode("ascii"))
        combined.update(b"\n")
    result = {"version": 1, "combined_sha256": combined.hexdigest(), "files": files}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def freeze_manifest(
    input_dir: Path,
    metadata_path: Path,
    output_path: Path,
    set_name: str,
    excluded_manifests: Iterable[Path],
    excluded_dirs: Iterable[Path] = (),
) -> dict[str, Any]:
    metadata = _read_json(metadata_path)
    by_name = {item["name"]: item for item in metadata["files"]}
    pdfs = sorted(input_dir.glob("*.pdf"), key=lambda path: path.name.casefold())
    errors: list[str] = []
    if set(by_name) != {path.name for path in pdfs}:
        missing_meta = sorted({path.name for path in pdfs} - set(by_name))
        missing_files = sorted(set(by_name) - {path.name for path in pdfs})
        if missing_meta:
            errors.append(f"metadata missing for: {missing_meta}")
        if missing_files:
            errors.append(f"PDF missing for metadata: {missing_files}")

    excluded_hashes = _known_hashes(excluded_manifests)
    for directory in excluded_dirs:
        excluded_hashes.update(sha256(path) for path in directory.rglob("*.pdf"))
    seen: set[str] = set()
    frozen: list[dict[str, Any]] = []
    required = {"vendor", "family", "layout"}
    for path in pdfs:
        item_hash = sha256(path)
        details = by_name.get(path.name, {})
        absent = sorted(required - set(details))
        if absent:
            errors.append(f"{path.name}: missing metadata keys {absent}")
        if item_hash in seen:
            errors.append(f"duplicate content inside set: {path.name}")
        if item_hash in excluded_hashes:
            errors.append(f"content already belongs to another set: {path.name}")
        seen.add(item_hash)
        frozen.append(
            {
                "name": path.name,
                "bytes": path.stat().st_size,
                "sha256": item_hash,
                "vendor": details.get("vendor"),
                "family": details.get("family"),
                "layout": details.get("layout"),
                "source_url": details.get("source_url"),
            }
        )
    if errors:
        raise ValueError("; ".join(errors))
    result = {"version": 1, "sets": {set_name: {"input_dir": str(input_dir), "files": frozen}}}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def _rate(correct: int, total: int) -> float:
    return correct / total if total else 0.0


def _metric(correct: int, total: int) -> dict[str, Any]:
    return {"correct": correct, "total": total, "rate": _rate(correct, total)}


def score(manifest: dict[str, Any], annotations: dict[str, Any], set_name: str) -> dict[str, Any]:
    files = manifest.get("sets", manifest)[set_name]["files"]
    manifest_names = {item["name"] for item in files}
    docs = annotations["documents"]
    annotation_names = {item["name"] for item in docs}
    structural_errors = []
    if annotation_names != manifest_names:
        structural_errors.append(
            {"kind": "document_set_mismatch", "missing": sorted(manifest_names - annotation_names), "extra": sorted(annotation_names - manifest_names)}
        )

    family_by_name = {item["name"]: item["family"] for item in files}
    totals = defaultdict(int)
    family_totals: dict[str, defaultdict[str, int]] = defaultdict(lambda: defaultdict(int))
    severe: list[dict[str, str]] = []

    for doc in docs:
        name = doc["name"]
        family = family_by_name.get(name, "__unknown__")
        buckets = (totals, family_totals[family])
        for bucket in buckets:
            bucket["documents_total"] += 1
            bucket["documents_ok"] += bool(doc.get("extraction_success"))

        for key, target in (("models", "models"), ("records", "records")):
            for record in doc.get(key, []):
                for bucket in buckets:
                    bucket[f"{target}_expected"] += 1
                    bucket[f"{target}_found"] += bool(record.get("found"))

        for field in doc.get("fields", []):
            risk = field.get("risk", "normal")
            if risk not in {"normal", "high"}:
                structural_errors.append({"kind": "invalid_risk", "document": name, "field": field.get("id")})
                continue
            correct = field.get("expected") == field.get("actual") and field.get("attribution_correct", True)
            for bucket in buckets:
                bucket[f"{risk}_fields_total"] += 1
                bucket[f"{risk}_fields_correct"] += correct
            if field.get("evidence_checked"):
                for bucket in buckets:
                    bucket["evidence_total"] += 1
                    bucket["evidence_correct"] += bool(field.get("evidence_correct"))
            severe_kind = field.get("severe_kind")
            if severe_kind:
                if severe_kind not in SEVERE_KINDS:
                    structural_errors.append({"kind": "invalid_severe_kind", "document": name, "field": field.get("id")})
                else:
                    severe.append({"document": name, "field": str(field.get("id")), "kind": severe_kind})

    families = {family: _summarize(bucket) for family, bucket in family_totals.items()}
    result = _summarize(totals)
    layouts = defaultdict(lambda: {"documents": 0, "successful": 0})
    layout_by_name = {item["name"]: item.get("layout") for item in files}
    for doc in docs:
        layout = layout_by_name.get(doc["name"], "__unknown__")
        layouts[layout]["documents"] += 1
        layouts[layout]["successful"] += bool(doc.get("extraction_success"))
    result.update({
        "set": set_name,
        "families": families,
        "layouts": dict(layouts),
        "document_field_samples": {doc["name"]: len(doc.get("fields", [])) for doc in docs},
        "configuration_audit": annotations.get("configuration_audit", {}),
        "severe_errors": severe,
        "structural_errors": structural_errors,
    })
    return result


def _summarize(totals: dict[str, int]) -> dict[str, Any]:
    normal_ok = totals["normal_fields_correct"]
    normal_total = totals["normal_fields_total"]
    return {
        "document_success": _metric(totals["documents_ok"], totals["documents_total"]),
        "model_recall": _metric(totals["models_found"], totals["models_expected"]),
        "record_recall": _metric(totals["records_found"], totals["records_expected"]),
        "high_risk_accuracy": _metric(totals["high_fields_correct"], totals["high_fields_total"]),
        "normal_field_accuracy": {
            **_metric(normal_ok, normal_total),
            "wilson_95_lower": wilson_lower_bound(normal_ok, normal_total),
        },
        "evidence_accuracy": _metric(totals["evidence_correct"], totals["evidence_total"]),
    }


def evaluate_blind(result: dict[str, Any]) -> tuple[bool, list[str]]:
    failures: list[str] = []

    if result["document_success"]["total"] < 30:
        failures.append(f"documents={result['document_success']['total']} < 30")
    if len(result["families"]) < 3:
        failures.append(f"families={len(result['families'])} < 3")
    if len(result["layouts"]) < 5:
        failures.append(f"layouts={len(result['layouts'])} < 5")
    normal_samples = result["normal_field_accuracy"]["total"]
    if normal_samples < 500:
        failures.append(f"normal_field_samples={normal_samples} < 500")
    for name, count in result["document_field_samples"].items():
        if count < 10:
            failures.append(f"document_field_samples[{name}]={count} < 10")

    def check_scope(label: str, scope: dict[str, Any]) -> None:
        thresholds = (
            ("document_success", 0.95),
            ("model_recall", 0.98),
            ("record_recall", 0.95),
            ("high_risk_accuracy", 1.0),
            ("normal_field_accuracy", 0.99),
            ("evidence_accuracy", 0.99),
        )
        for metric, threshold in thresholds:
            value = scope[metric]
            if value["total"] == 0 or value["rate"] < threshold:
                failures.append(f"{label}.{metric}={value['rate']:.6f} < {threshold:.6f} or has no samples")
        normal = scope["normal_field_accuracy"]
        if normal["wilson_95_lower"] < 0.98:
            failures.append(f"{label}.normal_field_accuracy.wilson_95_lower={normal['wilson_95_lower']:.6f} < 0.980000")

    check_scope("overall", result)
    for family, metrics in result["families"].items():
        check_scope(f"family[{family}]", metrics)
    if result["severe_errors"]:
        failures.append(f"severe_errors={len(result['severe_errors'])} > 0")
    if result["structural_errors"]:
        failures.append(f"structural_errors={len(result['structural_errors'])} > 0")
    return not failures, failures


def evaluate_config(result: dict[str, Any]) -> tuple[bool, list[str]]:
    failures: list[str] = []
    documents = result["document_success"]
    if documents["total"] < 10:
        failures.append(f"documents={documents['total']} < 10")
    if documents["rate"] < 0.90:
        failures.append(f"document_success={documents['rate']:.6f} < 0.900000")
    if len(result["layouts"]) < 5:
        failures.append(f"layouts={len(result['layouts'])} < 5")
    for layout, values in result["layouts"].items():
        if values["documents"] < 2:
            failures.append(f"layout[{layout}].documents={values['documents']} < 2")
        if values["successful"] < 1:
            failures.append(f"layout[{layout}].successful=0")
    audit = result["configuration_audit"]
    for key in ("core_business_changes", "filename_model_whitelists"):
        if audit.get(key) != 0:
            failures.append(f"configuration_audit.{key}={audit.get(key)!r} != 0")
    if result["severe_errors"]:
        failures.append(f"severe_errors={len(result['severe_errors'])} > 0")
    if result["structural_errors"]:
        failures.append(f"structural_errors={len(result['structural_errors'])} > 0")
    return not failures, failures


def main() -> None:
    parser = argparse.ArgumentParser(description="Freeze and score SSOT independent quality sets.")
    sub = parser.add_subparsers(dest="command", required=True)
    freeze = sub.add_parser("freeze-manifest")
    freeze.add_argument("--input-dir", type=Path, required=True)
    freeze.add_argument("--metadata", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)
    freeze.add_argument("--set", dest="set_name", required=True)
    freeze.add_argument("--exclude-manifest", type=Path, action="append", default=[])
    freeze.add_argument("--exclude-dir", type=Path, action="append", default=[])
    scoring = sub.add_parser("score")
    scoring.add_argument("--manifest", type=Path, required=True)
    scoring.add_argument("--annotations", type=Path, required=True)
    scoring.add_argument("--set", dest="set_name", required=True)
    scoring.add_argument("--gate", choices=("blind", "config"), default="blind")
    scoring.add_argument("--output", type=Path)
    code = sub.add_parser("freeze-code")
    code.add_argument("--root", type=Path, default=Path.cwd())
    code.add_argument("--output", type=Path, required=True)
    code.add_argument("--include", action="append", required=True)
    args = parser.parse_args()

    try:
        if args.command == "freeze-code":
            result = freeze_code_snapshot(args.root.resolve(), args.output, args.include)
            print(json.dumps({"status": "PASS", "files": len(result["files"]), "combined_sha256": result["combined_sha256"], "output": str(args.output)}, ensure_ascii=False))
            return
        if args.command == "freeze-manifest":
            result = freeze_manifest(args.input_dir, args.metadata, args.output, args.set_name, args.exclude_manifest, args.exclude_dir)
            print(json.dumps({"status": "PASS", "files": len(result["sets"][args.set_name]["files"]), "output": str(args.output)}, ensure_ascii=False))
            return
        result = score(_read_json(args.manifest), _read_json(args.annotations), args.set_name)
        passed, failures = evaluate_blind(result) if args.gate == "blind" else evaluate_config(result)
        result["status"] = "PASS" if passed else "FAIL"
        result["failures"] = failures
        payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(payload, encoding="utf-8")
        print(payload, end="")
        raise SystemExit(0 if passed else 1)
    except (KeyError, TypeError, ValueError) as error:
        print(json.dumps({"status": "ERROR", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
