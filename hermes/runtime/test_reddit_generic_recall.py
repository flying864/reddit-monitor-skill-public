"""Offline bounded generic-help recall and execution/coverage regressions."""
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

spec = importlib.util.spec_from_file_location("reddit_generic_recall", SCRIPTS_DIR / "reddit_monitor_tick_noagent.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def item(name, title, body="", count=0, flair=""):
    return {
        "permalink": f"https://www.reddit.com/r/test/comments/{name}/title/",
        "title": title,
        "selftext": body,
        "num_comments": count,
        "flair": flair,
        "created_utc": time.time() - 60,
        "subreddit": "test",
        "author": "a",
    }


class RecallTests(unittest.TestCase):
    def scan(self, posts, budget=5, seen=None, contexts=None, listing_error=None):
        contexts = contexts or {}
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state.json"
            cache = Path(td) / "cache.json"
            state.write_text(json.dumps({
                "watch": [{"name": "test", "url": "https://old.reddit.com/r/test/", "keywords": ["hose"], "maxCandidates": budget, "maxAgeHours": 48}],
                "seen": {"byPermalink": seen or {}},
            }), encoding="utf-8")

            def fetch_context(url):
                name = url.split("/comments/")[1].split("/")[0]
                details = contexts.get(name)
                if isinstance(details, Exception):
                    raise details
                source = next((p for p in posts if f"/comments/{name}/" in p["permalink"]), None)
                if source is None:
                    source = item(name, "Help", "")
                return {
                    **source,
                    **(details or {}),
                    "comments_coverage": {"truncated": False, "has_more": False, "unreadable_comments": False},
                }, [], "mock context"

            def fetch_listing(_):
                if listing_error:
                    raise listing_error
                return posts, "mock listing"

            with (
                patch.object(m, "_storage_paths", return_value=(state, cache)),
                patch.object(m, "fetch_listing", side_effect=fetch_listing),
                patch.object(m, "fetch_context", side_effect=fetch_context),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                if listing_error:
                    with self.assertRaises(SystemExit):
                        m.run_monitor()
                    return None, None, output.getvalue()
                if contexts and all(isinstance(contexts.get(p["permalink"].split("/comments/")[1].split("/")[0]), Exception) for p in posts):
                    with self.assertRaises(SystemExit) as exc:
                        m.run_monitor()
                    self.assertEqual(exc.exception.code, 2)
                else:
                    m.run_monitor()
            return json.loads(cache.read_text(encoding="utf-8")), json.loads(state.read_text(encoding="utf-8")), output.getvalue()

    def test_help_hose_fitting_and_advice_without_failure(self):
        posts = [
            item("fitting", "Help", "My pressure washer needs a hose fitting recommendation"),
            item("advice", "Any advice?", "Choosing a pump for my pressure washing work"),
            item("body", "Help", "Which hose should I get?"),
        ]
        cache, _, _ = self.scan(posts)
        self.assertEqual(len(cache["candidates"]), 3)
        self.assertEqual(cache["scan"]["newKeywordHits"], 2)
        self.assertEqual(cache["scan"]["newGenericHelpHits"], 1)
        by_name = {c["permalink"].split("/comments/")[1].split("/")[0]: c for c in cache["candidates"]}
        self.assertEqual(by_name["advice"]["keyword_hits"], [])
        self.assertEqual(by_name["body"]["keyword_hits"], ["hose"])
        self.assertEqual(by_name["body"]["discovery_route"], "keyword")

    def test_sparse_media_flair_unrelated_noise(self):
        posts = [
            item("image", "Help", ""),
            item("flair", "A fitting conundrum", "", flair="Question"),
            item("unrelated", "Help", "How to train my cat for a marathon?"),
            item("sale", "Help for sale", "pressure washer"),
            item("show", "Showcase: help me price my setup", "hose"),
        ]
        cache, _, _ = self.scan(posts, contexts={"image": {"media": [{"url": "https://i.redd.it/p.png"}]}})
        self.assertEqual({c["permalink"].split("/comments/")[1].split("/")[0] for c in cache["candidates"]}, {"image", "flair"})
        self.assertEqual(cache["scan"]["newGenericHelpHits"], 3)
        self.assertTrue(any(s["meta"]["reason"] == "unrelated body without relevant technical context" for s in cache["skipped"]))
        self.assertEqual(next(c for c in cache["candidates"] if "/flair/" in c["permalink"])["discovery_reason"], "help_flair")
        self.assertTrue(all(c["action"] == "candidate_pending_semantic_review" for c in cache["candidates"]))

    def test_quota_full_and_spare_zero_preference_nonzero(self):
        posts = [item("g1", "Help", "", 3), item("g0", "Help", "", 0)] + [item(f"k{i}", "Hose leak?", "", 0) for i in range(4)]
        cache, _, _ = self.scan(posts, budget=3)
        self.assertEqual([c["discovery_route"] for c in cache["candidates"]].count("generic_help"), 1)
        self.assertTrue(any("/g0/" in c["permalink"] for c in cache["candidates"]))
        self.assertFalse(any("/g1/" in c["permalink"] for c in cache["candidates"]))
        self.assertEqual((cache["scan"]["newKeywordHits"], cache["scan"]["newGenericHelpHits"], cache["scan"]["deferredNewCount"]), (4, 2, 3))
        cache, _, _ = self.scan(posts[:2], budget=3)
        self.assertEqual(len(cache["candidates"]), 2)
        self.assertEqual(cache["candidates"][0]["discussion_status"], "zero_comments")
        self.assertEqual(cache["candidates"][1]["discussion_status"], "has_comments")

    def test_retry_route_and_context_recomputed(self):
        old = item("old", "Help", "")
        key = m.norm_permalink(old["permalink"])
        seen = {
            key: {
                "action": "failed_agent",
                "created_utc": old["created_utc"],
                "title": "Help",
                "subreddit": "test",
                "keywordHits": [],
                "discovery_route": "generic_help",
                "discovery_reason": "generic_help_title",
            }
        }
        cache, state, _ = self.scan(
            [item("fresh", "Hose leak?", "")],
            budget=2,
            seen=seen,
            contexts={"old": {"selftext": "Which pressure washer pump should I choose?"}},
        )
        retry = cache["candidates"][1]
        self.assertEqual((retry["candidate_origin"], retry["discovery_route"], retry["keyword_hits"]), ("retry", "generic_help", []))
        self.assertEqual(state["seen"]["byPermalink"][key]["discovery_reason"], "generic_help_title")
        cache, _, _ = self.scan([item("body", "Help", "")], contexts={"body": {"selftext": "Which hose suits this connection?"}})
        self.assertEqual(cache["candidates"][0]["keyword_hits"], ["hose"])
        self.assertEqual(cache["candidates"][0]["discovery_route"], "generic_help")

    def test_execution_separate_from_coverage_and_context_failure(self):
        cache, _, report = self.scan([item("a", "Help", "")], contexts={"a": RuntimeError("offline")})
        self.assertEqual(cache["scan"]["executionStatus"], "failed")
        self.assertEqual(cache["scan"]["contextFetchFailures"], 1)
        self.assertFalse(cache["scan"]["coverageComplete"])
        self.assertEqual(cache["skipped"][0]["discovery_route"], "generic_help")
        self.assertIn("采集执行；覆盖不完整", report)
        cache, _, _ = self.scan([item("a", "Help", "")])
        self.assertEqual(cache["scan"]["executionStatus"], "success")
        self.assertFalse(cache["scan"]["coverageComplete"])
        cache, _, report = self.scan([], listing_error=RuntimeError("offline"))
        self.assertIsNone(cache)
        self.assertIn("扫描结果：失败", report)

    def test_setup_help_and_body_only_questions_not_overfiltered(self):
        posts = [
            item("setup", "Help with my new setup", ""),
            item("bodyquestion", "Hose options", "Which one should I choose?"),
            item("bodyonly", "Options", "Which hose should I choose?"),
        ]
        cache, _, _ = self.scan(posts, contexts={"setup": {"selftext": "My pump stops unexpectedly"}})
        self.assertEqual(len(cache["candidates"]), 3)
        self.assertIsNone(m.generic_help_route(item("show", "My new setup", "")))
        self.assertIsNone(m.generic_help_route(item("show2", "Showcase: my new setup", "")))

    def test_boundaries_and_listing_age(self):
        self.assertIsNone(m.generic_help_route(item("a", "Helpful hints", "")))
        self.assertIsNone(m.generic_help_route(item("a", "Helped a friend", "")))
        self.assertIsNone(m.generic_help_route(item("a", "For sale: help", "")))
        old = item("old", "Help", "")
        old["created_utc"] -= 49 * 3600
        cache, _, _ = self.scan([old, item("new", "Help", "")])
        self.assertEqual(cache["scan"]["newGenericHelpHits"], 1)


if __name__ == "__main__":
    unittest.main()
