from __future__ import annotations

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def fit(image: Image.Image, max_width: int, max_height: int) -> Image.Image:
    copy = image.convert("RGB")
    copy.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
    return copy


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--excel-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    font = ImageFont.load_default()
    count = 0
    for source_path in sorted(args.source_dir.glob("*.png")):
        excel_path = args.excel_dir / source_path.name
        if not excel_path.exists():
            continue
        with Image.open(source_path) as source_image, Image.open(excel_path) as excel_image:
            left = fit(source_image, 1100, 1400)
            right = fit(excel_image, 1100, 1400)
            banner_height = 42
            gap = 18
            width = left.width + right.width + gap
            height = max(left.height, right.height) + banner_height
            output = Image.new("RGB", (width, height), "white")
            output.paste(left, (0, banner_height))
            output.paste(right, (left.width + gap, banner_height))
            draw = ImageDraw.Draw(output)
            draw.rectangle((0, 0, width, banner_height), fill=(31, 78, 120))
            draw.text((10, 13), f"PDF evidence", fill="white", font=font)
            draw.text((left.width + gap + 10, 13), "Generated Excel rows", fill="white", font=font)
            output.save(args.output_dir / source_path.name)
            count += 1
    print(json.dumps({"comparisons": count, "output_dir": str(args.output_dir)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
