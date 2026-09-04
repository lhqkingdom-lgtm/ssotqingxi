from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = PROJECT_ROOT
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from core.output_contract import validate_data_contract

DEFAULT_TEMPLATE = WORKSPACE_ROOT / "template" / "ssot_template.xlsx"
DEFAULT_SCHEMA = PROJECT_ROOT / "config" / "schema.json"
DEFAULT_INPUT_DIR = WORKSPACE_ROOT / "datasheet"
DEFAULT_FAMILY_REGISTRY = PROJECT_ROOT / "config" / "document_families.json"
WRITER = PROJECT_ROOT / "src" / "write_workbook.mjs"
MAX_REQUEST_BYTES = 100 * 1024 * 1024
MAX_STATE_REQUEST_BYTES = 20 * 1024 * 1024
REVIEW_RESULT_FILENAME = "review_result.json"
REVIEW_RESULT_VERSION = 1
CONSOLE_DIR = Path(__file__).resolve().parent / "console"


class ReviewStateConflictError(RuntimeError):
    pass


def review_status(chip: object) -> str:
    if not isinstance(chip, dict):
        return "unreviewed"
    decision = chip.get("decision")
    if decision == "issue":
        return "issue"
    if decision != "approved":
        return "unreviewed"
    review_revision = chip.get("review_revision", 0)
    exported_revision = chip.get("exported_revision", 0)
    # Read the legacy synced revision only to keep older review_result.json files compatible.
    synced_revision = chip.get("synced_revision", 0)
    if isinstance(synced_revision, int) and isinstance(review_revision, int) and synced_revision >= review_revision:
        return "exported"
    if isinstance(exported_revision, int) and isinstance(review_revision, int) and exported_revision >= review_revision:
        return "exported"
    return "pending_export"


class ReviewStateStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._lock = threading.Lock()

    def _review_dir(self, batch: str) -> Path:
        if not isinstance(batch, str) or not batch.strip():
            raise ValueError("missing batch path")
        relative = Path(batch)
        if relative.is_absolute():
            raise ValueError("batch path must be relative to the review server root")
        target = (self.root / relative).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as error:
            raise ValueError("batch path escapes the review server root") from error
        if not target.is_dir() or not (target / "review_dataset.json").is_file():
            raise FileNotFoundError("review batch or review_dataset.json not found")
        return target

    @staticmethod
    def _dataset_meta(review_dir: Path) -> tuple[dict[str, object], set[str], set[str]]:
        dataset = json.loads((review_dir / "review_dataset.json").read_text(encoding="utf-8"))
        chip_ids = {
            str(chip["chip_id"])
            for chip in dataset.get("chips", [])
            if isinstance(chip, dict) and isinstance(chip.get("chip_id"), str)
        }
        sheets = {str(sheet) for sheet in dataset.get("sheet_order", [])}
        return dataset, chip_ids, sheets

    @staticmethod
    def _validate_state(state: object, dataset: dict[str, object], chip_ids: set[str], sheets: set[str]) -> dict[str, object]:
        if not isinstance(state, dict):
            raise ValueError("review state must be a JSON object")
        if state.get("state_version") != 1:
            raise ValueError("unsupported review state version")
        if state.get("run_id") != dataset.get("run_id"):
            raise ValueError("review state run_id does not match this batch")
        if state.get("base_extracted_sha256") != dataset.get("base_extracted_sha256"):
            raise ValueError("review state base hash does not match this batch")
        states = state.get("chips")
        if not isinstance(states, dict) or set(states) != chip_ids:
            raise ValueError("review state chip set does not match this batch")
        changes = state.get("record_changes", {})
        if not isinstance(changes, dict) or not set(changes).issubset(chip_ids):
            raise ValueError("review record changes contain an unknown chip")
        for chip_id, by_sheet in changes.items():
            if not isinstance(by_sheet, dict) or not set(by_sheet).issubset(sheets):
                raise ValueError(f"review record changes contain an unknown sheet for {chip_id}")
            for sheet, change in by_sheet.items():
                if not isinstance(change, dict):
                    raise ValueError(f"invalid record changes for {chip_id}/{sheet}")
                if not isinstance(change.get("updated", {}), dict) or not isinstance(change.get("deleted", []), list) or not isinstance(change.get("added", []), list):
                    raise ValueError(f"invalid record change collections for {chip_id}/{sheet}")
        if not isinstance(state.get("export_batches", []), list) or not isinstance(state.get("applied_receipts", []), list):
            raise ValueError("invalid review export history")
        return state

    def _read_unlocked(self, review_dir: Path) -> dict[str, object]:
        target = review_dir / REVIEW_RESULT_FILENAME
        if not target.is_file():
            return {"exists": False, "revision": 0, "saved_at": None, "state": None}
        envelope = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(envelope, dict) or envelope.get("file_version") != REVIEW_RESULT_VERSION:
            raise ValueError("invalid review_result.json format")
        revision = envelope.get("revision")
        if not isinstance(revision, int) or revision < 1:
            raise ValueError("invalid review_result.json revision")
        dataset, chip_ids, sheets = self._dataset_meta(review_dir)
        state = self._validate_state(envelope.get("state"), dataset, chip_ids, sheets)
        return {"exists": True, "revision": revision, "saved_at": envelope.get("saved_at"), "state": state}

    def load(self, batch: str) -> dict[str, object]:
        review_dir = self._review_dir(batch)
        with self._lock:
            return self._read_unlocked(review_dir)

    def save(self, batch: str, expected_revision: int, state: object) -> dict[str, object]:
        if not isinstance(expected_revision, int) or expected_revision < 0:
            raise ValueError("expected_revision must be a non-negative integer")
        review_dir = self._review_dir(batch)
        with self._lock:
            current = self._read_unlocked(review_dir)
            if current["revision"] != expected_revision:
                raise ReviewStateConflictError(
                    f"review state revision conflict: expected {expected_revision}, current {current['revision']}"
                )
            dataset, chip_ids, sheets = self._dataset_meta(review_dir)
            validated = self._validate_state(state, dataset, chip_ids, sheets)
            if current["exists"] and current["state"] == validated:
                return current
            revision = expected_revision + 1
            saved_at = datetime.now(timezone.utc).isoformat()
            envelope = {
                "file_version": REVIEW_RESULT_VERSION,
                "revision": revision,
                "saved_at": saved_at,
                "state": validated,
            }
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".review_result.", suffix=".tmp", dir=review_dir
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(envelope, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, review_dir / REVIEW_RESULT_FILENAME)
            finally:
                temporary.unlink(missing_ok=True)
            return {"exists": True, "revision": revision, "saved_at": saved_at, "state": validated}


