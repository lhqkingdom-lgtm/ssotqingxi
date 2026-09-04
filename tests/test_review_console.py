from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONSOLE = ROOT / "src" / "review_app" / "console"


class ReviewConsoleTests(unittest.TestCase):
    def test_console_has_document_and_batch_management(self):
        html = (CONSOLE / "index.html").read_text(encoding="utf-8")
        script = (CONSOLE / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="batchSearch"', html)
        self.assertIn('id="documentSearch"', html)
        self.assertIn('id="familyFilter"', html)
        self.assertIn('id="documentStatusFilter"', html)
        self.assertIn('id="duplicateFilter"', html)
        self.assertIn('id="documentRows"', html)
        self.assertIn('id="statusFilter"', html)
        self.assertIn('id="reviewFilter"', html)
        self.assertIn('id="batchRows"', html)
        self.assertIn('/api/review/batches', script)
        self.assertIn('/api/documents', script)
        self.assertIn('state.documentQuery = $("documentSearch").value', script)
        self.assertIn('window.location.href = item.review_url', script)
        self.assertIn('window.location.href = batch.review_url', script)
        self.assertIn('<option value="pending_export">待导出</option>', html)
        self.assertIn('<option value="exported">已导出</option>', html)
        self.assertNotIn("待同步", html)
        self.assertNotIn("已导出待确认", html)


if __name__ == "__main__":
    unittest.main()
