#!/usr/bin/env python3
"""Offline runtime and skill-template regressions; not deployment evidence."""
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import reddit_inbox_state as inbox  # noqa: E402
import reddit_monitor_tick_noagent as monitor  # noqa: E402

TEMPLATE = Path(__file__).resolve().parents[1] / "skill" / "scripts" / "scheduled_tick_persist_template.py"
STAMP = "2025-01-01T00:00:00Z"


class MonitorTests(unittest.TestCase):
    def test_normalization_and_fit(self):
        self.assertEqual(
            monitor.norm_permalink("https://www.reddit.com/r/test/comments/testpost/title/?context=3"),
            "https://old.reddit.com/r/test/comments/testpost/title/",
        )
        self.assertTrue(monitor.word_hit("my hose leaks", "hose"))
        self.assertFalse(monitor.word_hit("my house leaks", "hose"))
        fit, reason = monitor.candidate_fit({"title": "Hose reel for sale", "selftext": "what is this worth?"}, ["hose"])
        self.assertFalse(fit)
        self.assertIn("valuation", reason)
        self.assertTrue(monitor.candidate_fit({"title": "Quick connect will not lock", "selftext": "Any help?"}, ["quick connect"])[0])
        self.assertFalse(hasattr(monitor, "draft_for"))

    def test_verified_json_write(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "x.json"
            payload = {"ok": True, "items": [1, 2]}
            self.assertEqual(monitor.write_json_verified(path, payload), payload)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), payload)

    def test_live_cli_requires_each_setting_before_lock_or_network(self):
        config = {
            "REDDIT_MONITOR_AUTHORIZED": "1",
            "CAMOFOX_URL": "http://camofox.example.invalid",
            "CAMOFOX_USER_ID": "fixture-browser",
            "CAMOFOX_SESSION_KEY": "fixture-group",
            "REDDIT_EXPECTED_USER": "FixtureReader",
            "REDDIT_MONITOR_DIR": "unused",
        }
        for key in config:
            env = {**config, key: " "}
            with (
                self.subTest(key=key),
                patch.dict("os.environ", env, clear=True),
                patch.object(sys, "argv", ["monitor", "run"]),
                patch.object(monitor, "_acquire_lock_at") as lock,
                patch.object(monitor, "close_camofox_group") as cleanup,
                patch.object(monitor.urllib.request, "urlopen") as network,
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                with self.assertRaises(SystemExit) as result:
                    monitor.main()
                self.assertEqual(result.exception.code, 2)
                self.assertIn(key, output.getvalue())
                lock.assert_not_called()
                cleanup.assert_not_called()
                network.assert_not_called()

    def test_adapter_cannot_bypass_authorization(self):
        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(monitor.urllib.request, "urlopen") as network,
        ):
            with self.assertRaisesRegex(ValueError, "REDDIT_MONITOR_AUTHORIZED"):
                monitor.camofox_request("/tabs")
            network.assert_not_called()

    def test_regular_listing_checks_account(self):
        url = "https://old.reddit.com/r/fixturetools/new/"
        result = {"url": url, "posts": []}
        with (
            patch.dict("os.environ", {"REDDIT_EXPECTED_USER": "FixtureReader"}, clear=True),
            patch.object(monitor, "camofox_navigate"),
            patch.object(monitor, "camofox_evaluate", return_value=result),
        ):
            for account in (None, "", "FixtureOther"):
                result["logged_in_user"] = account
                with self.assertRaisesRegex(ValueError, "identity verification"):
                    monitor.fetch_listing(url)
            result["logged_in_user"] = "fixtureReader"
            self.assertEqual(monitor.fetch_listing(url)[0], [])