class ReviewBatchCatalog:
    def __init__(self, root: Path, state_store: ReviewStateStore) -> None:
        self.root = root.resolve()
        self.output_root = self.root / "output"
        self.state_store = state_store

    def _batch_info(self, dataset_path: Path) -> dict[str, object]:
        review_dir = dataset_path.parent
        relative = review_dir.relative_to(self.root).as_posix()
        batch_name = review_dir.parent.name if review_dir.name == "review" else review_dir.name
        modified_at = datetime.fromtimestamp(dataset_path.stat().st_mtime, timezone.utc).isoformat()
        base: dict[str, object] = {
            "batch_id": relative,
            "name": batch_name,
            "review_url": f"/review/?batch={quote(relative, safe='')}",
            "modified_at": modified_at,
            "status": "ready",
        }
        try:
            dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
            if not isinstance(dataset, dict) or not isinstance(dataset.get("chips"), list):
                raise ValueError("review_dataset.json must contain a chips array")
            chips = [chip for chip in dataset["chips"] if isinstance(chip, dict) and isinstance(chip.get("chip_id"), str)]
            documents = {
                str(document)
                for chip in chips
                for document in chip.get("documents", [])
                if isinstance(document, str) and document
            }
            summary = dataset.get("summary") if isinstance(dataset.get("summary"), dict) else {}
            records = summary.get("records") if isinstance(summary.get("records"), dict) else {}
            source_issues = dataset.get("source_issues") if isinstance(dataset.get("source_issues"), list) else []
            counts = {
                "unreviewed": len(chips),
                "issue": 0,
                "pending_export": 0,
                "exported": 0,
            }
            revision = 0
            saved_at = None
            try:
                saved = self.state_store.load(relative)
                revision = int(saved["revision"])
                saved_at = saved.get("saved_at")
                if saved["exists"]:
                    counts = {key: 0 for key in counts}
                    for chip_state in saved["state"]["chips"].values():
                        counts[review_status(chip_state)] += 1
            except (FileNotFoundError, ValueError, json.JSONDecodeError) as error:
                base["status"] = "state_error"
                base["error"] = str(error)
            base.update({
                "run_id": dataset.get("run_id"),
                "chip_count": len(chips),
                "document_count": len(documents),
                "record_count": sum(value for value in records.values() if isinstance(value, int)),
                "source_issue_count": len(source_issues),
                "review_revision": revision,
                "review_saved_at": saved_at,
                "review_counts": counts,
            })
        except (OSError, ValueError, json.JSONDecodeError) as error:
            base.update({
                "status": "dataset_error",
                "error": str(error),
                "chip_count": 0,
                "document_count": 0,
                "record_count": 0,
                "source_issue_count": 0,
                "review_revision": 0,
                "review_saved_at": None,
                "review_counts": {},
            })
        return base

    def list_batches(self) -> dict[str, object]:
        paths = list(self.output_root.rglob("review_dataset.json")) if self.output_root.is_dir() else []
        batches = [self._batch_info(path) for path in paths]
        batches.sort(key=lambda item: str(item.get("modified_at", "")), reverse=True)
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "batch_count": len(batches),
            "batches": batches,
        }


