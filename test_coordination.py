import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_pr_state as support


ROOT = Path(__file__).parent
spec = importlib.util.spec_from_file_location("coordinated_pr_state", ROOT / "pr_state.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="pr-state-coordination-test-")
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        cache_patch = patch.object(module, "cache_root", return_value=self.root)
        cache_patch.start()
        self.addCleanup(cache_patch.stop)
        clock_patch = patch.object(module.time, "time", return_value=1_800_000_000)
        self.clock = clock_patch.start()
        self.addCleanup(clock_patch.stop)
        self.state = self.observation()

    def observation(self, slug="example/api", number=123, target="dev"):
        data = support.fixture()
        pr = support.pull(data)
        pr["number"] = number
        pr["baseRefName"] = target
        return module.normalize(data, self.clock.return_value, slug)

    def claim(self, state=None, owner="worker-a", **kwargs):
        return module.coordination(state or self.state, claim=owner,
                                   contact=f"local:{owner}", **kwargs)

    def test_ownership_renewal_and_offline_release(self):
        created = self.claim(after=("example/api#124",), onto="d" * 40)["claim"]
        self.assertEqual(created["pr"], "example/api#123")
        self.assertEqual(created["owner"], "worker-a")
        self.assertEqual(created["contact"], "local:worker-a")
        self.assertEqual(created["head"], self.state["head"])
        self.assertEqual(created["recorded_base"], self.state["recorded_base"])
        self.assertEqual(created["target_tip"], self.state["target_tip"])
        self.assertEqual(created["after"], ["example/api#124"])
        self.assertEqual(created["onto"], "d" * 40)
        self.assertEqual(created["expires_at"] - created["updated_at"], 900)
        with self.assertRaises(module.StateError): self.claim(owner="worker-b")
        self.clock.return_value += 60
        renewed = self.claim()["claim"]
        self.assertEqual(renewed["updated_at"], self.clock.return_value)
        self.assertEqual(renewed["expires_at"], self.clock.return_value + 900)
        with self.assertRaises(module.StateError):
            module.release_claim("example/api", 123, "worker-b")
        with patch.object(module, "observe", side_effect=AssertionError("release must work offline")):
            self.assertTrue(module.release_claim("example/api", 123, "worker-a"))
            self.assertFalse(module.release_claim("example/api", 123, "worker-a"))
        self.assertIsNone(module.coordination(self.state)["claim"])

    def test_stale_snapshot_and_expired_claim_can_be_replaced(self):
        self.claim(lease=30)
        changed = copy.deepcopy(self.state)
        changed.update(head="e" * 40, target_branch="release", target_tip="f" * 40)
        record = module.coordination(changed)["claim"]
        self.assertFalse(record["expired"])
        self.assertFalse(record["head_matches"])
        self.assertFalse(record["target_matches"])
        self.assertFalse(record["target_tip_matches"])
        self.clock.return_value += 31
        self.assertTrue(module.coordination(self.state)["claim"]["expired"])
        replaced = self.claim(owner="worker-b")["claim"]
        self.assertEqual(replaced["owner"], "worker-b")
        self.assertFalse(replaced["expired"])
        self.assertTrue(replaced["head_matches"])
        self.assertTrue(replaced["target_matches"])
        self.assertTrue(replaced["target_tip_matches"])

    def test_peers_are_scoped_and_landing_intent_is_advisory(self):
        self.claim(self.observation(number=124), owner="worker-b", intent="landing", lease=30)
        self.claim(self.observation(number=125, target="release"), owner="worker-c", intent="landing")
        self.claim(self.observation(slug="example/other", number=126), owner="worker-d", intent="landing")
        result = self.claim(intent="landing")
        self.assertEqual([peer["pr"] for peer in result["peers"]], ["example/api#124"])
        self.assertTrue(result["landing_conflict"])
        self.assertEqual(result["claim"]["expires_at"] - result["claim"]["updated_at"], 300)
        self.assertEqual(result["claim"]["intent"], "landing")
        retargeted = module.coordination(self.observation(target="release"))
        self.assertFalse(retargeted["claim"]["target_matches"])
        self.assertEqual([peer["pr"] for peer in retargeted["peers"]], ["example/api#125"])
        self.assertFalse(retargeted["landing_conflict"])
        self.clock.return_value += 31
        result = module.coordination(self.state)
        self.assertEqual(result["peers"], [])
        self.assertFalse(result["landing_conflict"])
        self.assertEqual(result["claim"]["owner"], "worker-a")

    def test_claim_requires_contact_and_bounded_lease(self):
        with self.assertRaises(module.StateError): module.coordination(self.state, claim="worker-a")
        for lease in (29, 3601):
            with self.subTest(lease=lease), self.assertRaises(module.StateError): self.claim(lease=lease)
        self.assertEqual(self.claim(lease=3600)["claim"]["expires_at"] - self.clock.return_value, 3600)
        incomplete = dict(self.state, target_tip=None)
        with self.assertRaises(module.StateError): self.claim(incomplete)

    def test_private_files_and_failed_atomic_update_preserve_record(self):
        self.claim()
        records = list(self.root.rglob("*.json"))
        self.assertTrue(records, "claim should be persisted as private JSON")
        before = {path: path.read_bytes() for path in records}
        for path in self.root.rglob("*"):
            self.assertEqual(path.stat().st_mode & 0o077, 0, str(path))
        self.clock.return_value += 60
        with patch.object(module.os, "replace", side_effect=OSError("synthetic write failure")):
            with self.assertRaises((OSError, module.StateError)): self.claim()
        self.assertEqual({path: path.read_bytes() for path in records}, before)
        self.assertEqual(module.coordination(self.state)["claim"]["updated_at"], self.clock.return_value - 60)

    def test_corrupt_json_is_not_silently_overwritten(self):
        self.claim()
        records = list(self.root.rglob("*.json"))
        self.assertTrue(records)
        records[0].write_text("{not valid JSON")
        with self.assertRaises(module.StateError) as raised: module.coordination(self.state)
        self.assertIn(records[0].name, str(raised.exception))
        with self.assertRaises(module.StateError): self.claim()
        self.assertEqual(records[0].read_text(), "{not valid JSON")


if __name__ == "__main__":
    unittest.main()
