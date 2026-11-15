from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from rhythm_observatory.client import ProductClient, RemoteError
from rhythm_observatory.data import load_bundle
from rhythm_observatory.service import running_service
from rhythm_observatory.store import ArtifactStore
from rhythm_observatory.workflow import publish_overview, publish_rhythm


ROOT = Path(__file__).resolve().parents[1]


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        bundle = load_bundle(ROOT / "fixtures/bundle.json")
        cls.store = ArtifactStore(str(Path(cls.directory.name) / "state.sqlite3"), bundle.get("policy"))
        cls.running = running_service(bundle, cls.store)
        cls.client = ProductClient(cls.running.__enter__())

    @classmethod
    def tearDownClass(cls):
        cls.running.__exit__(None, None, None)
        cls.store.close()
        cls.directory.cleanup()

    def artifact(self):
        snapshot = self.client.snapshot("2026-01-01")
        return {"as_of": "2026-01-01", "snapshot_id": snapshot["snapshot_id"], "method": "observed-overview-v1", "result": {"count": 1}}

    def test_health_and_bad_date(self):
        self.assertEqual(self.client.request("/health")["status"], "ok")
        with self.assertRaises(RemoteError) as error:
            self.client.snapshot("wrong")
        self.assertEqual(error.exception.status, 400)

    def test_overview_roundtrip(self):
        value = publish_overview(self.client, "2026-01-01", "http-overview")
        self.assertEqual(self.client.artifact(value["artifact_id"]), value["artifact"])

    def test_rhythm_roundtrip_keeps_method_and_sources(self):
        value = publish_rhythm(self.client, "2026-01-01", "http-rhythm")
        artifact = value["artifact"]
        self.assertEqual(artifact["method"], "rhythm-sync-v1")
        restored = self.client.artifact(value["artifact_id"])
        self.assertEqual(restored, artifact)
        platforms = artifact["result"]["platforms"]
        self.assertTrue(platforms)
        self.assertTrue(all(platform["supporting_records"] for platform in platforms))
        first_record = platforms[0]["supporting_records"][0]
        self.assertIn("source_readings", first_record)
        self.assertIn("clock_records", first_record)
        guidance = artifact["result"]["scheduling_guidance"]
        self.assertTrue(all(item["guidance"] != "synchronized_schedule_supported"
                            or item["synchronization_status"] == "phase_consistent"
                            for item in guidance))

    def test_exact_request_replay(self):
        artifact = self.artifact()
        revision = self.client.policy()["revision"]
        first = self.client.publish("replay", artifact, revision)
        self.assertEqual(first, self.client.publish("replay", artifact, revision))

    def test_conflicting_request_body(self):
        artifact = self.artifact()
        revision = self.client.policy()["revision"]
        self.client.publish("conflict", artifact, revision)
        changed = deepcopy(artifact)
        changed["result"]["count"] = 2
        with self.assertRaises(RemoteError) as error:
            self.client.publish("conflict", changed, revision)
        self.assertEqual(error.exception.status, 409)

    def test_stale_policy_and_immutable_artifact(self):
        artifact = self.artifact()
        policy = self.client.policy()
        first = self.client.publish("before-change", artifact, policy["revision"])
        self.client.request("/policy", {"expected_revision": policy["revision"], "policy": {"blocked_items": ["newly-blocked"]}})
        with self.assertRaises(RemoteError) as error:
            self.client.publish("after-change", artifact, policy["revision"])
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.client.artifact(first["artifact_id"]), artifact)

    def test_forged_source_fingerprint(self):
        artifact = self.artifact()
        artifact["snapshot_id"] = "a" * 64
        with self.assertRaises(RemoteError) as error:
            self.client.publish("forged", artifact, self.client.policy()["revision"])
        self.assertEqual(error.exception.status, 400)

    def test_missing_artifact(self):
        with self.assertRaises(RemoteError) as error:
            self.client.artifact("missing")
        self.assertEqual(error.exception.status, 404)

    def test_persistence_after_reopen(self):
        path = str(Path(self.directory.name) / "separate.sqlite3")
        first = ArtifactStore(path)
        artifact = self.artifact()
        receipt = first.publish("persist", artifact, 1)
        first.close()
        second = ArtifactStore(path)
        try:
            self.assertEqual(second.get(receipt["artifact_id"]), artifact)
        finally:
            second.close()