class DocumentCatalog:
    def __init__(self, root: Path, input_dir: Path, family_registry: Path) -> None:
        self.root = root.resolve()
        self.input_dir = input_dir.resolve()
        self.output_root = self.root / "output"
        self.profiles = self._load_profiles(family_registry.resolve())
        self._hash_cache: dict[tuple[str, int, int], str] = {}

    @staticmethod
    def _load_profiles(registry_path: Path) -> list[dict[str, object]]:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        profiles: list[dict[str, object]] = []
        for relative in registry.get("profiles", []):
            profile_path = (registry_path.parent / str(relative)).resolve()
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            detection = profile.get("detection") if isinstance(profile.get("detection"), dict) else {}
            profiles.append({
                "profile_id": str(profile.get("profile_id", profile_path.stem)),
                "description": str(profile.get("description", "")),
                "filename_any": [str(value) for value in detection.get("filename_any", [])],
            })
        return profiles

    def _sha256(self, path: Path) -> str:
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns)
        cached = self._hash_cache.get(key)
        if cached:
            return cached
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        value = digest.hexdigest()
        self._hash_cache[key] = value
        return value

    def _filename_families(self, filename: str) -> list[str]:
        folded = filename.casefold()
        return [
            str(profile["profile_id"])
            for profile in self.profiles
            if any(token.casefold() in folded for token in profile["filename_any"])
        ]

    @staticmethod
    def _document_status(document: dict[str, object]) -> str:
        if str(document.get("status", "")).lower() in {"partial", "partially_complete"}:
            return "partial"
        sections = document.get("sections")
        if isinstance(sections, dict) and any(value != "success" for value in sections.values()):
            return "partial"
        if isinstance(document.get("review_items"), list) and document["review_items"]:
            return "partial"
        return "complete" if int(document.get("model_count", 0) or 0) > 0 else "parse_failed"

    def _extraction_index(self) -> dict[str, list[dict[str, object]]]:
        index: dict[str, list[dict[str, object]]] = {}
        paths = list(self.output_root.rglob("extracted.json")) if self.output_root.is_dir() else []
        for extracted_path in paths:
            try:
                payload = json.loads(extracted_path.read_text(encoding="utf-8"))
                documents = payload.get("documents") if isinstance(payload.get("documents"), list) else []
                batch_dir = extracted_path.parent
                review_dir = batch_dir / "review"
                if not (review_dir / "review_dataset.json").is_file():
                    continue
                relative_review = review_dir.relative_to(self.root).as_posix()
                review_url = f"/review/?batch={quote(relative_review, safe='')}"
                for document in documents:
                    if not isinstance(document, dict) or not isinstance(document.get("sha256"), str):
                        continue
                    item = {
                        "batch_name": batch_dir.name,
                        "modified_at": datetime.fromtimestamp(extracted_path.stat().st_mtime, timezone.utc).isoformat(),
                        "family_id": document.get("family_id"),
                        "status": self._document_status(document),
                        "chip_ids": [str(value) for value in document.get("models", [])],
                        "review_url": review_url,
                    }
                    index.setdefault(document["sha256"], []).append(item)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        for items in index.values():
            items.sort(key=lambda item: str(item["modified_at"]), reverse=True)
        return index

    def list_documents(self) -> dict[str, object]:
        extraction_index = self._extraction_index()
        pdfs = sorted(self.input_dir.rglob("*.pdf"), key=lambda path: path.as_posix().casefold())
        scanned: list[tuple[Path, str]] = [(path, self._sha256(path)) for path in pdfs]
        duplicate_counts: dict[str, int] = {}
        for _, digest in scanned:
            duplicate_counts[digest] = duplicate_counts.get(digest, 0) + 1

        documents: list[dict[str, object]] = []
        for path, digest in scanned:
            matches = extraction_index.get(digest, [])
            latest = matches[0] if matches else None
            candidates = self._filename_families(path.name)
            if latest:
                status = str(latest["status"])
                family_id = latest.get("family_id")
                classification_source = "extraction"
            elif len(candidates) == 1:
                status = "pending"
                family_id = candidates[0]
                classification_source = "filename"
            elif len(candidates) > 1:
                status = "classification_conflict"
                family_id = None
                classification_source = "filename"
            else:
                status = "unclassified"
                family_id = None
                classification_source = "filename"
            review_url = latest.get("review_url") if latest else None
            if review_url:
                review_url = f"{review_url}&document={quote(path.name, safe='')}"
            stat = path.stat()
            chip_ids = latest.get("chip_ids", []) if latest else []
            documents.append({
                "document_id": digest[:16],
                "filename": path.name,
                "relative_path": path.relative_to(self.input_dir).as_posix(),
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                "sha256": digest,
                "family_id": family_id,
                "family_candidates": candidates,
                "classification_source": classification_source,
                "status": status,
                "duplicate": duplicate_counts[digest] > 1,
                "duplicate_count": duplicate_counts[digest],
                "chip_ids": chip_ids,
                "chip_count": len(chip_ids),
                "batch_name": latest.get("batch_name") if latest else None,
                "review_url": review_url,
            })
        status_counts: dict[str, int] = {}
        for document in documents:
            status = str(document["status"])
            status_counts[status] = status_counts.get(status, 0) + 1
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "input_dir": str(self.input_dir),
            "document_count": len(documents),
            "duplicate_count": sum(1 for document in documents if document["duplicate"]),
            "status_counts": status_counts,
            "documents": documents,
        }

    def resolve_document_ids(self, document_ids: list[str]) -> list[Path]:
        requested = {str(value) for value in document_ids if str(value).strip()}
        if not requested:
            raise ValueError("请至少选择一份待解析 PDF")
        resolved: dict[str, Path] = {}
        for path in self.input_dir.rglob("*.pdf"):
            document_id = self._sha256(path)[:16]
            if document_id in requested:
                resolved[document_id] = path.resolve()
        missing = sorted(requested - resolved.keys())
        if missing:
            raise FileNotFoundError(f"找不到所选 PDF：{', '.join(missing)}")
        return [resolved[value] for value in document_ids if value in resolved]


