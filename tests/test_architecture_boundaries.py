from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GENERIC_ROOTS = (ROOT / "src" / "core", ROOT / "src" / "topologies")
FORBIDDEN = re.compile(r"\b(?:STM32|NXP|MIAI|TI|CA)\b", re.I)


class ArchitectureBoundaryTests(unittest.TestCase):
    def test_generic_layers_do_not_contain_document_family_names(self) -> None:
        violations = []
        for folder in GENERIC_ROOTS:
            for path in folder.rglob("*.py"):
                for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                    if FORBIDDEN.search(line):
                        violations.append(f"{path.relative_to(ROOT)}:{line_number}: {line.strip()}")
        self.assertEqual(violations, [], "document-family logic leaked into generic layers:\n" + "\n".join(violations))

    def test_topology_layer_does_not_apply_business_field_rules(self) -> None:
        forbidden = ("semantic_key", '.get("fields"', '.get("peripherals"')
        violations = []
        for path in (ROOT / "src" / "topologies").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for token in forbidden:
                if token in text:
                    violations.append(f"{path.relative_to(ROOT)} contains {token}")
        self.assertEqual(violations, [], "field rules leaked into topology layer:\n" + "\n".join(violations))


if __name__ == "__main__":
    unittest.main()
