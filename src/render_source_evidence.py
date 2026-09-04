from __future__ import annotations

import argparse
import json
from pathlib import Path

import pdfplumber
from PIL import Image, ImageDraw, ImageFont


def banner(image: Image.Image, text: str) -> Image.Image:
    font = ImageFont.load_default()
    height = 34
    output = Image.new("RGB", (image.width, image.height + height), "white")
    output.paste(image.convert("RGB"), (0, height))
    draw = ImageDraw.Draw(output)
    draw.rectangle((0, 0, output.width, height), fill=(31, 78, 120))
    draw.text((10, 10), text, fill="white", font=font)
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--resolution", type=int, default=160)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    evidences = [json.loads(line) for line in args.evidence.read_text(encoding="utf-8").splitlines() if line.strip()]
    by_file: dict[str, list[dict]] = {}
    for evidence in evidences:
        by_file.setdefault(evidence["source_file"], []).append(evidence)
    rendered = 0
    for source_file, items in by_file.items():
        with pdfplumber.open(source_file) as pdf:
            for evidence in items:
                page = pdf.pages[int(evidence["page"]) - 1]
                x0, top, x1, bottom = [float(value) for value in evidence["bbox"]]
                padding = 18
                crop_box = (
                    max(0, x0 - padding),
                    max(0, top - padding),
                    min(float(page.width), x1 + padding),
                    min(float(page.height), bottom + padding),
                )
                cropped = page.crop(crop_box)
                page_image = cropped.to_image(resolution=args.resolution, antialias=True)
                page_image.draw_rect((x0, top, x1, bottom), stroke="red", stroke_width=3)
                output_path = args.output_dir / f"{evidence['evidence_id']}.png"
                temp_path = args.output_dir / f".{evidence['evidence_id']}.tmp.png"
                page_image.save(temp_path, format="PNG")
                image = Image.open(temp_path)
                label = f"{evidence['evidence_id']} | {Path(source_file).name} | PDF page {evidence['page']}"
                banner(image, label).save(output_path)
                image.close()
                temp_path.unlink(missing_ok=True)
                rendered += 1
    print(json.dumps({"rendered": rendered, "output_dir": str(args.output_dir)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
