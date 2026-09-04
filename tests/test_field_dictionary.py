from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.field_dictionary import load_field_dictionary, load_table_recipe, match_common_field
from core.units import convert_number


DICTIONARY = ROOT / "config" / "common_field_dictionary.json"
RECIPE = ROOT / "config" / "table_presets" / "stm32_feature_matrix.json"


class CommonFieldDictionaryTests(unittest.TestCase):
    def test_common_and_family_aliases_share_one_matcher(self) -> None:
        dictionary = load_field_dictionary(DICTIONARY, {"ram_kb": ["片上工作区"]})
        self.assertEqual(match_common_field("Flash memory (Kbyte)", dictionary), "flash_kb")
        self.assertEqual(match_common_field("片上工作区 (Kbyte)", dictionary), "ram_kb")
        self.assertEqual(match_common_field("MTP Flash capacity", dictionary), "mtp_kb")

    def test_recipe_requires_and_receives_shared_fields_and_units(self) -> None:
        recipe = load_table_recipe(RECIPE, {"ram_kb": ["CCM RAM"]})
        self.assertIn("ccmram", [item.replace(" ", "").lower() for item in recipe["fields"]["ram_kb"]["rows"]])
        self.assertEqual(recipe["fields"]["flash_kb"]["unit"], "memory_kb")
        self.assertIn("frequency_mhz", recipe["units"])

    def test_unknown_family_alias_target_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown common field"):
            load_field_dictionary(DICTIONARY, {"vendor_magic": ["magic"]})

    def test_missing_dictionary_is_not_silently_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaisesRegex(ValueError, "not found"):
                load_field_dictionary(Path(temp_dir) / "missing.json")

    def test_recipe_without_dictionary_reference_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "recipe.json"
            path.write_text(json.dumps({"fields": {}}, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "no field_dictionary_file"):
                load_table_recipe(path)

    def test_shared_unit_conversion_is_explicit(self) -> None:
        dictionary = load_field_dictionary(DICTIONARY)
        memory = dictionary["units"]["memory_kb"]
        frequency = dictionary["units"]["frequency_mhz"]
        self.assertEqual(convert_number("2048 bytes", memory), 2)
        self.assertEqual(convert_number("2 MB", memory), 2048)
        self.assertEqual(convert_number("48000 kHz", frequency), 48)
        self.assertIsNone(convert_number("48", frequency))


if __name__ == "__main__":
    unittest.main()
