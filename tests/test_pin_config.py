from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.pin_config import load_pin_recipe


class PinConfigTests(unittest.TestCase):
    def test_shared_pin_topologies_load(self) -> None:
        recipe = load_pin_recipe(ROOT / "config" / "table_presets" / "pin_definition_af.json")
        self.assertEqual(recipe["topologies"]["af_matrix"]["header_pattern"], "^AF\\d+$")

    def test_missing_topology_has_no_legacy_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "pin.json"
            path.write_text(json.dumps({"topologies": {"pin_definition": {}}}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must define"):
                load_pin_recipe(path)


if __name__ == "__main__":
    unittest.main()
