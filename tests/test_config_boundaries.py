from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from core.profile import load_profile
from core.units import convert_number
from family_extractors import ca_miai


ROOT = Path(__file__).resolve().parents[1]
CA_PROFILE = ROOT / "config" / "profiles" / "ca_miai_v1.json"
class ConfigurationBoundaryTests(unittest.TestCase):
    @staticmethod
    def portable_profile(payload: dict) -> dict:
        payload["dictionary"]["base"] = str((ROOT / "config" / "common_field_dictionary.json").resolve())
        payload["model"]["aliases_file"] = str((ROOT / "config" / "chip_aliases.json").resolve())
        payload["topologies"]["presets"] = {
            name: str((CA_PROFILE.parent / reference).resolve())
            for name, reference in payload["topologies"]["presets"].items()
        }
        return payload

    def test_header_alias_changes_without_pipeline_code(self) -> None:
        payload = self.portable_profile(json.loads(CA_PROFILE.read_text(encoding="utf-8")))
        payload["dictionary"]["aliases"]["flash_kb"] = ["定制程序容量"]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "profile.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            profile = load_profile(path)
            try:
                ca_miai.configure(profile)
                self.assertEqual(ca_miai.semantic_columns({3: "定制程序容量"}), {"flash": 3})
            finally:
                ca_miai.configure(load_profile(CA_PROFILE))

    def test_model_format_changes_only_in_profile(self) -> None:
        payload = self.portable_profile(json.loads(CA_PROFILE.read_text(encoding="utf-8")))
        payload["model"]["pattern"] = r"ZX\d{2}"
        payload["model"]["concatenation_boundary"] = r"(?=ZX)"
        payload["model"]["strip_prefixes"] = []
        payload["model"]["skip_suffixes"] = []
        payload["model"]["exact"] = [{"prefix": "ZX", "min_total_length": 4, "require_digit": True}]
        payload["model"]["series"] = [{"kind": "prefix", "prefix": "ZX"}]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "profile.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            profile = load_profile(path)
            self.assertEqual(profile.model_tokens("new model ZX12", exact_only=True), ["ZX12"])

    def test_unit_factor_changes_only_in_config(self) -> None:
        config = {"aliases": {"箱": "BOX"}, "factors": {"BOX": 7}}
        self.assertEqual(convert_number("2箱", config), 14)

    def test_compact_profile_rejects_old_rule_and_fixed_position_details(self) -> None:
        payload = self.portable_profile(json.loads(CA_PROFILE.read_text(encoding="utf-8")))
        for key, value in (("rules_file", "old.json"), ("page", 10), ("expected_value", 16)):
            candidate = copy.deepcopy(payload)
            candidate["model"][key] = value
            with tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "profile.json"
                path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "implementation details"):
                    load_profile(path)

    def test_runtime_source_does_not_reference_legacy_rule_files(self) -> None:
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "src").rglob("*.py")
        )
        self.assertNotIn("parameter_rules.json", source)
        self.assertNotIn("stm32_parameter_rules.json", source)


if __name__ == "__main__":
    unittest.main()
