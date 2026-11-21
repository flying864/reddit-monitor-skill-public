"""Isolated custom feed adapter tests; no network or production writes."""
import importlib.util
from pathlib import Path
import sys
from unittest.mock import patch
import unittest

SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

spec = importlib.util.spec_from_file_location("monitor_custom_feed", SCRIPTS_DIR / "reddit_monitor_tick_noagent.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
URL = "https://www.reddit.com/user/fixturereader/m/fixture-feed/new/"
OLD = URL.replace("www.reddit.com", "old.reddit.com")


class FeedTests(unittest.TestCase):
    def setUp(self):
        network = patch.object(m.urllib.request, "urlopen", side_effect=AssertionError("network forbidden"))
        network.start()
        self.addCleanup(network.stop)
        mock = patch.dict("os.environ", {
            "REDDIT_EXPECTED_USER": "FixtureReader",
            "REDDIT_MONITOR_AUTHORIZED": "1",
            "CAMOFOX_URL": "http://camofox.example.invalid",
            "CAMOFOX_USER_ID": "fixture-browser",
            "CAMOFOX_SESSION_KEY": "fixture-group",
        }, clear=True)
        mock.start()
        self.addCleanup(mock.stop)

    def response(self, **kw):
        return {
            "url": OLD,
            "selected_sort": "new",
            "logged_in_user": "FixtureReader",
            "feed_members": ["fixturegear", "fixturetools"],
            "posts": [{"title": "Hose help?"}],
        } | kw

    def test_adapter_and_membership_updates(self):
        with patch.object(m, "camofox_navigate") as navigate, patch.object(m, "camofox_evaluate", return_value=self.response()):
            posts, _ = m.fetch_listing(URL)
            navigate.assert_called_once_with(OLD)
            self.assertEqual(len(posts), 1)
        watch = {"url": URL}
        self.assertTrue(m.watch_contains(watch, {"subreddit": "FixtureTools"}, ""))
        self.assertFalse(m.watch_contains(watch, {"subreddit": "FixtureOther"}, ""))
        with patch.object(m, "camofox_navigate"), patch.object(m, "camofox_evaluate", return_value=self.response(feed_members=["FixtureOther"])):
            m.fetch_listing(URL)
        self.assertFalse(m.watch_contains(watch, {"subreddit": "fixturetools"}, ""))
        self.assertTrue(m.watch_contains(watch, {}, "https://old.reddit.com/r/FixtureOther/comments/a/"))

    def test_fail_closed_sort_identity_membership_and_url(self):
        for changes in (
            {"selected_sort": "hot"},
            {"logged_in_user": "another"},
            {"feed_members": []},
            {"url": "https://old.reddit.com/r/all/new/"},
        ):
            with self.subTest(changes=changes), patch.object(m, "camofox_navigate"), patch.object(m, "camofox_evaluate", return_value=self.response(**changes)):
                with self.assertRaises(ValueError):
                    m.fetch_listing(URL)
                self.assertFalse(m.watch_contains({"url": URL}, {"subreddit": "fixturegear"}, ""))

    def test_old_combined_watch_unchanged(self):
        watch = {"url": "https://old.reddit.com/r/fixturegear+fixturetools/new/"}
        self.assertTrue(m.watch_contains(watch, {}, "https://old.reddit.com/r/fixturetools/comments/a/"))
        self.assertFalse(m.watch_contains(watch, {"subreddit": "unrelated"}, ""))
        self.assertIsNone(m.custom_feed_path("https://www.reddit.com/user/fixturereader/m/fixture-feed/"))


if __name__ == "__main__":
    unittest.main()
