from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import pdfplumber
from PIL import Image

try:
    from review_app.build_dataset import validate_review_dataset
except ModuleNotFoundError:  # Direct execution: python src/review_app/build_readonly.py
    from build_dataset import validate_review_dataset


def evidence_image_name(evidence_id: object) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(evidence_id)) + ".png"


def image_meets_resolution(target: Path, crop_width_points: float, resolution: int) -> bool:
    if not target.is_file() or target.stat().st_size <= 0:
        return False
    expected_width = max(1, round(crop_width_points * resolution / 72.0))
    try:
        with Image.open(target) as image:
            return image.width >= expected_width - 2
    except OSError:
        return False


def render_evidence_images(dataset: dict[str, object], output_dir: Path, resolution: int = 300) -> tuple[int, int]:
    image_dir = output_dir / "evidence"
    image_dir.mkdir(parents=True, exist_ok=True)
    grouped: dict[Path, list[dict[str, object]]] = {}
    missing = 0
    for item in dataset.get("evidence", []):
        source = Path(str(item.get("source_file", "")))
        if not source.is_file():
            missing += 1
            continue
        grouped.setdefault(source, []).append(item)

    generated = 0
    for source, items in grouped.items():
        with pdfplumber.open(source) as pdf:
            for item in items:
                target = image_dir / evidence_image_name(item.get("evidence_id"))
                page_number = int(item["page"])
                if page_number < 1 or page_number > len(pdf.pages):
                    missing += 1
                    continue
                page = pdf.pages[page_number - 1]
                x0, top, x1, bottom = [float(value) for value in item["bbox"]]
                padding = 14.0
                crop = (
                    max(0.0, x0 - padding),
                    max(0.0, top - padding),
                    min(float(page.width), x1 + padding),
                    min(float(page.height), bottom + padding),
                )
                if image_meets_resolution(target, crop[2] - crop[0], resolution):
                    generated += 1
                    continue
                page.crop(crop).to_image(resolution=resolution, antialias=True).save(target, format="PNG")
                generated += 1
    return generated, missing


def build_readonly_review(dataset_path: Path, output_dir: Path) -> dict[str, object]:
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    errors = validate_review_dataset(dataset)
    if errors:
        raise ValueError("invalid review dataset:\n- " + "\n- ".join(errors))
    output_dir.mkdir(parents=True, exist_ok=True)
    target_dataset = output_dir / "review_dataset.json"
    if dataset_path.resolve() != target_dataset.resolve():
        target_dataset.write_bytes(dataset_path.read_bytes())
    generated, missing = render_evidence_images(dataset, output_dir)
    return {
        "output": str(target_dataset),
        "chips": dataset["summary"]["chips"],
        "records": dataset["summary"]["records"],
        "evidence_images": generated,
        "missing_evidence_images": missing,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the chip-centered read-only review page.")
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    result = build_readonly_review(args.dataset, args.output_dir)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
