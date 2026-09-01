from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify that frozen PDF fixtures have not changed.")
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--set", required=True, dest="fixture_set")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    config = manifest[args.fixture_set]
    input_dir = args.root / config["input_dir"]
    errors = []
    for expected in config["files"]:
        path = input_dir / expected["name"]
        if not path.is_file():
            errors.append({"kind": "missing", "file": expected["name"]})
            continue
        actual_size = path.stat().st_size
        actual_hash = sha256(path)
        if actual_size != expected["bytes"] or actual_hash != expected["sha256"]:
            errors.append({"kind": "changed", "file": expected["name"], "bytes": actual_size, "sha256": actual_hash})
    print(json.dumps({"status": "PASS" if not errors else "FAIL", "set": args.fixture_set, "files": len(config["files"]), "errors": errors}, ensure_ascii=False))
    raise SystemExit(1 if errors else 0)


if __name__ == "__main__":
    main()
