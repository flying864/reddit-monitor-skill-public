"""Offline synthetic DOM, actual JavaScript extraction and CLI regressions."""
import ast
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('hardening_monitor', Path(__file__).with_name('reddit_monitor_tick_noagent.py'))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

NODE = shutil.which('node')


class Hardening(unittest.TestCase):
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

    def test_media_host_path_resize_gallery(self):
        urls = ['https://old.reddit.com/domain/i.redd.it/', 'https://evil.test/?u=https://i.redd.it/a.png', 'https://i.redd.it.evil.test/a.png', 'https://v.redd.it/a', 'https://preview.redd.it/a.png?width=108', 'https://preview.redd.it/a.png?width=640', 'https://i.redd.it/a.png', 'https://www.reddit.com/gallery/abc']
        selected = m.select_media([{'url': u} for u in urls])
        self.assertEqual([x['url'] for x in selected], urls[-2:])
        self.assertEqual(len(m.select_media([{'url':f'https://i.redd.it/{i}.jpg'} for i in range(12)])), 6)

    def test_synthetic_media_fixture(self):
        fixture=json.loads((Path(__file__).parent/'fixtures/reddit_monitor_dom.json').read_text())
        media=fixture['media']
        raw=[{'url':item.get('url') or item.get('href') or item.get('src') or ''} for item in media]
        selected=m.select_media(raw)
        self.assertTrue(selected)
        self.assertLessEqual(len(selected),6)
        self.assertFalse(any('/domain/' in item['url'] for item in selected))
        self.assertTrue(all(item['url'] in [x['url'] for x in raw] for item in selected))

    def test_intent_negation_quote_and_primary_sale(self):
        for title in ['Not for sale: hose help?', 'Hose repair help: seller said "for sale"', 'How to fix a hose listed for sale?']:
            self.assertTrue(m.candidate_fit({'title':title}, ['hose'])[0], title)
        for title in ['Hose for sale, not taking offers?', 'Showcase: my hose setup?', 'What is this worth? Hose']:
            self.assertFalse(m.candidate_fit({'title':title}, ['hose'])[0], title)
        self.assertTrue(m.word_hit('hoses', 'hose'))
        self.assertFalse(m.word_hit('M22s', 'm22'))
        self.assertFalse(m.word_hit('house', 'hose'))

    def test_context_identity_url_and_fullname(self):
        url='https://old.reddit.com/r/test/comments/abc/old/'
        good={'url':'https://www.reddit.com/r/test/comments/abc/new/', 'logged_in_user':'FixtureReader', 'post':{'fullname':'t3_abc','media':[]},'comments':[]}
        with patch.object(m,'camofox_navigate'), patch.object(m,'camofox_evaluate',return_value=good): m.fetch_context(url)
        for changes in [{'logged_in_user':'other'},{'url':'https://evil.test/r/test/comments/abc/new/'},{'url':'https://old.reddit.com/r/test/comments/def/new/'},{'post':{'fullname':'t3_wrong'}}]:
            with patch.object(m,'camofox_navigate'),patch.object(m,'camofox_evaluate',return_value={**good,**changes}):
                with self.assertRaises(ValueError):m.fetch_context(url)
        with patch.object(m,'camofox_navigate') as nav:
            with self.assertRaises(ValueError):m.fetch_context('https://evil.test/r/test/comments/abc/x/')
            nav.assert_not_called()

    @unittest.skipUnless(NODE, 'Node.js required for JavaScript DOM fixture')
    def test_actual_context_javascript_shape(self):
        source=Path(m.__file__).read_text()
        tree=ast.parse(source)
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='fetch_context')
        expr=next(n.args[0].value for n in ast.walk(fn) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='camofox_evaluate')
        harness="""
const leaf = text => ({textContent:text,innerText:text});
const media = [{href:'https://old.reddit.com/domain/i.redd.it/'},{src:'https://preview.redd.it/a.png?width=108'},{href:'https://i.redd.it/a.png'}];
const comment={dataset:{fullname:'t1_x'},querySelector:s=>s===':scope > .entry .author'?leaf('parent'):s===':scope > .entry .usertext-body .md'?leaf('direct body'):null};
const post={dataset:{fullname:'t3_abc',targetParentFullname:'t3_orig',commentsCount:'1'},querySelector:s=>null,querySelectorAll:s=>media};
global.location={href:'https://old.reddit.com/r/test/comments/abc/x/'};
global.document={title:'test',querySelector:s=>s==='.thing.link'?post:s==='.user a'?leaf('FixtureReader'):null,querySelectorAll:s=>[comment]};
"""
        result=json.loads(subprocess.check_output([NODE,'-e',harness+'console.log(JSON.stringify('+expr+'));'],text=True,env={},timeout=10))
        self.assertEqual(result['post']['crosspost_parent_fullname'],'t3_orig')
        self.assertEqual(result['comments'][0]['body'],'direct body')
        self.assertEqual(result['comments'][0]['author'],'parent')
        self.assertEqual([x['url'] for x in m.select_media(result['post']['media'])],['https://i.redd.it/a.png'])

    @unittest.skipUnless(NODE, 'Node.js required for JavaScript DOM fixture')
    def test_actual_listing_javascript_parent_not_button(self):
        tree=ast.parse(Path(m.__file__).read_text())
        fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='fetch_listing')
        expr=next(n.args[0].value for n in ast.walk(fn) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='camofox_evaluate')
        fixture=json.loads((Path(__file__).parent/'fixtures/reddit_monitor_dom.json').read_text())
        actual=fixture['feed']
        harness='const samples='+json.dumps(actual)+';'+"""
const nodes=samples.map(p=>({dataset:{crosspostFullname:p.crosspostFullname,targetParentFullname:p.targetParentFullname,permalink:p.permalink},querySelector:s=>null}));
global.location={href:'https://old.reddit.com/user/fixturereader/m/fixture-feed/new/'};
global.document={title:'test',querySelector:s=>null,querySelectorAll:s=>s==='.thing.link'?nodes:[]};
"""
        result=json.loads(subprocess.check_output([NODE,'-e',harness+'console.log(JSON.stringify('+expr+'));'],text=True,env={},timeout=10))
        self.assertEqual(len(result['posts']), len(actual))
        for original,extracted in zip(actual,result['posts']):
            self.assertEqual(extracted['crosspost_parent_fullname'],original.get('targetParentFullname'))
        cross=next(p for p in result['posts'] if '/testchild/' in p['permalink'])
        self.assertEqual(m.canonical_permalink(cross),'t3_testparent')

    def test_cli_all_context_failure_persists(self):
        with tempfile.TemporaryDirectory() as td:
            state,cache=Path(td)/'state.json',Path(td)/'cache.json'
            p={'permalink':'/r/test/comments/abc/help/','title':'Hose help?','num_comments':0,'created_utc':time.time(),'subreddit':'test'}
            state.write_text(json.dumps({'watch':[{'url':'https://old.reddit.com/r/test/','keywords':['hose']}],'seen':{'byPermalink':{}}}))
            with patch.object(m, '_storage_paths', return_value=(state, cache)),patch.object(m,'fetch_listing',return_value=([p],'mock')),patch.object(m,'fetch_context',side_effect=RuntimeError('offline')),patch.object(m,'close_camofox_group',return_value=(True,{})),patch.object(sys,'argv',['monitor']),contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as exc:m.main()
            self.assertEqual(exc.exception.code,2)
            scan=json.loads(cache.read_text())['scan']
            self.assertEqual((scan['contextReadsAttempted'],scan['contextReadsSucceeded'],scan['contextReadsFailed']),(1,0,1))
            self.assertTrue(scan['businessFailed'])
            entry=next(iter(json.loads(state.read_text())['seen']['byPermalink'].values()))
            self.assertEqual(entry['action'],'skipped_context_unverified')
            self.assertEqual(entry['retryCount'],1)

    def test_crosspost_bidirectional_and_pending_survives(self):
        from test_reddit_zero_priority import IntegrationTests, post
        helper=IntegrationTests()
        original=post('orig',0); cross={**post('cross',0),'crosspost_parent_fullname':'t3_orig'}
        for listed,known in [(cross,original),(original,cross)]:
            key=m.norm_permalink(known['permalink'])
            entry={'action':'posted','created_utc':known['created_utc'],'subreddit':'test'}
            if known is cross:entry['canonical_permalink']='t3_orig'
            cache,_,_=helper.run_case([listed],seen={key:entry})
            self.assertEqual(cache['candidates'],[])
        key=m.norm_permalink(cross['permalink'])
        cache,state,_=helper.run_case([original],seen={key:{'action':'failed_agent','canonical_permalink':'t3_orig','title':'Hose help?','keywordHits':['hose'],'created_utc':cross['created_utc'],'subreddit':'test'}})
        self.assertEqual(len(cache['candidates']),1)
        self.assertEqual(cache['candidates'][0]['candidate_origin'],'retry')
        self.assertEqual(state['seen']['byPermalink'][key]['canonical_permalink'],'t3_orig')

    def test_review_replay_and_drift(self):
        with tempfile.TemporaryDirectory() as td:
            s,c,r=[Path(td)/name for name in ('state.json','cache.json','result.json')]
            key='https://old.reddit.com/r/test/comments/abc/help/'
            s.write_text(json.dumps({'contentRunId':'run','seen':{'byPermalink':{key:{'action':'candidate_pending_semantic_review'}}}}))
            c.write_text(json.dumps({'contentRunId':'run','candidates':[{'permalink':key,'selftext':'real body'}]}))
            r.write_text(json.dumps({'contentRunId':'run','permalink':key,'result':'needs_manual_review','notes':'missing size'}))
            self.assertEqual(m.apply_review_result(r,s,c)['manualReviewCount'],1)
            self.assertTrue(m.apply_review_result(r,s,c)['idempotent'])
            baseline=json.loads(s.read_text())
            for field,value in [('action','failed_agent'),('reviewStatus','recommended'),('reviewNotes','changed'),('manualReviewCount',2)]:
                data=json.loads(json.dumps(baseline));data['seen']['byPermalink'][key][field]=value;s.write_text(json.dumps(data))
                with self.assertRaises(ValueError):m.apply_review_result(r,s,c)
            s.write_text(json.dumps(baseline))
            data=json.loads(c.read_text());data['candidates'][0]['selftext']='tampered';c.write_text(json.dumps(data))
            with self.assertRaises(ValueError):m.apply_review_result(r,s,c)

    def test_protection_all_fields(self):
        for field in ('action','status','reviewStatus'):
            for value in m.PROTECTED_VALUES:
                self.assertTrue(m.protected_entry({field:value}))
                self.assertTrue(m.protected_entry({'meta':{field:value}}))
        self.assertFalse(m.protected_entry({'action':'recommended'}))

if __name__=='__main__':unittest.main()
