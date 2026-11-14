from copy import deepcopy
from pathlib import Path
import json
import unittest

from rhythm_observatory.data import load_bundle, visible_snapshot
from rhythm_observatory.product import *


class ProductTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        self.snapshot = visible_snapshot(load_bundle(root / "fixtures/bundle.json"), "2026-01-01")

    def test_overview_is_finite_and_preserves_sources(self):
        original = deepcopy(self.snapshot)
        result = summarize(self.snapshot)
        self.assertTrue(result)
        self.assertIn("interpretation", result)
        json.dumps(result, allow_nan=False)
        self.assertEqual(self.snapshot, original)

    def test_counter_resets_are_not_negative_events(self):
        result = observed_increments(self.snapshot)
        self.assertTrue(result)
        self.assertTrue(all(row["count"] >= 0 for row in result))
        self.assertTrue(all(row["end_hour"] > row["start_hour"] for row in result))

    def test_clock_patch_is_selected_in_range(self):
        rows = clocked_readings(self.snapshot)
        patched = [row for row in rows if row["device"] == "d1" and 1100 <= row["raw_hour"] < 1800]
        self.assertTrue(patched)
        self.assertTrue(all(row["applied_clock"] == "clock-patch" for row in patched))