class InboxTests(unittest.TestCase):
    def test_local_persistence_and_idempotent_readback(self):
        with tempfile.TemporaryDirectory() as td, patch.dict("os.environ", {"REDDIT_MONITOR_DIR": td}, clear=True):
            state, result = (Path(td) / name for name in ("state.json", "result.json"))
            state.write_text(json.dumps({"contentRunId": "fixture-content", "seen": {"keep": True}}), encoding="utf-8")
            payload = {
                "runId": "fixture-inbox",
                "inboxRunId": "fixture-inbox",
                "checkedAt": STAMP,
                "sources": {
                    s: {"status": "unverified", "checkedAt": STAMP, "count": None, "coverage": "synthetic fixture"}
                    for s in inbox.ALLOWED_SOURCES
                },
                **{section: [] for section in inbox.ARRAYS},
            }
            result.write_text(json.dumps(payload), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as output:
                inbox.persist(result)
                inbox.persist(result)
                self.assertTrue(json.loads(output.getvalue().splitlines()[-1])["idempotent"])
            saved = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(saved["contentRunId"], "fixture-content")
            self.assertEqual(saved["seen"], {"keep": True})
            self.assertNotIn("lastCheckedAt", saved["inbox"])

    def test_posted_evidence_requires_matching_account(self):
        comment = {
            "source": "old-reddit-messages",
            "from": "FixtureSender",
            "action": "posted_reply",
            "author": "FixtureReader",
            "commentId": "t1_testreply",
            "parentCommentId": "t1_testparent",
            "commentPermalink": "https://old.reddit.com/r/test/comments/testpost/title/testreply/",
            "replyText": "Synthetic text.",
            "composerReadback": "Synthetic text.",
            "postReloadReadback": "Synthetic text.",
            "handledAt": STAMP,
            "verification": dict.fromkeys(("author", "text", "parent", "comment"), True),
        }
        for config in ({}, {"REDDIT_EXPECTED_USER": "FixtureOther"}):
            with patch.dict("os.environ", config, clear=True):
                with self.assertRaisesRegex(ValueError, "REDDIT_EXPECTED_USER"):
                    inbox.validate_posted(comment)
        with patch.dict("os.environ", {"REDDIT_EXPECTED_USER": "FixtureReader"}, clear=True):
            inbox.validate_posted(comment)


class SkillTemplateTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("fixture_persist_template", TEMPLATE)
        assert spec is not None and spec.loader is not None
        self.template = importlib.util.module_from_spec(spec)
        exec(compile(TEMPLATE.read_bytes(), str(TEMPLATE), "exec"), self.template.__dict__)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        root_patch = patch.object(self.template, "SKILL_ROOT", root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        self.fixture = root / ".persist-fixture-test"
        self.fixture.mkdir(mode=0o700)
        self.state, self.cache = (self.fixture / name for name in ("state.json", "cache.json"))
        self.state.write_text("{}", encoding="utf-8")
        self.args = {
            "state_path": self.state,
            "cache_path": self.cache,
            "run_id": "fixture-run",
            "draft_version": "test-version",
            "scan": {
                "range": "synthetic",
                "status": "success",
                "started_at": STAMP,
                "finished_at": STAMP,
                "source": "fixture",
                "requested": ["fixture"],
                "succeeded": ["fixture"],
                "failed": [],
                "partial": False,
            },
            "candidates": [],
            "inbox": {
                name: {"status": "unknown", "source": "fixture", "checked_at": STAMP, "unread_count": None, "items": None}
                for name in self.template.CHANNELS
            },
        }

    def test_incomplete_candidate_and_unknown_inbox_count_fail_without_write(self):
        with self.assertRaisesRegex(ValueError, "candidate missing"):
            self.template.persist_tick(**{**self.args, "candidates": [{}]})
        next(iter(self.args["inbox"].values()))["unread_count"] = 0
        with self.assertRaisesRegex(ValueError, "unread_count must be null"):
            self.template.persist_tick(**self.args)
        self.assertEqual(self.state.read_text(encoding="utf-8"), "{}")
        self.assertFalse(self.cache.exists())

    def test_replay_and_protect_posted_state(self):
        candidate: dict[str, object] = dict.fromkeys(self.template.REQUIRED_CANDIDATE_FIELDS, "synthetic")
        key = "https://old.reddit.com/r/test/comments/testpost/title/"
        candidate.update(permalink=key, created_utc=0, context_verified=True, keyword_hits=[])
        self.args["candidates"] = [candidate]
        posted = {"status": "posted", "draftCount": 7}
        self.state.write_text(json.dumps({"seen": {"byPermalink": {key: posted}}}), encoding="utf-8")
        self.assertEqual(self.template.persist_tick(**self.args)["candidate_count"], 0)
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["seen"]["byPermalink"][key], posted)
        before = self.state.read_bytes(), self.cache.read_bytes()
        self.assertTrue(self.template.persist_tick(**self.args)["cache_ok"])
        self.assertEqual(before, (self.state.read_bytes(), self.cache.read_bytes()))
        with self.assertRaisesRegex(RuntimeError, "different payload"):
            self.template.persist_tick(**{**self.args, "draft_version": "changed"})

    def test_busy_lock_and_interrupted_second_replace(self):
        with self.template._lock_file(self.fixture / self.template.LOCK_NAME) as lock:
            self.template._try_flock_nonblocking(lock)
            try:
                with self.assertRaisesRegex(RuntimeError, "lock is busy"):
                    self.template.persist_tick(**self.args)
            finally:
                self.template._unlock_file(lock)
        real_replace = self.template.os.replace
        for target in (self.cache, self.state):
            with self.subTest(target=target):
                def replace(source, destination):
                    if destination == target:
                        raise OSError("synthetic interruption")
                    return real_replace(source, destination)
                with patch.object(self.template.os, "replace", side_effect=replace):
                    with self.assertRaisesRegex(OSError, "synthetic interruption"):
                        self.template.persist_tick(**self.args)
                self.assertEqual(self.state.read_text(encoding="utf-8"), "{}")
                self.assertEqual(self.cache.exists(), target == self.state)
        with self.assertRaisesRegex(RuntimeError, "run_id mismatch"):
            self.template.persist_tick(**self.args)


if __name__ == "__main__":
    unittest.main()
