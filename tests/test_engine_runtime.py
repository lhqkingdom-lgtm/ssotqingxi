from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
import sys


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from core.engine_runtime import ExtractionRun, compare_runs, declarative_module, resolve_engine_mode


SCHEMA = {
    "sheets": {
        "chips": {"key": ["chip_id"]},
        "pin_mux": {"key": ["chip_id", "pin_number", "function_name"]},
    }
}


def run_with(*, flash: int = 32, page: int = 1) -> ExtractionRun:
    evidence = SimpleNamespace(
        source_file="source.pdf",
        page=page,
        bbox=[0, 0, 1, 1],
        kind="table",
        raw_text=str(flash),
    )
    return ExtractionRun(
        record={"models": ["X1"], "model_count": 1, "status": "complete", "sections": {"chips": "success"}},
        evidences=[evidence],
        data={
            "chips": [{"chip_id": "X1", "flash_kb": flash, "_confidence": "A"}],
            "pin_mux": [],
        },
        conflicts=[],
    )


class EngineRuntimeTests(unittest.TestCase):
    def test_shadow_comparison_checks_values_and_evidence(self) -> None:
        self.assertEqual(compare_runs(run_with(), run_with(), SCHEMA), [])
        scopes = {item["scope"] for item in compare_runs(run_with(), run_with(flash=64, page=2), SCHEMA)}
        self.assertEqual(scopes, {"sheet", "evidence"})

    def test_shadow_mode_never_silently_uses_legacy(self) -> None:
        profile = SimpleNamespace(profile_id="sample", payload={})
        self.assertEqual(resolve_engine_mode(None, profile), "legacy")
        with self.assertRaisesRegex(ValueError, "must not silently fall back"):
            declarative_module(profile)

    def test_only_shared_declarative_module_is_accepted(self) -> None:
        profile = SimpleNamespace(
            profile_id="sample",
            payload={"execution": {"declarative_adapter": "family_extractors.declarative"}},
        )
        self.assertEqual(declarative_module(profile), "family_extractors.declarative")


if __name__ == "__main__":
    unittest.main()