class ParseJobManager:
    def __init__(self, root: Path, document_catalog: DocumentCatalog, template_path: Path) -> None:
        self.root = root.resolve()
        self.document_catalog = document_catalog
        self.template_path = template_path.resolve()
        self._lock = threading.Lock()
        self._job: dict[str, object] = {"status": "idle", "logs": []}

    def status(self) -> dict[str, object]:
        with self._lock:
            result = dict(self._job)
            result["logs"] = list(self._job.get("logs", []))[-120:]
            return result

    def start(self, document_ids: list[str]) -> dict[str, object]:
        sources = self.document_catalog.resolve_document_ids(document_ids)
        with self._lock:
            if self._job.get("status") == "running":
                raise RuntimeError("已有解析任务正在运行，请等待完成")
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_dir = self.root / "output" / f"frontend_parse_{stamp}"
            self._job = {
                "job_id": stamp,
                "status": "running",
                "document_ids": list(document_ids),
                "filenames": [path.name for path in sources],
                "output_dir": str(output_dir),
                "started_at": datetime.now(timezone.utc).isoformat(),
                "logs": ["准备解析输入文档…"],
            }
        threading.Thread(target=self._run, args=(sources, output_dir), daemon=True).start()
        return self.status()

    def _append_log(self, line: str) -> None:
        with self._lock:
            logs = self._job.setdefault("logs", [])
            if isinstance(logs, list):
                logs.append(line.rstrip())
                del logs[:-200]

    def _run(self, sources: list[Path], output_dir: Path) -> None:
        staging_root = self.root / "tmp"
        staging_root.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix="frontend_parse_", dir=staging_root))
        try:
            for source in sources:
                shutil.copy2(source, staging / source.name)
            command = [
                "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(PROJECT_ROOT / "run.ps1"),
                "-InputDir", str(staging),
                "-OutputDir", str(output_dir),
                "-Template", str(self.template_path),
                "-IngestedBy", "frontend",
            ]
            process = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="gbk" if os.name == "nt" else "utf-8",
                errors="replace",
            )
            assert process.stdout is not None
            for line in process.stdout:
                if line.strip():
                    self._append_log(line)
            exit_code = process.wait()
            if exit_code != 0:
                raise RuntimeError(f"解析流程退出码：{exit_code}")
            relative_review = (output_dir / "review").relative_to(self.root).as_posix()
            with self._lock:
                self._job.update({
                    "status": "complete",
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "review_url": f"/review/?batch={quote(relative_review, safe='')}",
                    "excel_path": str(output_dir / "ssot_generated.xlsx"),
                })
        except Exception as error:
            self._append_log(f"错误：{error}")
            with self._lock:
                self._job.update({
                    "status": "failed",
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "error": str(error),
                })
        finally:
            shutil.rmtree(staging, ignore_errors=True)


