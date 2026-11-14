from copy import deepcopy
from pathlib import Path
import tempfile
import json
import unittest

from rhythm_observatory.data import fingerprint, load_bundle, visible_snapshot


ROOT = Path(__file__).resolve().parents[1]


class DataTests(unittest.TestCase):
    def setUp(self):
        self.bundle = load_bundle(ROOT / "fixtures/bundle.json")

    def test_bundle_has_real_records(self):
        self.assertGreater(sum(len(rows) for rows in self.bundle["tables"].values()), 30)

    def test_future_record_not_visible(self):
        snapshot = visible_snapshot(self.bundle, "2024-01-01")
        self.assertTrue(all(row["released_at"] <= "2024-01-01" for rows in snapshot["tables"].values() for row in rows))

    def test_revision_replaces_same_identity(self):
        table = next(iter(self.bundle["tables"]))
        row = deepcopy(self.bundle["tables"][table][0])
        self.bundle["tables"][table].append({**row, "revision": 99, "released_at": "2025-12-31", "changed": True})
        older = visible_snapshot(self.bundle, "2025-01-01")
        newer = visible_snapshot(self.bundle, "2026-01-01")
        self.assertFalse(any(value.get("changed") for value in older["tables"][table]))
        self.assertEqual(sum(value["id"] == row["id"] for value in newer["tables"][table]), 1)

    def test_withdrawal_excludes_latest(self):
        table = next(iter(self.bundle["tables"]))
        row = self.bundle["tables"][table][0]
        self.bundle["tables"][table].append({**row, "revision": 999, "released_at": "2025-12-31", "withdrawn": True})
        newer = visible_snapshot(self.bundle, "2026-01-01")
        self.assertNotIn(row["id"], [value["id"] for value in newer["tables"][table]])

    def test_source_bundle_not_mutated(self):
        before = fingerprint(self.bundle)
        snapshot = visible_snapshot(self.bundle, "2026-01-01")
        next(iter(snapshot["tables"].values())).clear()
        self.assertEqual(fingerprint(self.bundle), before)

    def test_duplicate_revision_is_rejected(self):
        table = next(iter(self.bundle["tables"]))
        self.bundle["tables"][table].append(self.bundle["tables"][table][0])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bundle.json"
            path.write_text(json.dumps(self.bundle), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_bundle(path)
