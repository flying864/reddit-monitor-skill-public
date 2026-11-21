#!/usr/bin/env python3
"""Offline, isolated selection and persistence regressions for Reddit content scan."""
import contextlib
import importlib.util
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).with_name('reddit_monitor_tick_noagent.py')
spec = importlib.util.spec_from_file_location('reddit_new_post_focus', SCRIPT)
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


def post(name, count, age=60):
    return {'permalink': f'/r/test/comments/{name}/help/', 'title': f'Hose leak help {name}',
            'selftext': 'Hose leak help?', 'author': 'test', 'subreddit': 'test',
            'created_utc': time.time() - age, 'num_comments': count}


class SelectionTests(unittest.TestCase):
    def run_case(self, posts, *, budget=5, seen=None, contexts=None):
        contexts = contexts or {}
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            state_path = base / 'state.json'
            cache_path = base / 'review-cache' / 'latest.json'
            state_path.write_text(json.dumps({'watch': [{'name': 'test', 'url': 'https://old.reddit.com/r/test/',
                'keywords': ['hose'], 'maxAgeHours': 48, 'maxCandidates': budget}],
                'seen': {'byPermalink': seen or {}}}), encoding='utf-8')
            def context(permalink):
                name = permalink.split('/comments/')[1].split('/')[0]
                if name in contexts and isinstance(contexts[name], Exception):
                    raise contexts[name]
                source = next((item for item in posts if f'/comments/{name}/' in item['permalink']), None)
                if source is None:
                    source = post(name, (seen[permalink].get('meta') or {}).get('commentCountAtReview'))
                details = contexts.get(name, {})
                comments = details.get('comments', [])
                thread = {**source, **{k:v for k,v in details.items() if k not in ('comments','coverage')}, 'comments_coverage': details.get('coverage',
                    {'returned': len(comments), 'limit': 20, 'truncated': False})}
                return thread, comments, 'mock context'
            with patch.object(monitor, '_storage_paths', return_value=(state_path, cache_path)), \
                 patch.object(monitor, 'fetch_listing', return_value=(posts, 'mock listing')), \
                 patch.object(monitor, 'fetch_context', side_effect=context), contextlib.redirect_stdout(io.StringIO()) as output:
                monitor.run_monitor()
            return json.loads(cache_path.read_text()), json.loads(state_path.read_text()), output.getvalue()

    def test_new_first_zero_preferred_but_not_gate_and_budget(self):
        posts = [post('nonzero', 4, 10), post('zero_old', 0, 300), post('missing', None, 5),
                 post('zero_new', 0, 100), post('deferred', 2, 400)]
        old = post('old', 2)
        key = monitor.norm_permalink(old['permalink'])
        seen = {key: {'action': 'candidate_pending_semantic_review', 'created_utc': old['created_utc'],
                      'title': old['title'], 'subreddit': 'test', 'keywordHits': ['hose'],
                      'meta': {'commentCountAtReview': 2}}}
        cache, state, text = self.run_case(posts, budget=4, seen=seen)
        candidates = cache['candidates']
        self.assertEqual([c['title'].split()[-1] for c in candidates], ['zero_new', 'zero_old', 'missing', 'nonzero'])
        self.assertEqual([c['candidate_origin'] for c in candidates], ['new'] * 4)
        self.assertEqual([c['discussion_status'] for c in candidates],
                         ['zero_comments', 'zero_comments', 'unknown', 'has_comments'])
        scan = cache['scan']
        self.assertEqual((scan['newKeywordHits'], scan['deferredNewCount'], scan['deferredRetryCount']), (5, 1, 1))
        self.assertEqual((scan['newCandidateCount'], scan['retryCandidateCount']), (4, 0))
        self.assertEqual(scan['discussionStatusCounts'], {'zero_comments': 2, 'has_comments': 1, 'unknown': 1})
        self.assertIn('first page, at most 25', scan['listingCoverage'])
        self.assertTrue(scan['startedAt'] and scan['finishedAt'])
        self.assertIn('新候选：4；旧帖重试候选：0', text)
        self.assertIn(key, state['seen']['byPermalink'])

    def test_retry_leftover_and_old_only_truthful(self):
        old = post('old', 3)
        key = monitor.norm_permalink(old['permalink'])
        seen = {key: {'action': 'failed_agent', 'created_utc': old['created_utc'],
                      'title': old['title'], 'subreddit': 'test', 'keywordHits': ['hose'],
                      'meta': {'commentCountAtReview': 3}}}
        cache, _, text = self.run_case([post('fresh', 0)], budget=2, seen=seen,
            contexts={'old': {'comments': [{'body': 'off point'}]}})
        self.assertEqual([c['candidate_origin'] for c in cache['candidates']], ['new', 'retry'])
        self.assertEqual(cache['candidates'][1]['discussion_status'], 'has_comments')
        self.assertFalse(cache['candidates'][1]['comments_coverage']['complete'])
        self.assertEqual(cache['scan']['newCandidateCount'], 1)
        self.assertEqual(cache['scan']['retryCandidateCount'], 1)
        cache, _, text = self.run_case([post('posted', 0)], budget=2,
            seen={**seen, monitor.norm_permalink(post('posted', 0)['permalink']): {'action': 'posted'}})
        self.assertEqual([c['candidate_origin'] for c in cache['candidates']], ['retry'])
        self.assertEqual((cache['scan']['newCandidateCount'], cache['scan']['retryCandidateCount']), (0, 1))
        self.assertIn('新候选：0；旧帖重试候选：1', text)

    def test_replied_thread_can_have_verified_complete_coverage(self):
        comments = [{'body': 'Try soap'}, {'body': 'Nice setup'}]
        good = {'returned': 2, 'limit': 20, 'truncated': False,
                'has_more': False, 'unreadable_comments': False}
        cache, _, _ = self.run_case([post('replied', 2)],
            contexts={'replied': {'comments': comments, 'coverage': good}})
        candidate = cache['candidates'][0]
        self.assertEqual(candidate['candidate_origin'], 'new')
        self.assertEqual(candidate['discussion_status'], 'has_comments')
        self.assertTrue(candidate['comments_coverage']['complete'])
        for overrides in ({'has_more': True}, {'unreadable_comments': True}, {'truncated': True}):
            coverage = monitor.comment_coverage({'num_comments': 2, 'comments_coverage': {**good, **overrides}}, comments)
            self.assertFalse(coverage['complete'])
        self.assertFalse(monitor.comment_coverage({'num_comments': None, 'comments_coverage': good}, comments)['complete'])

    def test_posted_protected_failure_and_incomplete_unknown(self):
        posted = post('posted', 0)
        key = monitor.norm_permalink(posted['permalink'])
        cache, state, _ = self.run_case([posted, post('broken', 0), post('partial', 5), post('unknown', None), post('contradict', 0)],
            seen={key: {'action': 'posted', 'meta': {'status': 'posted'}}},
            contexts={'broken': RuntimeError('mock context unavailable'),
                      'partial': {'comments': [{'body': 'off point'}]},
                      'unknown': {'comments': []},
                      'contradict': {'comments': [{'body': 'reply'}]}})
        self.assertEqual(state['seen']['byPermalink'][key]['action'], 'posted')
        self.assertNotIn(key, [c['permalink'] for c in cache['candidates']])
        self.assertEqual(cache['skipped'][0]['action'], 'skipped_context_unverified')
        self.assertEqual(cache['skipped'][0]['candidate_origin'], 'new')
        self.assertEqual(state['seen']['byPermalink'][monitor.norm_permalink(post('broken', 0)['permalink'])]['action'], 'skipped_context_unverified')
        by_name = {c['title'].split()[-1]: c for c in cache['candidates']}
        self.assertFalse(by_name['partial']['comments_coverage']['complete'])
        self.assertEqual(by_name['unknown']['discussion_status'], 'unknown')
        self.assertFalse(by_name['unknown']['comments_coverage']['complete'])
        self.assertEqual(by_name['contradict']['discussion_status'], 'has_comments')


if __name__ == '__main__':
    unittest.main()
