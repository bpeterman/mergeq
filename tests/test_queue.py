import contextlib
import io
import tempfile
import unittest
from unittest.mock import Mock
from mergeq.cli import GitHub, QueueError, Store, main, pr_reference, step, worker


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(self.temp.name)
        self.repo = "someone/project"
        self.store.add(self.repo, 12, "main")
        self.entry = self.store.entries(self.repo)[0]
        self.pr = dict(number=12, state="OPEN", isDraft=False, baseRefName="main",
                       headRefOid="abc", mergeable="MERGEABLE", mergeStateStatus="CLEAN",
                       reviewDecision="APPROVED", url="https://github.com/someone/project/pull/12")
        self.gh = Mock()
        self.gh.pr.return_value = self.pr
        self.gh.checks.return_value = [{"name": "test", "bucket": "pass"}]

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def tick(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return step(self.gh, self.store, self.repo, self.entry, "squash")

    def test_ready_merges_with_exact_head_and_confirms_later(self):
        self.assertFalse(self.tick())
        self.gh.merge.assert_called_once_with(self.repo, 12, "abc", "squash")
        self.assertEqual(len(self.store.entries(self.repo)), 1)
        self.gh.pr.return_value = dict(self.pr, state="MERGED")
        self.assertTrue(self.tick())
        self.assertEqual(self.store.entries(self.repo), [])

    def test_already_merged_after_restart_is_not_merged_twice(self):
        self.gh.pr.return_value = dict(self.pr, state="MERGED")
        self.assertTrue(self.tick())
        self.gh.merge.assert_not_called()

    def test_behind_updates_before_looking_at_old_checks(self):
        self.gh.pr.return_value = dict(self.pr, mergeStateStatus="BEHIND")
        self.assertFalse(self.tick())
        self.gh.update.assert_called_once_with(self.repo, 12, "abc")
        self.gh.checks.assert_not_called()
        self.gh.merge.assert_not_called()

    def test_head_changed_during_checks_never_merges(self):
        self.gh.pr.side_effect = [self.pr, dict(self.pr, headRefOid="new")]
        self.assertFalse(self.tick())
        self.gh.merge.assert_not_called()

    def test_main_advanced_during_checks_never_merges(self):
        self.gh.pr.side_effect = [self.pr, dict(self.pr, mergeStateStatus="BEHIND")]
        self.assertFalse(self.tick())
        self.gh.merge.assert_not_called()

    def test_missing_and_pending_checks_wait(self):
        for checks in ([], [{"name": "test", "bucket": "pending"}]):
            self.gh.checks.return_value = checks
            self.assertFalse(self.tick())
        self.gh.merge.assert_not_called()

    def test_failures_and_conflicts_block(self):
        self.gh.checks.return_value = [{"name": "test", "bucket": "fail"}]
        with self.assertRaisesRegex(QueueError, "Required checks failed"):
            self.tick()
        self.gh.pr.return_value = dict(self.pr, mergeable="CONFLICTING")
        with self.assertRaisesRegex(QueueError, "Merge conflict"):
            self.tick()
        self.gh.merge.assert_not_called()

    def test_approvals_wait_and_changed_base_blocks(self):
        self.gh.pr.return_value = dict(self.pr, reviewDecision="REVIEW_REQUIRED")
        self.assertFalse(self.tick())
        self.gh.pr.return_value = dict(self.pr, baseRefName="release")
        with self.assertRaisesRegex(QueueError, "Target branch changed"):
            self.tick()
        self.gh.merge.assert_not_called()

    def test_stop_and_remove_prevent_merge(self):
        self.store.stop(self.repo, True)
        self.assertFalse(self.tick())
        self.store.stop(self.repo, False)
        self.store.set(self.entry, "removed")
        self.assertFalse(self.tick())
        self.gh.merge.assert_not_called()

    def test_persistence_deduplication_and_fifo(self):
        self.assertFalse(self.store.add(self.repo, 12, "main"))
        self.store.add(self.repo, 4, "main")
        other = Store(self.temp.name)
        self.assertEqual([e["number"] for e in other.entries(self.repo)], [12, 4])
        other.db.close()

    def test_lock_prevents_second_worker(self):
        other = Store(self.temp.name)
        with self.store.lock(self.repo):
            with self.assertRaisesRegex(QueueError, "already running"):
                with other.lock(self.repo):
                    pass
        other.db.close()

    def test_worker_stops_at_blocked_head(self):
        self.store.add(self.repo, 4, "main")
        self.store.set(self.entry, "blocked", "CI failed")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(worker(self.gh, self.store, self.repo, 1, 1, "squash"), 1)
        self.gh.pr.assert_not_called()
        self.assertEqual([e["number"] for e in self.store.entries(self.repo)], [12, 4])

    def test_api_error_preserves_and_blocks_head(self):
        self.gh.pr.side_effect = QueueError("Network unavailable")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(worker(self.gh, self.store, self.repo, 1, 1, "squash"), 1)
        self.assertEqual(self.store.entries(self.repo)[0]["status"], "blocked")

    def test_reference_validation(self):
        self.assertEqual(pr_reference("12", self.repo), 12)
        self.assertEqual(pr_reference("https://github.com/someone/project/pull/12", self.repo), 12)
        for value in ("0", "-1", "12; echo bad", "https://github.com/other/project/pull/12"):
            with self.assertRaises(QueueError):
                pr_reference(value, self.repo)


class ProtectionTests(unittest.TestCase):
    def setUp(self):
        self.gh = GitHub()
        self.gh.api = Mock()

    def test_strict_ruleset(self):
        self.gh.api.return_value = [{"type": "required_status_checks", "parameters": {
            "strict_required_status_checks_policy": True,
            "required_status_checks": [{"context": "test"}],
        }}]
        self.gh.require_protection("someone/project", "main")

    def test_classic_protection(self):
        self.gh.api.side_effect = [[], {"required_status_checks": {
            "strict": True, "contexts": ["test"],
        }}]
        self.gh.require_protection("someone/project", "main")

    def test_loose_or_unreadable_protection_rejected(self):
        for protection in ({}, {"required_status_checks": {"strict": False, "contexts": ["test"]}},
                           {"required_status_checks": {"strict": True, "contexts": []}}):
            self.gh.api.side_effect = [[], protection]
            with self.assertRaises(QueueError):
                self.gh.require_protection("someone/project", "main")
        self.gh.api.side_effect = [[], QueueError("403")]
        with self.assertRaisesRegex(QueueError, "Cannot verify"):
            self.gh.require_protection("someone/project", "main")

    def test_native_queue_rejected(self):
        self.gh.api.return_value = [{"type": "merge_queue"}]
        with self.assertRaisesRegex(QueueError, "Disable Require merge queue"):
            self.gh.require_protection("someone/project", "main")

    def test_merge_is_immediate_sha_guarded_api_request(self):
        self.gh.api.return_value = {"merged": True}
        self.gh.merge("someone/project", 12, "abc", "squash")
        self.gh.api.assert_called_once_with("repos/someone/project/pulls/12/merge", "PUT",
                                            sha="abc", merge_method="squash")
        self.gh.api.return_value = {"merged": False, "message": "blocked"}
        with self.assertRaisesRegex(QueueError, "blocked"):
            self.gh.merge("someone/project", 12, "abc", "squash")


if __name__ == "__main__":
    unittest.main()
