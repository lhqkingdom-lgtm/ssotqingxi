from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.document_semantics import detect_arm_cores, resolve_target_model
from core.documents import extract_page_texts


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe target model and Arm core semantics without producing SSOT rows.")
    parser.add_argument("--pdf", required=True, type=Path)
    args = parser.parse_args()
    profile = json.loads((ROOT / "config" / "profiles" / "stm32_v1.json").read_text(encoding="utf-8"))
    config = profile["document_semantics"]
    texts = extract_page_texts(args.pdf)
    model = resolve_target_model(args.pdf, texts, config)
    core = detect_arm_cores(texts, config)
    print(json.dumps({"file": args.pdf.name, "model": model.__dict__, "core": core.__dict__}, ensure_ascii=False))


if __name__ == "__main__":
    main()