def default_node_executable() -> Path:
    bundled = (
        Path.home()
        / ".cache"
        / "codex-runtimes"
        / "codex-primary-runtime"
        / "dependencies"
        / "node"
        / "bin"
        / "node.exe"
    )
    if bundled.is_file():
        return bundled
    discovered = shutil.which("node")
    if discovered:
        return Path(discovered)
    raise FileNotFoundError("Node.js runtime not found; pass --node-exe")


def export_payload(batch: dict[str, object], schema: dict[str, object]) -> dict[str, object]:
    if not isinstance(batch, dict) or not isinstance(batch.get("export_batch_id"), str):
        raise ValueError("missing export_batch_id")
    chips = batch.get("chips")
    if not isinstance(chips, list) or not chips:
        raise ValueError("the export batch contains no chips")

    sheet_configs = schema.get("sheets")
    if not isinstance(sheet_configs, dict) or not sheet_configs:
        raise ValueError("invalid workbook schema")
    data: dict[str, list[dict[str, object]]] = {name: [] for name in sheet_configs}

    for chip in chips:
        if not isinstance(chip, dict) or not isinstance(chip.get("chip_id"), str):
            raise ValueError("invalid chip in export batch")
        chip_id = chip["chip_id"]
        tables = chip.get("tables")
        if not isinstance(tables, dict):
            raise ValueError(f"missing tables for {chip_id}")
        for sheet_name, config in sheet_configs.items():
            fields = config.get("fields") if isinstance(config, dict) else None
            rows = tables.get(sheet_name, [])
            if not isinstance(fields, list) or not isinstance(rows, list):
                raise ValueError(f"invalid {sheet_name} table for {chip_id}")
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError(f"invalid {sheet_name} row for {chip_id}")
                if row.get("chip_id") != chip_id:
                    raise ValueError(f"{sheet_name} row chip_id does not match {chip_id}")
                public = {field: row.get(field) for field in fields}
                public["is_reviewed"] = True
                data[sheet_name].append(public)
    issues = validate_data_contract(data, schema)
    if issues:
        summary = "; ".join(str(item.get("message")) for item in issues[:5])
        raise ValueError(f"reviewed data violates the output contract: {summary}")
    return {"data": data}


def build_workbook_bytes(
    batch: dict[str, object],
    *,
    schema_path: Path,
    template_path: Path,
    node_executable: Path,
) -> bytes:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    payload = export_payload(batch, schema)
    with tempfile.TemporaryDirectory(prefix="ssot-review-export-") as temp:
        temp_dir = Path(temp)
        input_path = temp_dir / "reviewed.json"
        output_path = temp_dir / "reviewed.xlsx"
        row_map_path = temp_dir / "row_map.json"
        input_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        command = [
            str(node_executable),
            str(WRITER),
            "--input", str(input_path),
            "--template", str(template_path),
            "--schema", str(schema_path),
            "--output", str(output_path),
            "--row-map", str(row_map_path),
        ]
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            creationflags=creationflags,
            check=False,
        )
        post_write_status = completed.returncode in (1, -1073740791, 3221226505)
        output_complete = output_path.is_file() and row_map_path.is_file()
        if completed.returncode != 0 and not (post_write_status and output_complete):
            detail = (completed.stderr or completed.stdout or f"exit {completed.returncode}").strip()
            raise RuntimeError(f"Excel generation failed: {detail[-1500:]}")
        if not output_complete:
            raise RuntimeError("Excel generation did not produce the workbook and row map")
        return output_path.read_bytes()


