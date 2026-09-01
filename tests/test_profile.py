from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from core.profile import load_profile


PROFILE = Path(__file__).resolve().parents[1] / "config" / "profiles" / "ca_miai_v1.json"


class ProfileTests(unittest.TestCase):
    def test_current_model_rules(self) -> None:
        profile = load_profile(PROFILE)
        self.assertEqual(profile.model_tokens("SQ-CA51F950L3 / MiAi-6099A1", exact_only=True), ["CA51F950L3", "MiAi-6099A1"])
        self.assertEqual(profile.model_tokens("CA51Fxxx CA51F950XX", exact_only=True), [])
        self.assertFalse(profile.is_exact_model("CA51F950"))
        self.assertTrue(profile.is_exact_model("CA51F950L3"))

    def test_series_and_normalization(self) -> None:
        profile = load_profile(PROFILE)
        self.assertEqual(profile.series_for_model("MiAi-6099A1"), "MiAi-609")
        self.assertEqual(profile.series_for_model("CA32F0156L1"), "CA32F")
        self.assertEqual(profile.normalize_function("iic_scl"), "I2C_SCL")

    def test_invalid_profile_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "bad.json"
            path.write_text(json.dumps({"profile_id": "bad"}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing required keys"):
                load_profile(path)


if __name__ == "__main__":
    unittest.main()
