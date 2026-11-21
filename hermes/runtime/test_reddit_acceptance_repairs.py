"""Independent acceptance tests for the approved Reddit repairs; offline only."""
import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location('reddit_acceptance_target', Path(__file__).with_name('reddit_monitor_tick_noagent.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def post(pid, count=0, age=10):
    return {'id':'t3_'+pid,'fullname':'t3_'+pid,'permalink':f'https://old.reddit.com/r/test/comments/{pid}/help/',
            'title':'Hose leak help?', 'selftext':'Which hose fits? I need repair advice.', 'subreddit':'test',
            'created_utc':time.time()-age, 'num_comments':count, 'flair':'Help'}


class IndependentAcceptance(unittest.TestCase):
    def setUp(self):
        network = patch.object(m.urllib.request, 'urlopen', side_effect=AssertionError('network forbidden'))
        network.start()
        self.addCleanup(network.stop)
        mock = patch.dict('os.environ', {
            'REDDIT_EXPECTED_USER': 'FixtureReader',
            'REDDIT_MONITOR_AUTHORIZED': '1',
            'CAMOFOX_URL': 'http://camofox.example.invalid',
            'CAMOFOX_USER_ID': 'fixture-browser',
            'CAMOFOX_SESSION_KEY': 'fixture-group',
        }, clear=True)
        mock.start()
        self.addCleanup(mock.stop)

    def test_finite_plural_rules_with_configured_keyword_boundaries(self):
        for kw in ['hose','nozzle','adapter','fitting','coupler','connector','swivel','orifice','wand']:
            with self.subTest(kw=kw):
                self.assertTrue(m.word_hit('Need '+kw+'s',kw))
                self.assertFalse(m.word_hit('prefix'+kw+'ssuffix',kw))
        self.assertFalse(m.word_hit('M22s','m22'))
        self.assertFalse(m.word_hit('house','hose'))

    def test_negated_sale_is_not_terminal_noise(self):
        p=post('abc1');p['selftext']='This machine is not for sale; I need to repair it. Which hose fits?'
        self.assertTrue(m.candidate_fit(p,['hose'])[0])
        actual=post('abc2');actual.update(title='Hose for sale, $30',selftext='Selling my spare hose. Not interested in trades.',flair='')
        self.assertFalse(m.candidate_fit(actual,['hose'])[0])

    def test_primary_sale_with_defect_stays_noise(self):
        for title,body in [('Broken hose for sale, $30','Needs repair; not accepting trades.'),
                           ('Selling a hose, help me clear garage','It leaks.'),
                           ('What is this worth? Broken hose','Needs repair.')]:
            with self.subTest(title=title):
                self.assertFalse(m.candidate_fit({'title':title,'selftext':body},['hose'])[0])
        self.assertTrue(m.candidate_fit({'title':'How to fix a hose listed for sale?','selftext':'Can the split end be repaired?'},['hose'])[0])

    def test_review_rejects_untrusted_raw_target_and_uses_small_digest(self):
        with tempfile.TemporaryDirectory() as td:
            sp,cp,rp,key=self.review_fixture(td)
            payload=json.loads(rp.read_text());payload['permalink']=key.replace('old.reddit.com','evil.test');rp.write_text(json.dumps(payload))
            before=sp.read_bytes()
            with self.assertRaises(ValueError):m.apply_review_result(rp,sp,cp)
            self.assertEqual(before,sp.read_bytes())
            payload['permalink']=key;rp.write_text(json.dumps(payload));m.apply_review_result(rp,sp,cp)
            entry=json.loads(sp.read_text())['seen']['byPermalink'][key]
            self.assertNotIn('lastReviewCandidate',entry)
            self.assertEqual(len(entry['lastReviewCandidateHash']),64)
            self.assertTrue(m.apply_review_result(rp,sp,cp)['idempotent'])

    def test_imgur_entry_is_gallery_not_discarded(self):
        selected=m.select_media([{'url':'https://imgur.com/a/FixtureAlbum'}, {'url':'https://imgur.com.evil.test/a/FixtureAlbum'}])
        self.assertEqual([x['url'] for x in selected],['https://imgur.com/a/FixtureAlbum'])
        self.assertEqual(selected[0]['type'],'gallery')

    def response(self,target):
        return {'url':target['permalink'],'logged_in_user':'FixtureReader','post':target,
                'comments':[],'comments_coverage':{'truncated':False,'has_more':False,'unreadable_comments':False}}

    def test_source_validation_before_normalization(self):
        p=post('abc1')
        for bad in ['https://evil.example/r/test/comments/abc1/help/',
                    'https://old.reddit.com.evil.example/r/test/comments/abc1/help/',
                    'https://old.reddit.com/r/test/comments/other/help/']:
            res=self.response(p);res['url']=bad
            with self.subTest(url=bad),patch.object(m,'camofox_navigate'),patch.object(m,'camofox_evaluate',return_value=res):
                with self.assertRaises(ValueError):m.fetch_context(p['permalink'])
        res=self.response(p);res['logged_in_user']='not_the_authorized_account'
        with patch.object(m,'camofox_navigate'),patch.object(m,'camofox_evaluate',return_value=res):
            with self.assertRaises(ValueError):m.fetch_context(p['permalink'])

    def test_valid_domain_and_slug_change_still_allowed(self):
        p=post('abc1');res=self.response(p);res['url']='https://www.reddit.com/r/test/comments/abc1/new_slug/?sort=new'
        with patch.object(m,'camofox_navigate'),patch.object(m,'camofox_evaluate',return_value=res):
            actual,_,_=m.fetch_context(p['permalink'])
        self.assertEqual(actual['title'],p['title'])

    def test_media_classification_through_context_adapter(self):
        p=post('abc1');p['media']=[
          {'url':'https://old.reddit.com/domain/i.redd.it/','alt':'i.redd.it'},
          {'url':'https://preview.redd.it/photo.png?width=140&height=140','alt':'small'},
          {'url':'https://i.redd.it/photo.png','alt':'original'},
          {'url':'https://example.net/redirect?next=i.redd.it','alt':'not an image'},
          {'url':'https://old.reddit.com/gallery/xyz1','alt':'gallery'}]
        with patch.object(m,'camofox_navigate'),patch.object(m,'camofox_evaluate',return_value=self.response(p)):
            actual,_,_=m.fetch_context(p['permalink'])
        urls=[x['url'] for x in actual['media']]
        self.assertIn('https://i.redd.it/photo.png',urls)
        self.assertIn('https://old.reddit.com/gallery/xyz1',urls)
        self.assertEqual(len(urls),2)

    def review_fixture(self,td,entry=None):
        base=Path(td);sp=base/'state.json';cp=base/'cache.json';rp=base/'review.json';p=post('abc1')
        candidate={**p,'candidate_origin':'new','discussion_status':'zero_comments','listing_discussion_status':'zero_comments'}
        e={'action':'candidate_pending_semantic_review','reviewStatus':'pending_semantic_review','manualReviewCount':0,**(entry or {})}
        sp.write_text(json.dumps({'contentRunId':'test-run','seen':{'byPermalink':{p['permalink']:e}},'inbox':{'sentinel':'keep'}}))
        cp.write_text(json.dumps({'contentRunId':'test-run','candidates':[candidate],'scan':{},'skipped':[]}))
        payload={'contentRunId':'test-run','permalink':p['permalink'],'result':'recommended','notes':'test result'}
        rp.write_text(json.dumps(payload));return sp,cp,rp,p['permalink']

    def test_protected_fields_mixed_legacy(self):
        for field in ['action','status','reviewStatus']:
            for value in ['posted','handled','posted_manual','posted_reply','already_replied']:
                for nested in [False,True]:
                    entry={'meta':{field:value}} if nested else {field:value}
                    with self.subTest(field=field,value=value,nested=nested),tempfile.TemporaryDirectory() as td:
                        sp,cp,rp,_=self.review_fixture(td,entry);before=(sp.read_bytes(),cp.read_bytes())
                        with self.assertRaises(ValueError):m.apply_review_result(rp,sp,cp)
                        self.assertEqual(before,(sp.read_bytes(),cp.read_bytes()))

    def test_normal_replay_does_not_rewrite_or_increment(self):
        with tempfile.TemporaryDirectory() as td:
            sp,cp,rp,key=self.review_fixture(td)
            m.apply_review_result(rp,sp,cp);before=(sp.read_bytes(),cp.read_bytes())
            again=m.apply_review_result(rp,sp,cp)
            self.assertTrue(again['idempotent']);self.assertEqual(before,(sp.read_bytes(),cp.read_bytes()))
            self.assertEqual(json.loads(sp.read_text())['inbox'],{'sentinel':'keep'})

    def test_replay_rejects_state_drift(self):
        for field,value in [('reviewStatus','failed_agent'),('action','failed_agent'),('reviewNotes','altered'),('manualReviewCount',17)]:
            with self.subTest(field=field),tempfile.TemporaryDirectory() as td:
                sp,cp,rp,key=self.review_fixture(td);m.apply_review_result(rp,sp,cp)
                state=json.loads(sp.read_text());state['seen']['byPermalink'][key][field]=value;sp.write_text(json.dumps(state))
                before=(sp.read_bytes(),cp.read_bytes())
                with self.assertRaises(ValueError):m.apply_review_result(rp,sp,cp)
                self.assertEqual(before,(sp.read_bytes(),cp.read_bytes()))

    def test_replay_rejects_cache_drift(self):
        for change in ['title','duplicate']:
            with self.subTest(change=change),tempfile.TemporaryDirectory() as td:
                sp,cp,rp,key=self.review_fixture(td);m.apply_review_result(rp,sp,cp)
                cache=json.loads(cp.read_text())
                if change=='title':cache['candidates'][0]['title']='other evidence'
                else:cache['candidates'].append(copy.deepcopy(cache['candidates'][0]))
                cp.write_text(json.dumps(cache));before=(sp.read_bytes(),cp.read_bytes())
                with self.assertRaises((ValueError,RuntimeError)):m.apply_review_result(rp,sp,cp)
                self.assertEqual(before,(sp.read_bytes(),cp.read_bytes()))

    def run_monitor_case(self, posts, seen=None, contexts=None, cli=False):
        with tempfile.TemporaryDirectory() as td:
            base=Path(td);sp=base/'state.json';cp=base/'cache.json'
            sp.write_text(json.dumps({'watch':[{'name':'test','url':'https://old.reddit.com/r/test/new/','keywords':['hose'],'maxAgeHours':48,'maxCandidates':5}],
                                     'seen':{'byPermalink':seen or {}},'inbox':{'sentinel':'keep'}}))
            def ctx(url):
                p=next(x for x in posts if m.norm_permalink(x['permalink'])==url);detail=(contexts or {}).get(p['id'],{})
                if isinstance(detail,Exception):raise detail
                p={**p,**detail,'comments_coverage':{'truncated':False,'has_more':False,'unreadable_comments':False}}
                return p,[],'fixture'
            with patch.object(m, '_storage_paths', return_value=(sp, cp)),\
                 patch.object(m,'fetch_listing',return_value=(posts,'fixture')),patch.object(m,'fetch_context',side_effect=ctx),\
                 patch.object(m,'close_camofox_group',return_value=(True,{'pagesVerifiedAbsent':True})),\
                 patch.object(m.urllib.request,'urlopen',side_effect=AssertionError('network forbidden')),contextlib.redirect_stdout(io.StringIO()) as stdout:
                code=0
                if cli:
                    with patch.object(sys,'argv',['monitor']):
                        try:m.main()
                        except SystemExit as exc:code=exc.code
                else:m.run_monitor()
            return code,json.loads(cp.read_text()) if cp.exists() else None,json.loads(sp.read_text()),stdout.getvalue()

    def test_all_context_fail_cli_nonzero_but_empty_success(self):
        p=post('abc1');code,cache,state,_=self.run_monitor_case([p],contexts={'t3_abc1':RuntimeError('read failed')},cli=True)
        self.assertNotEqual(code,0);self.assertIsNotNone(cache);self.assertEqual(state['inbox'],{'sentinel':'keep'})
        code,_,_,_=self.run_monitor_case([],cli=True);self.assertEqual(code,0)

    def test_retry_order_uses_current_listing_count(self):
        first=post('abc1',0,40);second=post('abc2',4,10)
        pending={}
        for p,old in [(first,4),(second,0)]:
            pending[p['permalink']]={'action':'failed_agent','reviewStatus':'failed_agent','title':p['title'],
                'subreddit':'test','created_utc':p['created_utc'],'keywordHits':['hose'],'meta':{'commentCountAtReview':old}}
        # Four new leads consume four of the fixture budget of five; one retry slot remains.
        fresh=[post('fresh'+str(i),0,i) for i in range(4)]
        _,cache,_,_=self.run_monitor_case(fresh+[first,second],seen=pending)
        retried=[x for x in cache['candidates'] if x['candidate_origin']=='retry']
        self.assertEqual([x['permalink'] for x in retried],[first['permalink']])
        self.assertEqual(retried[0]['listing_comment_count_source'],'current_listing')
        self.assertEqual(retried[0]['num_comments_at_listing'],0)
        self.assertEqual(len(cache['candidates']),5)

    def test_partial_context_failure_remains_partial_success(self):
        a,b=post('abc1'),post('abc2')
        code,cache,_,_=self.run_monitor_case([a,b],contexts={'t3_abc2':RuntimeError('one failed')},cli=True)
        self.assertEqual(code,0)
        self.assertEqual(cache['scan']['executionStatus'],'partial')
        self.assertFalse(cache['scan']['businessFailed'])
        self.assertEqual((cache['scan']['contextReadsAttempted'],cache['scan']['contextReadsSucceeded'],cache['scan']['contextReadsFailed']),(2,1,1))

    def test_empty_successful_watch_plus_failed_watch_is_partial(self):
        with tempfile.TemporaryDirectory() as td:
            b=Path(td);sp=b/'state.json';cp=b/'cache.json'
            sp.write_text(json.dumps({'watch':[{'name':'empty','url':'https://old.reddit.com/r/test/new/','keywords':['hose']},
                                              {'name':'failed','url':'https://old.reddit.com/r/other/new/','keywords':['hose']}],
                                     'seen':{'byPermalink':{}}}))
            with patch.object(m, '_storage_paths', return_value=(sp, cp)),\
                 patch.object(m,'fetch_listing',side_effect=[([], 'mock'),RuntimeError('one watch failed')]),\
                 patch.object(m,'fetch_context',side_effect=AssertionError('must not read a thread')),contextlib.redirect_stdout(io.StringIO()):
                m.run_monitor()
            scan=json.loads(cp.read_text())['scan']
            self.assertEqual(scan['executionStatus'],'partial')
            self.assertEqual((scan['listingFetchSuccesses'],scan['listingFetchFailures']),(1,1))
            self.assertFalse(scan['businessFailed'])

    def test_crosspost_original_processed_dedup(self):
        original=post('orig1');cross=post('cross1');cross['crosspost_parent_list']=[{'permalink':original['permalink']}]
        _,cache,state,_=self.run_monitor_case([cross],seen={original['permalink']:{'action':'posted'}})
        self.assertEqual(cache['candidates'],[]);self.assertEqual(state['seen']['byPermalink'][original['permalink']]['action'],'posted')

    def test_crosspost_reverse_processed_dedup(self):
        original=post('orig1');cross=post('cross1')
        seen={cross['permalink']:{'action':'posted','canonical_permalink':original['permalink']}}
        _,cache,_,_=self.run_monitor_case([original],seen=seen)
        self.assertEqual(cache['candidates'],[])

if __name__=='__main__':unittest.main()
