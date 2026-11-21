#!/usr/bin/env python3
"""Audit verification tests covering every bug fix and optimization added during review."""
import contextlib
import email.message
import io
import json
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import file_lock  # noqa: E402
import reddit_inbox_state as inbox  # noqa: E402
import reddit_monitor_tick_noagent as monitor  # noqa: E402

STAMP_1 = "2026-11-22T00:00:00Z"
STAMP_2 = "2026-11-22T01:00:00Z"


class AuditOptimizationTests(unittest.TestCase):
    def test_cross_platform_nonblocking_lock_contention(self):
        with tempfile.TemporaryDirectory() as td:
            lock_path = Path(td) / "run.lock"
            with file_lock.acquire_lock_file(lock_path) as first:
                first.write_owner("owner-1\n")
                with self.assertRaisesRegex(RuntimeError, "already active"):
                    file_lock.acquire_lock_file(lock_path)
            # Re-acquiring after release must succeed cleanly.
            with file_lock.acquire_lock_file(lock_path) as second:
                second.write_owner("owner-2\n")

    def test_word_hit_eliminates_false_positives_on_domain_terms_and_acronyms(self):
        # Previously 'psi' matched 'upside'/'pepsi' and 'pump' matched 'pumpkin'!
        self.assertFalse(monitor.word_hit("Is there any upside to buying Pepsi?", "psi"))
        self.assertFalse(monitor.word_hit("Carving a pumpkin for Halloween", "pump"))
        self.assertFalse(monitor.word_hit("Rapid transit schedule", "api"))
        self.assertTrue(monitor.word_hit("Rated at 3000 psi max", "psi"))
        self.assertTrue(monitor.word_hit("Two pumps in series", "pump"))
        self.assertTrue(monitor.word_hit("REST API timeout error", "api"))

        unrelated_post = {
            "title": "Help?",
            "selftext": "Is there any upside to buying Pepsi for a pumpkin party?",
            "flair": "Question",
        }
        fit, reason = monitor.candidate_fit(unrelated_post, [], discovery_route="generic_help")
        self.assertFalse(fit)
        self.assertEqual(reason, "unrelated body without relevant technical context")

    def test_configurable_watch_domain_terms_for_non_hardware_communities(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            cache_path = Path(td) / "cache.json"
            state_path.write_text(json.dumps({
                "watch": [{
                    "name": "python-dev",
                    "url": "https://old.reddit.com/r/learnpython/new/",
                    "keywords": ["asyncio", "fastapi"],
                    "domainTerms": ["coroutine", "event loop", "pydantic", "uvicorn"],
                    "maxAgeHours": 48,
                    "maxCandidates": 5,
                }],
                "seen": {"byPermalink": {}},
            }), encoding="utf-8")
            posts = [
                {
                    "permalink": "/r/learnpython/comments/py1/help/",
                    "title": "Any advice?",
                    "selftext": "My coroutine blocks the event loop under load",
                    "num_comments": 0,
                    "created_utc": time.time() - 30,
                    "subreddit": "learnpython",
                    "author": "dev1",
                },
                {
                    "permalink": "/r/learnpython/comments/py2/help/",
                    "title": "Help?",
                    "selftext": "My pressure washer unloader is leaking water",
                    "num_comments": 0,
                    "created_utc": time.time() - 20,
                    "subreddit": "learnpython",
                    "author": "dev2",
                },
            ]

            def mock_ctx(url):
                pid = url.split("/comments/")[1].split("/")[0]
                src = next(p for p in posts if f"/comments/{pid}/" in p["permalink"])
                return {**src, "comments_coverage": {"truncated": False, "has_more": False, "unreadable_comments": False}}, [], "mock"

            with (
                patch.object(monitor, "_storage_paths", return_value=(state_path, cache_path)),
                patch.object(monitor, "fetch_listing", return_value=(posts, "mock")),
                patch.object(monitor, "fetch_context", side_effect=mock_ctx),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                monitor.run_monitor()

            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual([c["permalink"] for c in cache["candidates"]], ["https://old.reddit.com/r/learnpython/comments/py1/help/"])
            self.assertEqual(len(cache["skipped"]), 1)
            self.assertEqual(cache["skipped"][0]["action"], "skipped_low_fit")

    def test_context_time_crosspost_dedup_not_marked_failed_or_burning_retries(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = Path(td) / "state.json"
            cache_path = Path(td) / "cache.json"
            state_path.write_text(json.dumps({
                "watch": [{"name": "test", "url": "https://old.reddit.com/r/test/new/", "keywords": ["hose"], "maxAgeHours": 48, "maxCandidates": 5}],
                "seen": {"byPermalink": {}},
            }), encoding="utf-8")
            # At listing time, crosspost relation is hidden; revealed only at fetch_context time.
            posts = [
                {"permalink": "/r/test/comments/orig1/help/", "title": "Hose leak help?", "selftext": "Need fitting", "num_comments": 0, "created_utc": time.time() - 20, "subreddit": "test", "author": "u1"},
                {"permalink": "/r/test/comments/cross1/help/", "title": "Hose leak help?", "selftext": "Need fitting", "num_comments": 0, "created_utc": time.time() - 30, "subreddit": "test", "author": "u1"},
            ]

            def mock_ctx(url):
                if "/orig1/" in url:
                    p = {**posts[0], "comments_coverage": {"truncated": False, "has_more": False, "unreadable_comments": False}}
                else:
                    p = {
                        **posts[1],
                        "crosspost_original": "https://old.reddit.com/r/test/comments/orig1/help/",
                        "comments_coverage": {"truncated": False, "has_more": False, "unreadable_comments": False},
                    }
                return p, [], "mock"

            with (
                patch.object(monitor, "_storage_paths", return_value=(state_path, cache_path)),
                patch.object(monitor, "fetch_listing", return_value=(posts, "mock")),
                patch.object(monitor, "fetch_context", side_effect=mock_ctx),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                monitor.run_monitor()

            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            state = json.loads(state_path.read_text(encoding="utf-8"))
            # 1 candidate, 1 explicit dedup in skipped, 0 context failures!
            self.assertEqual(len(cache["candidates"]), 1)
            self.assertEqual(len(cache["skipped"]), 1)
            self.assertEqual(cache["skipped"][0]["action"], "skipped_duplicate_thread")
            self.assertEqual(cache["scan"]["contextFetchFailures"], 0)
            self.assertEqual(cache["scan"]["contextDedupCount"], 1)
            self.assertEqual(
                sum(cache["scan"]["newSelectedDiscussionCounts"].values()),
                len(cache["candidates"]) + len(cache["skipped"]),
            )
            # Surviving candidate starts at retryCount == 0; deduped crosspost is NOT marked failed_agent!
            orig_key = "https://old.reddit.com/r/test/comments/orig1/help/"
            cross_key = "https://old.reddit.com/r/test/comments/cross1/help/"
            self.assertEqual(state["seen"]["byPermalink"][orig_key]["retryCount"], 0)
            self.assertNotIn(cross_key, state["seen"]["byPermalink"])

    def test_summary_reconciles_scan_counts_and_detects_evidence_or_state_drift(self):
        with tempfile.TemporaryDirectory() as td:
            sp, cp, rp = (Path(td) / n for n in ("state.json", "cache.json", "review.json"))
            key1 = "https://old.reddit.com/r/test/comments/c1/help/"
            key2 = "https://old.reddit.com/r/test/comments/c2/help/"
            c1 = {"permalink": key1, "candidate_origin": "new", "discussion_status": "zero_comments", "selftext": "original evidence"}
            c2 = {"permalink": key2, "candidate_origin": "new", "discussion_status": "zero_comments", "selftext": "second post"}
            sp.write_text(json.dumps({
                "contentRunId": "r1",
                "seen": {"byPermalink": {
                    key1: {"action": "candidate_pending_semantic_review"},
                    key2: {"action": "candidate_pending_semantic_review"},
                }},
            }), encoding="utf-8")
            cp.write_text(json.dumps({
                "contentRunId": "r1",
                "scan": {"newCandidateCount": 2, "retryCandidateCount": 0, "newCandidateDiscussionCounts": {"zero_comments": 2, "has_comments": 0, "unknown": 0}},
                "candidates": [c1, c2],
                "skipped": [],
            }), encoding="utf-8")
            rp.write_text(json.dumps({"contentRunId": "r1", "permalink": key1, "result": "recommended", "notes": "helpful"}), encoding="utf-8")
            monitor.apply_review_result(rp, sp, cp)

            # Valid summary succeeds
            summary = monitor.review_summary(sp, cp)
            self.assertEqual(summary["outcomesByOriginAndContextDiscussion"]["new"]["zero_comments"]["recommended"], 1)

            # 1) Deleting a candidate from cache while scan.newCandidateCount==2 must fail
            tampered_cache = json.loads(cp.read_text(encoding="utf-8"))
            tampered_cache["candidates"] = [c1]
            cp.write_text(json.dumps(tampered_cache), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "newCandidateCount"):
                monitor.review_summary(sp, cp)

            # 2) Restoring count but mutating reviewed candidate's evidence must fail fingerprint check
            tampered_cache["candidates"] = [{**c1, "selftext": "altered after review"}, c2]
            cp.write_text(json.dumps(tampered_cache), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "fingerprint drift"):
                monitor.review_summary(sp, cp)

            # 3) Restoring cache but mutating state reviewStatus must fail snapshot check
            tampered_cache["candidates"] = [c1, c2]
            cp.write_text(json.dumps(tampered_cache), encoding="utf-8")
            tampered_state = json.loads(sp.read_text(encoding="utf-8"))
            tampered_state["seen"]["byPermalink"][key1]["reviewNotes"] = "tampered note"
            sp.write_text(json.dumps(tampered_state), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "snapshot"):
                monitor.review_summary(sp, cp)

    def test_archive_cli_creates_journal_removes_manual_pending_and_blocks_protected(self):
        with tempfile.TemporaryDirectory() as td:
            sp, cp, ap = (Path(td) / n for n in ("state.json", "cache.json", "archive.json"))
            manual_key = "https://old.reddit.com/r/test/comments/m1/help/"
            active_key = "https://old.reddit.com/r/test/comments/a1/help/"
            posted_key = "https://old.reddit.com/r/test/comments/p1/help/"
            manual_entry = {"action": "pending_manual", "reviewStatus": "needs_manual_review", "reviewNotes": "missing photo", "manualReviewCount": 3}
            sp.write_text(json.dumps({
                "contentRunId": "run-arch",
                "seen": {"byPermalink": {
                    manual_key: manual_entry,
                    active_key: {"action": "candidate_pending_semantic_review"},
                    posted_key: {"action": "posted"},
                }},
                "inbox": {"keep": True},
            }), encoding="utf-8")
            cp.write_text(json.dumps({
                "contentRunId": "run-arch",
                "candidates": [{"permalink": active_key, "candidate_origin": "new", "discussion_status": "zero_comments"}],
                "manual_pending": [{"permalink": manual_key, "title": "Old item", "reason": "retry_limit", "status": "pending_manual"}],
            }), encoding="utf-8")

            # Refuses archiving active candidate or protected entry
            for bad_target in (active_key, posted_key):
                ap.write_text(json.dumps({"userApproved": True, "reason": "cleanup", "contentRunId": "run-arch", "targets": [bad_target]}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    monitor.archive_entries(ap, sp, cp)

            # Succeeds with exact target + fingerprint check
            expected_sha = monitor.candidate_fingerprint(manual_entry)
            ap.write_text(json.dumps({
                "userApproved": True,
                "reason": "user approved archive after 3 manual reviews",
                "contentRunId": "run-arch",
                "targets": [{"permalink": manual_key, "expectedSha256": expected_sha}],
            }), encoding="utf-8")
            res = monitor.archive_entries(ap, sp, cp)
            self.assertEqual(res["archivedCount"], 1)
            self.assertTrue((Path(res["journalDir"]) / "state.before.json").exists())
            saved_state = json.loads(sp.read_text(encoding="utf-8"))
            saved_cache = json.loads(cp.read_text(encoding="utf-8"))
            archived_item = saved_state["seen"]["byPermalink"][manual_key]
            self.assertEqual(archived_item["archive"]["status"], "archived")
            self.assertEqual(archived_item["reviewNotes"], "missing photo")
            self.assertEqual(saved_cache["manual_pending"], [])
            self.assertEqual(len(saved_cache["candidates"]), 1)

    def test_camofox_retry_after_and_tab_id_quoting(self):
        env = {
            "REDDIT_EXPECTED_USER": "FixtureReader",
            "REDDIT_MONITOR_AUTHORIZED": "1",
            "CAMOFOX_URL": "http://camofox.example.invalid",
            "CAMOFOX_USER_ID": "fixture-browser",
            "CAMOFOX_SESSION_KEY": "fixture-group",
        }
        headers = email.message.Message()
        headers["Retry-After"] = "7"
        err_429 = urllib.error.HTTPError("http://camofox.example.invalid/tabs", 429, "Too Many Requests", headers, None)
        huge_headers = email.message.Message()
        huge_headers["Retry-After"] = "120"
        err_huge = urllib.error.HTTPError("http://camofox.example.invalid/tabs", 429, "Too Many Requests", huge_headers, None)

        class FakeResp:
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return b'{"ok": true, "tabs": []}'

        with patch.dict("os.environ", env, clear=True):
            with patch.object(monitor.urllib.request, "urlopen", side_effect=[err_429, FakeResp()]), patch.object(monitor.time, "sleep") as sleep_mock:
                data = monitor.camofox_request("/tabs")
                self.assertTrue(data["ok"])
                sleep_mock.assert_called_once_with(7.0)
            with patch.object(monitor.urllib.request, "urlopen", side_effect=err_huge), patch.object(monitor.time, "sleep") as sleep_mock:
                with self.assertRaisesRegex(RuntimeError, "Retry-After"):
                    monitor.camofox_request("/tabs")
                sleep_mock.assert_not_called()

            # Verify unsafe tabId characters are percent-encoded in path
            monitor._CAMOFOX_TAB_ID = "tab/../weird id"
            try:
                with patch.object(monitor, "camofox_request", return_value={"ok": True}) as req_mock:
                    monitor.camofox_navigate("https://old.reddit.com/r/test/new/")
                    called_path = req_mock.call_args[0][0]
                    self.assertIn("tab%2F..%2Fweird%20id", called_path)
                    self.assertNotIn("/../", called_path)
            finally:
                monitor._CAMOFOX_TAB_ID = None

    def test_inbox_stale_a_b_a_replay_url_alias_protection_and_verified_source(self):
        with tempfile.TemporaryDirectory() as td, patch.dict("os.environ", {"REDDIT_MONITOR_DIR": td, "REDDIT_EXPECTED_USER": "FixtureReader"}, clear=True):
            state_path = Path(td) / "state.json"
            state_path.write_text(json.dumps({"seen": {"byPermalink": {}}}), encoding="utf-8")

            posted_item = {
                "source": "old-reddit-messages",
                "from": "asker",
                "action": "posted_reply",
                "author": "FixtureReader",
                "commentId": "t1_out1",
                "parentCommentId": "t1_in1",
                "permalink": "https://www.reddit.com/r/test/comments/p1/slug_v1/in1/",
                "commentPermalink": "https://old.reddit.com/r/test/comments/p1/slug_v1/out1/",
                "replyText": "Check the O-ring first.",
                "composerReadback": "Check the O-ring first.",
                "postReloadReadback": "Check the O-ring first.",
                "handledAt": STAMP_1,
                "verification": {"author": True, "text": True, "parent": True, "comment": True},
            }

            # 1) All-unverified sources with postedReplies must be rejected
            unverified_batch = {
                "runId": "run-bad",
                "inboxRunId": "run-bad",
                "checkedAt": STAMP_1,
                "sources": {s: {"status": "unverified", "checkedAt": STAMP_1, "count": None, "coverage": "none"} for s in inbox.ALLOWED_SOURCES},
                "actionable": [], "noise": [], "handled": [], "skippedInsufficientInfo": [], "deferred": [],
                "postedReplies": [posted_item],
            }
            bad_file = Path(td) / "bad.json"
            bad_file.write_text(json.dumps(unverified_batch), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "verified source"):
                inbox.persist(bad_file)

            # 2) Duplicate target in postedReplies must be rejected
            verified_sources = {
                "old-reddit-messages": {"status": "verified", "checkedAt": STAMP_1, "count": 1, "coverage": "page 1"},
                "new-reddit-notifications": {"status": "unverified", "checkedAt": STAMP_1, "count": None, "coverage": "skipped"},
                "reddit-chat": {"status": "unverified", "checkedAt": STAMP_1, "count": None, "coverage": "skipped"},
            }
            dup_batch = {
                **unverified_batch,
                "sources": verified_sources,
                "postedReplies": [
                    posted_item,
                    {**posted_item, "commentId": "t1_out2", "commentPermalink": "https://old.reddit.com/r/test/comments/p1/slug_v1/out2/"},
                ],
            }
            bad_file.write_text(json.dumps(dup_batch), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate target"):
                inbox.persist(bad_file)

            # 3) Batch A succeeds with posted_reply
            batch_a = {**unverified_batch, "runId": "run-A", "inboxRunId": "run-A", "sources": verified_sources, "postedReplies": [posted_item]}
            file_a = Path(td) / "a.json"
            file_a.write_text(json.dumps(batch_a), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                inbox.persist(file_a)

            # 4) Batch B tries to send `no_reply` for the SAME inbound comment (`t1_in1`) under a DIFFERENT URL slug (`slug_v2`)
            batch_b = {
                "runId": "run-B",
                "inboxRunId": "run-B",
                "checkedAt": STAMP_2,
                "sources": {**verified_sources, "old-reddit-messages": {**verified_sources["old-reddit-messages"], "checkedAt": STAMP_2}},
                "actionable": [],
                "noise": [{
                    "source": "old-reddit-messages",
                    "action": "no_reply",
                    "commentId": "t1_in1",
                    "permalink": "https://old.reddit.com/r/test/comments/p1/slug_v2_renamed/in1/",
                }],
                "handled": [], "skippedInsufficientInfo": [], "postedReplies": [], "deferred": [],
            }
            file_b = Path(td) / "b.json"
            file_b.write_text(json.dumps(batch_b), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                inbox.persist(file_b)

            saved = json.loads(state_path.read_text(encoding="utf-8"))
            # Must NOT create a duplicate handled entry and must preserve posted_reply!
            self.assertEqual(len(saved["inbox"]["handled"]), 1)
            only_entry = next(iter(saved["inbox"]["handled"].values()))
            self.assertEqual(only_entry["action"], "posted_reply")

            # 5) Replaying stale Batch A (`A -> B -> A`) must be rejected so watermark stays at B!
            with self.assertRaisesRegex(RuntimeError, "stale inbox batch replay"):
                inbox.persist(file_a)
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["inbox"]["inboxRunId"], "run-B")


if __name__ == "__main__":
    unittest.main()