class ReviewRequestHandler(SimpleHTTPRequestHandler):
    server: "ReviewHTTPServer"

    def _send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_error(self, status: HTTPStatus, message: str) -> None:
        self._send_json(status, {"error": message})

    def _send_console_file(self, filename: str, content_type: str) -> None:
        target = CONSOLE_DIR / filename
        if not target.is_file():
            self._json_error(HTTPStatus.NOT_FOUND, "review console asset not found")
            return
        body = target.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _state_batch(self) -> str:
        values = parse_qs(urlparse(self.path).query).get("batch", [])
        if len(values) != 1:
            raise ValueError("exactly one batch query parameter is required")
        return values[0]

    def do_PUT(self) -> None:  # noqa: N802 - inherited HTTP method name
        if urlparse(self.path).path != "/api/review/state":
            self._json_error(HTTPStatus.NOT_FOUND, "unknown endpoint")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_STATE_REQUEST_BYTES:
                raise ValueError("invalid review state request size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("review state request must be a JSON object")
            result = self.server.state_store.save(
                self._state_batch(), payload.get("expected_revision"), payload.get("state")
            )
            self._send_json(HTTPStatus.OK, result)
        except ReviewStateConflictError as error:
            self._json_error(HTTPStatus.CONFLICT, str(error))
        except FileNotFoundError as error:
            self._json_error(HTTPStatus.NOT_FOUND, str(error))
        except (ValueError, json.JSONDecodeError) as error:
            self._json_error(HTTPStatus.BAD_REQUEST, str(error))
        except Exception as error:
            self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

    def do_POST(self) -> None:  # noqa: N802 - inherited HTTP method name
        if self.path == "/api/parse/start":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 64 * 1024:
                    raise ValueError("invalid parse request size")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                document_ids = payload.get("document_ids") if isinstance(payload, dict) else None
                if not isinstance(document_ids, list):
                    raise ValueError("document_ids must be an array")
                self._send_json(HTTPStatus.ACCEPTED, self.server.parse_jobs.start(document_ids))
            except RuntimeError as error:
                self._json_error(HTTPStatus.CONFLICT, str(error))
            except FileNotFoundError as error:
                self._json_error(HTTPStatus.NOT_FOUND, str(error))
            except (ValueError, json.JSONDecodeError) as error:
                self._json_error(HTTPStatus.BAD_REQUEST, str(error))
            except Exception as error:
                self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))
            return
        if self.path != "/api/review/export-xlsx":
            self._json_error(HTTPStatus.NOT_FOUND, "unknown endpoint")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_REQUEST_BYTES:
                raise ValueError("invalid request size")
            batch = json.loads(self.rfile.read(length).decode("utf-8"))
            content = build_workbook_bytes(
                batch,
                schema_path=self.server.schema_path,
                template_path=self.server.template_path,
                node_executable=self.server.node_executable,
            )
            batch_id = str(batch["export_batch_id"])
            filename = f"{batch_id}.xlsx"
            self.server.store_export(batch_id, filename, content)
            result = json.dumps({
                "export_batch_id": batch_id,
                "filename": filename,
                "download_url": f"/api/review/download/{quote(batch_id, safe='')}",
                "bytes": len(content),
            }, ensure_ascii=False).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(result)))
            self.end_headers()
            self.wfile.write(result)
        except (ValueError, json.JSONDecodeError) as error:
            self._json_error(HTTPStatus.BAD_REQUEST, str(error))
        except Exception as error:  # keep the local UI usable and return a concise failure
            self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))

    def do_GET(self) -> None:  # noqa: N802 - inherited HTTP method name
        prefix = "/api/review/download/"
        parsed = urlparse(self.path)
        if parsed.path == "/api/review/batches":
            try:
                self._send_json(HTTPStatus.OK, self.server.batch_catalog.list_batches())
            except Exception as error:
                self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))
            return
        if parsed.path == "/api/documents":
            try:
                self._send_json(HTTPStatus.OK, self.server.document_catalog.list_documents())
            except Exception as error:
                self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))
            return
        if parsed.path == "/api/parse/status":
            self._send_json(HTTPStatus.OK, self.server.parse_jobs.status())
            return
        if parsed.path == "/api/review/state":
            try:
                self._send_json(HTTPStatus.OK, self.server.state_store.load(self._state_batch()))
            except FileNotFoundError as error:
                self._json_error(HTTPStatus.NOT_FOUND, str(error))
            except (ValueError, json.JSONDecodeError) as error:
                self._json_error(HTTPStatus.BAD_REQUEST, str(error))
            except Exception as error:
                self._json_error(HTTPStatus.INTERNAL_SERVER_ERROR, str(error))
            return
        console_assets = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/review-console/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/review-console/styles.css": ("styles.css", "text/css; charset=utf-8"),
        }
        if parsed.path in console_assets:
            self._send_console_file(*console_assets[parsed.path])
            return
        review_assets = {
            "/review/": ("index.html", "text/html; charset=utf-8"),
            "/review/index.html": ("index.html", "text/html; charset=utf-8"),
            "/review/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/review/review_state.js": ("review_state.js", "text/javascript; charset=utf-8"),
            "/review/styles.css": ("styles.css", "text/css; charset=utf-8"),
        }
        if parsed.path in review_assets:
            filename, content_type = review_assets[parsed.path]
            target = Path(__file__).resolve().parent / "static" / filename
            if not target.is_file():
                self._json_error(HTTPStatus.NOT_FOUND, "review application asset not found")
                return
            body = target.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not parsed.path.startswith(prefix):
            super().do_GET()
            return
        batch_id = unquote(parsed.path[len(prefix):])
        exported = self.server.get_export(batch_id)
        if not exported:
            self._json_error(HTTPStatus.NOT_FOUND, "Excel export is no longer available; generate it again")
            return
        filename, content = exported
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename)}")
        self.send_header("X-Review-Export-Batch-Id", batch_id)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


