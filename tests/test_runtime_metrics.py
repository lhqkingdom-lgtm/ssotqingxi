from __future__ import annotations

import unittest

from core.runtime_metrics import format_memory, process_memory_mb, release_process_working_set


class RuntimeMetricsTests(unittest.TestCase):
    def test_working_set_release_is_best_effort_boolean(self) -> None:
        self.assertIsInstance(release_process_working_set(), bool)
    def test_memory_metric_is_valid_when_available(self) -> None:
        values = process_memory_mb()
        if values is None:
            self.skipTest("process memory metrics unavailable on this platform")
        self.assertGreater(values["rss_mb"], 0)
        self.assertGreaterEqual(values["peak_rss_mb"], values["rss_mb"])

    def test_memory_log_has_stable_prefix(self) -> None:
        self.assertTrue(format_memory("checkpoint").startswith("  [memory] checkpoint"))


if __name__ == "__main__":
    unittest.main()
