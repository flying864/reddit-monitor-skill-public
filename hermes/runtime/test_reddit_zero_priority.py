"""Offline zero-priority regressions. No production data or network access."""
import contextlib
import importlib.util
import io
import json
import random
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('reddit_zero_priority', Path(__file__).with_name('reddit_monitor_tick_noagent.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def item(name, count, route='keyword', timestamp=100):
    return ({'num_comments': count, 'created_utc': timestamp}, name, name, [], route, 'test')


def post(name, count, route='keyword', age=60):
    return {'permalink': f'/r/test/comments/{name}/help/',
            'title': 'Hose help?' if route == 'keyword' else 'Help with my pressure washer?',
            'selftext': 'My pump stops unexpectedly', 'author': 'a', 'subreddit': 'test',
            'created_utc': time.time()-age, 'num_comments': count}


class PriorityTests(unittest.TestCase):
    def test_confirmed_counterexample_all_three_zero_selected(self):
        items = [item(f'k{i}', 5, timestamp=200-i) for i in range(4)] + [item(f'g{i}', 0, 'generic_help', 100-i) for i in range(3)]
        chosen, deferred = m.select_new_work(items, 5)
        self.assertEqual([x[1] for x in chosen], ['g0','g1','g2','k0','k1'])
        self.assertEqual([x[1] for x in deferred], ['k2','k3'])

    def test_generic_replied_cannot_displace_zero(self):
        chosen, deferred = m.select_new_work([item('g', 2, 'generic_help', 999)] + [item(str(i),0) for i in range(5)],5)
        self.assertEqual(len(chosen),5)
        self.assertTrue(all(x[0]['num_comments']==0 for x in chosen))
        self.assertEqual(deferred[0][1],'g')

    def test_generic_exploration_kept_inside_zero_tier(self):
        chosen, _ = m.select_new_work([item('g',0,'generic_help',1)] + [item(str(i),0,timestamp=200+i) for i in range(5)],3)
        self.assertIn('g',[x[1] for x in chosen])
        self.assertEqual(len(chosen),3)

    def test_all_nonzero_remain_eligible_with_exploration(self):
        chosen, _ = m.select_new_work([item('g',8,'generic_help',1)] + [item(str(i),1,timestamp=200+i) for i in range(5)],3)
        self.assertIn('g',[x[1] for x in chosen])
        self.assertEqual(len(chosen),3)

    def test_invalid_unknown_never_zero(self):
        for count in (None,False,True,-1,'0',0.0):
            self.assertEqual(m.listing_discussion_status(count),'unknown')
        self.assertEqual(m.listing_discussion_status(0),'zero_comments')
        chosen,_=m.select_new_work([item('unknown',None,timestamp=999),item('zero',0,timestamp=1)],1)
        self.assertEqual(chosen[0][1],'zero')

    def test_budget_zero_negative_and_empty(self):
        for budget in (0,-1):
            chosen,deferred=m.select_new_work([item('a',0)],budget)
            self.assertEqual(chosen,[]);self.assertEqual(len(deferred),1)
        self.assertEqual(m.select_new_work([],5),([],[]))

    def test_duplicate_canonical_does_not_consume_budget(self):
        a=item('a',0);b=list(item('b',0));b[2]='a'
        chosen,deferred=m.select_new_work([a,tuple(b),item('c',1)],2)
        self.assertEqual([i[1] for i in chosen],['a','c']);self.assertEqual(deferred,[])

    def test_property_global_zero_priority_and_budget(self):
        rng=random.Random(42)
        for _ in range(400):
            items=[item(str(i),rng.choice([0,0,1,9,None]),rng.choice(['keyword','generic_help']),rng.randint(1,1000)) for i in range(rng.randint(0,25))]
            budget=rng.randint(0,6)
            chosen,deferred=m.select_new_work(items,budget)
            self.assertEqual(len(chosen),min(budget,len(items)))
            self.assertEqual(len(chosen)+len(deferred),len(items))
            self.assertEqual(sum(x[0]['num_comments']==0 for x in chosen),min(budget,sum(x[0]['num_comments']==0 for x in items)))
            zeros=[x[0]['num_comments']==0 for x in chosen]
            self.assertEqual(zeros,sorted(zeros,reverse=True))


class IntegrationTests(unittest.TestCase):
    def run_case(self,posts,*,budget=5,contexts=None,seen=None):
        contexts=contexts or {}
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);state=base/'state.json';cache=base/'cache.json'
            state.write_text(json.dumps({'watch':[{'name':'test','url':'https://old.reddit.com/r/test/','keywords':['hose'],'maxCandidates':budget,'maxAgeHours':48}],
                                         'seen':{'byPermalink':seen or {}},'inbox':{'sentinel':'do not change'}}))
            def context(url):
                name=url.split('/comments/')[1].split('/')[0]
                detail=contexts.get(name,{})
                if isinstance(detail,Exception):raise detail
                source=next((p for p in posts if f'/comments/{name}/' in p['permalink']),post(name,0))
                result={**source,**detail}
                count=result.get('num_comments')
                comments=detail.get('comments',[{'body':'example reply'} for _ in range(count if type(count) is int and count>=0 else 0)])
                result['comments_coverage']=detail.get('coverage',{'truncated':False,'has_more':False,'unreadable_comments':False})
                return result,comments,'isolated fixture'
            with patch.object(m, '_storage_paths', return_value=(state, cache)), \
                 patch.object(m,'fetch_listing',return_value=(posts,'isolated fixture')),patch.object(m,'fetch_context',side_effect=context), \
                 patch.object(m.urllib.request,'urlopen',side_effect=AssertionError('Network forbidden')),contextlib.redirect_stdout(io.StringIO()) as out:
                m.run_monitor()
            return json.loads(cache.read_text()),json.loads(state.read_text()),out.getvalue()

    def test_global_selection_and_new_cohort_counters(self):
        posts=[post(f'k{i}',4,age=i+1) for i in range(4)]+[post(f'g{i}',0,'generic_help',age=100+i) for i in range(3)]
        cache,state,text=self.run_case(posts)
        s=cache['scan']
        self.assertEqual(s['newLeadDiscussionCounts'],{'zero_comments':3,'has_comments':4,'unknown':0})
        self.assertEqual(s['newSelectedDiscussionCounts'],{'zero_comments':3,'has_comments':2,'unknown':0})
        self.assertEqual(s['newDeferredDiscussionCounts'],{'zero_comments':0,'has_comments':2,'unknown':0})
        self.assertEqual(s['newCandidateDiscussionCounts'],s['newSelectedDiscussionCounts'])
        self.assertEqual(s['deferredNewCount'],2)
        self.assertEqual(state['inbox'],{'sentinel':'do not change'})
        self.assertIn('发现 3；选择读取 3；预算延期 0',text)
        self.assertFalse(s['coverageComplete']);self.assertEqual(s['listingPageLimit'],25)

    def test_listing_zero_can_change_by_context_without_false_zero(self):
        cache,_,_=self.run_case([post('changed',0)],contexts={'changed':{'num_comments':2}})
        s=cache['scan'];c=cache['candidates'][0]
        self.assertEqual(s['newLeadDiscussionCounts']['zero_comments'],1)
        self.assertEqual(s['newCandidateDiscussionCounts']['zero_comments'],0)
        self.assertEqual(s['newListingZeroChangedAtContextCount'],1)
        self.assertEqual((c['num_comments_at_listing'],c['num_comments_at_scan']),(0,2))

    def test_skip_failure_unknown_reconcile(self):
        posts=[post('lowfit',0,'generic_help'),post('failed',0),post('unknown',None)]
        cache,_,_=self.run_case(posts,contexts={'lowfit':{'title':'Help?','selftext':'My cat is hungry'},'failed':RuntimeError('mock failure')})
        s=cache['scan']
        self.assertEqual(s['newPrefilterSkippedDiscussionCounts']['zero_comments'],1)
        self.assertEqual(s['newContextFailureListingCounts']['zero_comments'],1)
        self.assertEqual(s['newCandidateDiscussionCounts']['unknown'],1)
        self.assertEqual(sum(s['newSelectedDiscussionCounts'].values()),len(cache['candidates'])+len(cache['skipped']))

    def test_new_before_zero_retry_and_terminal_protection(self):
        old=post('old',0);key=m.norm_permalink(old['permalink']);posted=m.norm_permalink(post('done',0)['permalink'])
        seen={key:{'action':'failed_agent','created_utc':old['created_utc'],'title':'Help?','subreddit':'test','meta':{'commentCountAtReview':0}},
              posted:{'action':'posted','reviewStatus':'pending_semantic_review'}}
        cache,state,_=self.run_case([post('fresh',2),post('done',0)],budget=1,seen=seen)
        self.assertEqual(cache['candidates'][0]['candidate_origin'],'new')
        self.assertEqual(cache['scan']['deferredRetryCount'],1)
        self.assertEqual(state['seen']['byPermalink'][posted],seen[posted])

    def test_old_cohort_not_in_new_zero_counts(self):
        old=post('old',0);key=m.norm_permalink(old['permalink'])
        seen={key:{'action':'failed_agent','created_utc':old['created_utc'],'title':'Hose help?','subreddit':'test','meta':{'commentCountAtReview':0}}}
        cache,_,_=self.run_case([post('fresh',1)],budget=2,seen=seen)
        self.assertEqual(cache['scan']['newCandidateDiscussionCounts']['zero_comments'],0)
        self.assertEqual(cache['scan']['retryCandidateDiscussionCounts']['zero_comments'],1)


class SummaryTests(unittest.TestCase):
    def fixture(self,td):
        state=Path(td)/'state.json';cache=Path(td)/'cache.json';run='isolated-run';candidates=[];entries={}
        for i,(result,origin,status) in enumerate([('recommended','new','zero_comments'),('not_recommended','new','zero_comments'),('needs_manual_review','new','zero_comments'),('failed_agent','new','zero_comments'),(None,'new','zero_comments'),('recommended','retry','zero_comments'),('recommended','new','has_comments')]):
            key=f'https://old.reddit.com/r/test/comments/{i}/help/'
            candidates.append({'permalink':key,'candidate_origin':origin,'discussion_status':status})
            payload={'contentRunId':run,'permalink':key,'result':result,'notes':'isolated semantic fixture'}
            entries[key]={'lastReviewRunId':run if result else 'old-run','lastReviewPayload':payload,'reviewStatus':result}
        state.write_text(json.dumps({'contentRunId':run,'seen':{'byPermalink':entries}}))
        cache.write_text(json.dumps({'contentRunId':run,'candidates':candidates,'scan':{},'skipped':[]}))
        return state,cache

    def test_exact_outcomes_pending_failed_and_read_only(self):
        with tempfile.TemporaryDirectory() as td:
            state,cache=self.fixture(td);before=(state.read_bytes(),cache.read_bytes())
            result=m.review_summary(state,cache);g=result['outcomesByOriginAndContextDiscussion']
            self.assertEqual(g['new']['zero_comments'],{'candidates':5,'reviewed':3,'recommended':1,'not_recommended':1,'needs_manual_review':1,'failed_agent':1,'pending':1})
            self.assertEqual(g['retry']['zero_comments']['recommended'],1)
            self.assertEqual(g['new']['has_comments']['recommended'],1)
            self.assertEqual(before,(state.read_bytes(),cache.read_bytes()))

    def test_batch_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            state,cache=self.fixture(td);d=json.loads(cache.read_text());d['contentRunId']='other';cache.write_text(json.dumps(d))
            with self.assertRaises(RuntimeError):m.review_summary(state,cache)

    def test_wrong_payload_target_or_run_not_counted(self):
        for field,value in [('contentRunId','old'),('permalink','/r/test/comments/wrong/')]:
            with tempfile.TemporaryDirectory() as td:
                state,cache=self.fixture(td);d=json.loads(state.read_text());next(iter(d['seen']['byPermalink'].values()))['lastReviewPayload'][field]=value;state.write_text(json.dumps(d))
                g=m.review_summary(state,cache)['outcomesByOriginAndContextDiscussion']['new']['zero_comments']
                self.assertEqual(g['recommended'],0);self.assertEqual(g['pending'],2)

    def test_missing_or_invalid_group_fields_rejected_not_zero(self):
        for field, value in [('candidate_origin',None),('candidate_origin','legacy'),
                             ('discussion_status',None),('discussion_status','unverified'),('permalink',None)]:
            with self.subTest(field=field,value=value),tempfile.TemporaryDirectory() as td:
                state,cache=self.fixture(td);d=json.loads(cache.read_text())
                if value is None:d['candidates'][0].pop(field)
                else:d['candidates'][0][field]=value
                cache.write_text(json.dumps(d));before=(state.read_bytes(),cache.read_bytes())
                with self.assertRaises(ValueError):m.review_summary(state,cache)
                self.assertEqual(before,(state.read_bytes(),cache.read_bytes()))

    def test_missing_candidates_or_duplicate_target_rejected(self):
        for replacement in (None,{},'missing','duplicate'):
            with self.subTest(replacement=replacement),tempfile.TemporaryDirectory() as td:
                state,cache=self.fixture(td);d=json.loads(cache.read_text())
                if replacement=='missing':d.pop('candidates')
                elif replacement=='duplicate':d['candidates'].append(d['candidates'][0])
                else:d['candidates']=replacement
                cache.write_text(json.dumps(d))
                with self.assertRaises(ValueError):m.review_summary(state,cache)

    def test_genuine_empty_candidates_returns_verified_zero(self):
        with tempfile.TemporaryDirectory() as td:
            state,cache=self.fixture(td);d=json.loads(cache.read_text());d['candidates']=[];cache.write_text(json.dumps(d))
            groups=m.review_summary(state,cache)['outcomesByOriginAndContextDiscussion']
            self.assertEqual(sum(group['candidates'] for origin in groups.values() for group in origin.values()),0)

    def test_missing_metadata_summary_cli_fails_nonzero(self):
        with tempfile.TemporaryDirectory() as td:
            state,cache=self.fixture(td);d=json.loads(cache.read_text());d['candidates'][0].pop('discussion_status');cache.write_text(json.dumps(d))
            with patch.object(m, '_storage_paths', return_value=(state, cache)), \
                 patch.object(sys,'argv',['monitor','summary']),patch.object(m,'camofox_request',side_effect=AssertionError('No browser')), \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                with self.assertRaises(SystemExit) as exc:m.main()
            self.assertEqual(exc.exception.code,2)
            self.assertIn('cannot report zero',out.getvalue())

    def test_summary_cli_dispatch_no_browser_or_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            state,cache=self.fixture(td)
            with patch.object(m, '_storage_paths', return_value=(state, cache)), \
                 patch.object(sys,'argv',['monitor','summary']),patch.object(m,'close_camofox_group',side_effect=AssertionError('No cleanup')), \
                 patch.object(m,'camofox_request',side_effect=AssertionError('No browser')),contextlib.redirect_stdout(io.StringIO()) as out:
                with self.assertRaises(SystemExit) as exc:m.main()
            self.assertEqual(exc.exception.code,0)
            self.assertEqual(json.loads(out.getvalue())['contentRunId'],'isolated-run')


if __name__=='__main__':
    unittest.main()
