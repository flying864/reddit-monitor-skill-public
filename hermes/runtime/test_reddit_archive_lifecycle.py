"""Regression: explicit archived overlays never become active/reviewed automatically."""
import copy
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location("archive_fixture", Path(__file__).with_name("test_reddit_acceptance_repairs.py"))
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
m = a.m


class ArchiveLifecycleTests(unittest.TestCase):
    def test_overlay_preserves_original_review_and_blocks_pending(self):
        entry = {
            "action": "pending_manual",
            "reviewStatus": "needs_manual_review",
            "reviewNotes": "keep evidence",
            "archive": {"status": "archived", "automaticReview": False, "reminders": False},
        }
        before = copy.deepcopy(entry)
        self.assertTrue(m.archived_entry(entry))
        self.assertTrue(m.terminal_entry(entry))
        self.assertFalse(m.pending_entry(entry))
        self.assertEqual(entry, before)
        self.assertTrue(m.pending_entry({"action": "pending_manual"}))

    def test_review_cannot_reopen_even_if_old_cache_contains_target(self):
        helper = a.IndependentAcceptance()
        with tempfile.TemporaryDirectory() as td:
            sp, cp, rp, _ = helper.review_fixture(td, {"archive": {"status": "archived"}})
            before = (sp.read_bytes(), cp.read_bytes())
            with self.assertRaisesRegex(ValueError, "archived"):
                m.apply_review_result(rp, sp, cp)
            self.assertEqual(before, (sp.read_bytes(), cp.read_bytes()))

    def test_two_rounds_no_retry_or_reminder_and_keep_other_candidate(self):
        target = a.post("arch1")
        fresh = a.post("fresh1")
        archived = {
            target["permalink"]: {
                **target,
                "action": "pending_manual",
                "reviewStatus": "needs_manual_review",
                "reviewNotes": "historical reason",
                "retryCount": 3,
                "manualReviewCount": 3,
                "archive": {"status": "archived", "automaticReview": False, "reminders": False},
            }
        }
        fixture = a.IndependentAcceptance()
        for _ in range(2):
            _, cache, state, _ = fixture.run_monitor_case([target, fresh], seen=archived)
            self.assertNotIn(target["permalink"], [x["permalink"] for x in cache["candidates"]])
            self.assertNotIn(target["permalink"], [x["permalink"] for x in cache["manual_pending"]])
            self.assertEqual([x["permalink"] for x in cache["candidates"]], [fresh["permalink"]])
            self.assertEqual(state["seen"]["byPermalink"][target["permalink"]], archived[target["permalink"]])
            archived = state["seen"]["byPermalink"]
            archived.pop(fresh["permalink"])

    def test_crosspost_of_archived_thread_is_not_rediscovered(self):
        original = a.post("arch1")
        cross = a.post("cross1")
        cross["crosspost_parent_list"] = [{"permalink": original["permalink"]}]
        _, cache, _, _ = a.IndependentAcceptance().run_monitor_case(
            [cross],
            seen={original["permalink"]: {"action": "pending_manual", "archive": {"status": "archived"}}},
        )
        self.assertEqual(cache["candidates"], [])
        self.assertEqual(cache["manual_pending"], [])


if __name__ == "__main__":
    unittest.main()