class ReviewHTTPServer(ThreadingHTTPServer):
    def __init__(
        self,
        address: tuple[str, int],
        root: Path,
        *,
        schema_path: Path,
        template_path: Path,
        node_executable: Path,
        input_dir: Path = DEFAULT_INPUT_DIR,
        family_registry: Path = DEFAULT_FAMILY_REGISTRY,
    ) -> None:
        handler = lambda *args, **kwargs: ReviewRequestHandler(*args, directory=str(root), **kwargs)
        super().__init__(address, handler)
        self.schema_path = schema_path
        self.template_path = template_path
        self.node_executable = node_executable
        self.state_store = ReviewStateStore(root)
        self.batch_catalog = ReviewBatchCatalog(root, self.state_store)
        self.document_catalog = DocumentCatalog(root, input_dir, family_registry)
        self.parse_jobs = ParseJobManager(root, self.document_catalog, template_path)
        self._exports: dict[str, tuple[str, bytes]] = {}
        self._export_order: list[str] = []
        self._export_lock = threading.Lock()

    def store_export(self, batch_id: str, filename: str, content: bytes) -> None:
        with self._export_lock:
            self._exports[batch_id] = (filename, content)
            if batch_id in self._export_order:
                self._export_order.remove(batch_id)
            self._export_order.append(batch_id)
            while len(self._export_order) > 20:
                expired = self._export_order.pop(0)
                self._exports.pop(expired, None)

    def get_export(self, batch_id: str) -> tuple[str, bytes] | None:
        with self._export_lock:
            return self._exports.get(batch_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve SSOT review pages and generate reviewed Excel exports.")
    parser.add_argument("--root", type=Path, default=WORKSPACE_ROOT)
    parser.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--node-exe", type=Path)
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR)
    parser.add_argument("--family-registry", type=Path, default=DEFAULT_FAMILY_REGISTRY)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    node_executable = args.node_exe or default_node_executable()
    for path, label in ((args.root, "root"), (args.template, "template"), (args.schema, "schema"), (node_executable, "node"), (args.input_dir, "input directory"), (args.family_registry, "family registry")):
        if not path.exists():
            raise FileNotFoundError(f"{label} not found: {path}")
    server = ReviewHTTPServer(
        (args.bind, args.port),
        args.root.resolve(),
        schema_path=args.schema.resolve(),
        template_path=args.template.resolve(),
        node_executable=node_executable.resolve(),
        input_dir=args.input_dir.resolve(),
        family_registry=args.family_registry.resolve(),
    )
    print(f"Review server: http://{args.bind}:{args.port}/", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
