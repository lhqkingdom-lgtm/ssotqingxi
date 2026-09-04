from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from core.pipeline import profiles_from_registry, run_extraction


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True, write_through=True)
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Extract MCU SSOT fields through a selected document-family profile.")
    parser.add_argument("--input", required=True, type=Path, help="PDF folder")
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--schema", type=Path, default=project_root / "config" / "schema.json")
    parser.add_argument("--profile", type=Path, action="append", default=None, help="Repeat to restrict automatic classification to selected profiles")
    parser.add_argument("--family-registry", type=Path, default=project_root / "config" / "document_families.json")
    parser.add_argument("--aliases", type=Path, default=None, help="Optional profile aliases override")
    parser.add_argument("--ingested-by", default=getpass.getuser())
    parser.add_argument("--ingested-at", default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Return success when some documents fail, while retaining errors in extracted.json.",
    )
    args = parser.parse_args()
    profile_paths = args.profile or profiles_from_registry(args.family_registry)
    payload = run_extraction(
        input_dir=args.input,
        output_dir=args.output_dir,
        schema_path=args.schema,
        profile_paths=profile_paths,
        ingested_by=args.ingested_by,
        ingested_at=args.ingested_at,
        limit=args.limit,
        aliases_override=args.aliases,
    )
    if not args.allow_partial and (payload["errors"] or payload["validation_issues"]):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
