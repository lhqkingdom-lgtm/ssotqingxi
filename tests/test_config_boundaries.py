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
CA_RULES = ROOT / "config" / "parameter_rules.json"


class ConfigurationBoundaryTests(unittest.TestCase):
    def test_header_alias_changes_without_pipeline_code(self) -> None:
        profile = load_profile(CA_PROFILE)
        original = json.loads(CA_RULES.read_text(encoding="utf-8"))
        changed = copy.deepcopy(original)
        changed["header_aliases"]["flash"] = ["定制程序容量"]
        try:
            ca_miai.configure(profile, changed)
            self.assertEqual(ca_miai.semantic_columns({3: "定制程序容量"}), {"flash": 3})
        finally:
            ca_miai.configure(profile, original)

    def test_model_format_changes_only_in_profile(self) -> None:
        payload = json.loads(CA_PROFILE.read_text(encoding="utf-8"))
        payload["patterns"]["model"] = r"ZX\d{2}"
        payload["patterns"]["concatenation_boundary"] = r"(?=ZX)"
        payload["model_rules"] = {
            "strip_prefixes": [],
            "skip_suffixes": [],
            "exact": [{"prefix": "ZX", "min_total_length": 4, "require_digit": True}],
        }
        payload["series_rules"] = [{"kind": "prefix", "prefix": "ZX"}]
        payload["rules_file"] = str((ROOT / "config" / "parameter_rules.json").resolve())
        payload["aliases_file"] = str((ROOT / "config" / "chip_aliases.json").resolve())
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "profile.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            profile = load_profile(path)
            self.assertEqual(profile.model_tokens("new model ZX12", exact_only=True), ["ZX12"])

    def test_unit_factor_changes_only_in_config(self) -> None:
        config = {"aliases": {"箱": "BOX"}, "factors": {"BOX": 7}}
        self.assertEqual(convert_number("2箱", config), 14)


if __name__ == "__main__":
    unittest.main()
