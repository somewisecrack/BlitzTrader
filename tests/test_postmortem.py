"""
tests/test_postmortem.py
------------------------
Focused tests for the post-mortem helper classification and alternate-root data lookup.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.postmortem import classify_symbol, find_csv_files, find_data_export_dir


class TestPostmortemHelpers(unittest.TestCase):

    def test_classify_symbol_variants(self):
        self.assertEqual(classify_symbol("NIFTY28APR26F"), "futures_tsym")
        self.assertEqual(classify_symbol("BANKNIFTY"), "bare_logical")
        self.assertEqual(classify_symbol("NIFTY24500CE"), "option")
        self.assertEqual(classify_symbol("SOMETHINGELSE"), "unknown")

    def test_find_data_export_dir_and_csv_files_under_alternate_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            export_dir = root / "data_exports" / "20260410"
            export_dir.mkdir(parents=True)
            (export_dir / "strategy_signals.csv").write_text("a,b\n1,2\n", encoding="utf-8")
            (export_dir / "indicators.csv").write_text("x,y\n3,4\n", encoding="utf-8")

            self.assertEqual(find_data_export_dir(root, __import__("datetime").date(2026, 4, 10)), export_dir)
            csvs = find_csv_files(root)
            names = sorted(p.name for p in csvs)
            self.assertEqual(names, ["indicators.csv", "strategy_signals.csv"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
