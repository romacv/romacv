import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

FUNCTIONS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FUNCTIONS))
from fake_firestore import FakeFirestore
import platforms as p
import drafter
import main as app
from store import Store, StateError
from blog_commit import GitHub


class StoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        from PIL import Image
        Image.new('RGB',(1080,1920),(90,90,90)).save(self.root/'image0.jpg','JPEG')
        p.configure({'channels':{'tg-channel':-100123},'telegram_bot_token':'x'},Store(FakeFirestore()),self.root)
        self.spec = {'slug':'trip','targets':['tg-channel','tg-story'],'media':{'images':[{'path':'image0.jpg','alt':'a'}]},'copy':{'tg-story':'Дом'}}
        self.identity = {'id':'biz','user':{'id':1,'username':'romacv777'}}

    def post(self, state, reply=None):
        calls = []
        def fake(method, fields=None, files=None):
            calls.append((method,fields))
            if isinstance(reply,Exception):
                raise reply
            return {'username':'romacv777','id':-100123} if method == 'getChat' else {'id':7}
        with patch.object(p,'telegram_identity',return_value=self.identity), patch.object(p,'telegram',fake), patch.object(p,'checkpoint',lambda spec,st,target,step,action:action()):
            return p.post_telegram(self.spec,state,'tg-story'), calls

    def test_story_links_the_channel_post_and_waits_for_it(self):
        with self.assertRaises(p.Error):
            self.post({'steps':{}})
        url, calls = self.post({'steps':{'tg-channel:publish':{'status':'done','value':{'message_id':305}}}})
        area = calls[-1][1]['areas'][0]
        self.assertEqual(area['type'],{'type':'link','url':'https://t.me/romacv777/305'})
        self.assertGreater(area['position']['y_percentage'],60)

    def test_story_failure_is_reported_in_russian_with_the_reason(self):
        failure = p.Error('API failure: 400',status_code=400)
        failure.description = "Bad Request: can't download or generate the file"
        self.spec['targets'] = ['tg-story']
        with self.assertRaises(p.Error) as caught:
            self.post({'steps':{}},failure)
        self.assertIn('Telegram не принял историю',str(caught.exception))
        self.assertIn("can't download",str(caught.exception))


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(FakeFirestore())
        self.config = {'roman_user_id':1, 'roman_chat_id':1}
        p.configure(self.config, self.store, self.root)
        self.spec = json.loads((FUNCTIONS.parent / 'examples/example-work.json').read_text())
        self.spec.update(targets=['tg-channel','threads'], at='2020-01-01T00:00:00+00:00')
        self.spec.pop('blog')
        self.assets = {target:{} for target in self.spec['targets']}
        self.digest = app.asset_hash(self.spec,self.assets)
        self.store.put('items/item-1', {'status':'previewed','revision':1,'messages':[]})
        self.store.put('specs/example', {'state':'previewed','hash':self.digest,'item':'item-1','preview_message':77,'spec':self.spec,'assets':self.assets,'results':{},'errors':{}})

    def callback(self, verb='pub', digest=None, identifier=1):
        return self.store.intake({'update_id':identifier,'callback_query':{'id':str(identifier),'data':f'{verb}:example:{(digest or self.digest)[:8]}'}},self.config)

    def test_unapproved_and_changed_content_refused(self):
        self.assertFalse(self.store.claim_run('example'))
        self.callback()
        self.store.change('specs/example',lambda value:value.update(hash='changed'))
        self.assertFalse(self.store.claim_run('example'))

    def test_stale_and_duplicate_preview_cannot_approve(self):
        self.assertIsNone(self.callback(digest='oldhash8'))
        self.assertEqual(self.store.get('specs/example')['state'],'previewed')
        self.assertTrue(self.callback(identifier=2))
        self.assertIsNone(self.callback(identifier=2))
        self.assertIsNone(self.callback(identifier=3))
        self.assertEqual(len(list(self.store.db.collection('outbox').stream())),1)

    def test_completed_targets_never_repost(self):
        self.callback()
        with patch.object(app,'remove_keyboard'), patch.object(app,'tell',return_value={'message_id':999}), patch.object(app.media,'materialize'), patch.object(p,'social_session',return_value=('token','123',p.THREADS)), patch.object(p,'link_opens',return_value=True), patch.object(p,'post_telegram',return_value='https://t.me/example/1') as tg, patch.object(p,'post_social',return_value='https://www.threads.com/@romacv777/post/Abc') as th:
            app.run(self.config,self.store,None,'example',self.root)
            app.run(self.config,self.store,None,'example',self.root)
        self.assertEqual((tg.call_count,th.call_count),(1,1))

    def test_retry_skips_published_targets_and_preserves_published_precedence(self):
        self.callback()
        for results in ({},{'tg-channel':{'url':'https://t.me/example/1','verified':True}}):
            with self.subTest(results=results):
                published = {'tg-channel':{'url':'','verified':True,'deleted':True,'copy':'Исправленный текст'}}
                self.store.change('specs/example',lambda value:value.update(state='partial',results=results,published=published,errors={'threads':'setup'},attempts={}))
                self.assertTrue(self.callback('retry',identifier=10 + len(results)))
                with patch.object(app,'remove_keyboard'), patch.object(app,'tell',return_value={'message_id':999}), patch.object(app.media,'materialize'), patch.object(p,'social_session',return_value=('token','123',p.THREADS)), patch.object(p,'link_opens',return_value=True), patch.object(p,'post_telegram') as tg, patch.object(p,'post_social',return_value='https://www.threads.com/@romacv777/post/Abc') as th:
                    app.run(self.config,self.store,None,'example',self.root)
                tg.assert_not_called()
                th.assert_called_once()
                self.assertEqual(self.store.get('specs/example')['published']['tg-channel'],published['tg-channel'])
                self.assertEqual(self.store.get('items/item-1')['published']['tg-channel'],published['tg-channel'])

    def test_pending_creation_never_repeated(self):
        action = Mock(side_effect=p.Error('timeout'))
        for _ in range(2):
            with self.assertRaises((p.Error,StateError)):
                self.store.checkpoint('example','threads:create',action)
        action.assert_called_once()

    def test_blog_atomic_commit_resumes_saved_checkpoints(self):
        spec = json.loads((FUNCTIONS.parent/'examples/example-work.json').read_text())
        (self.root/'project.html').write_text('<header>Roman</header>')
        calls = []
        def api(method,path,value=None):
            calls.append((method,path,value))
            if path == '/git/ref/heads/main':
                return {'object':{'sha':'parent'}}
            if path == '/git/commits/parent':
                return {'tree':{'sha':'base'}}
            if path == '/git/trees/base?recursive=1':
                return {'tree':[]}
            return {'sha':'saved-sha'}
        github = GitHub({'github_pat':'unused','github_noreply':'example@users.noreply.github.com'})
        with patch.object(github,'api',side_effect=api):
            url = github.publish(spec,{'steps':{}},self.root)
            writes = sum(method != 'GET' for method,_,_ in calls)
            github.publish(spec,{'steps':{}},self.root)
        self.assertEqual(url,p.SITE+'/blog/example')
        self.assertEqual(sum(method != 'GET' for method,_,_ in calls),writes)
        commits = [value for method,path,value in calls if method == 'POST' and path == '/git/commits']
        self.assertEqual(len(commits),1)
        self.assertEqual(commits[0]['author']['name'],'Roman Resenchuk')
        self.assertEqual(commits[0]['parents'],['parent'])
        refs = [value for method,path,value in calls if method == 'PATCH']
        self.assertEqual(refs,[{'sha':'saved-sha','force':False}])

    def test_media_bytes_change_approval_hash(self):
        image = self.root/'asset.png'
        image.write_bytes(b'original')
        self.spec['media'] = {'images':[{'path':'asset.png','alt':'Example'}]}
        before = p.fingerprint(self.spec)
        image.write_bytes(b'changed')
        self.assertNotEqual(before,p.fingerprint(self.spec))



class PublishedTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(FakeFirestore())
        self.config = {'roman_user_id':1,'roman_chat_id':1,'telegram_bot_id':2,'channels':{'tg-channel':-100123}}
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        p.configure(self.config,self.store,self.root)
        keyboard = patch.object(app,'remove_keyboard')
        self.keyboard = keyboard.start()
        self.addCleanup(keyboard.stop)
        self.spec = {'slug':'article','kind':'work','at':'2020-01-01T00:00:00+00:00','targets':['tg-channel','threads','ig-post'],'media':{},'copy':{'tg-channel':'Здесь ошибка в тексте.','threads':'A concrete fact.\nWhat next?','ig-post':'A concrete fact. #tools #coding #software'},'approval':{}}
        self.results = {'tg-channel':{'url':'https://t.me/example/10','chat_id':-100123,'message_ids':[10,11],'caption':True,'copy':self.spec['copy']['tg-channel']},'threads':{'url':'https://www.threads.com/@romacv777/post/Abc','id':'123','copy':self.spec['copy']['threads']},'ig-post':{'url':'https://www.instagram.com/p/Abc/','id':'456','copy':self.spec['copy']['ig-post']}}
        self.source = {'item':'item-1','state':'done','spec':self.spec,'results':self.results,'preview_message':20,'published_at':1,'hash':'abcdefgh1234'}
        self.store.put('specs/article',self.source)
        self.store.put('published-messages/20',{'slug':'article'})
        self.store.put('items/item-1',{'status':'previewed','revision':1})

    def intake(self, text, reply=None, identifier=100):
        message = {'text':text,'message_id':identifier}
        if reply:
            message['reply_to_message'] = {'message_id':reply}
        return self.store.intake({'update_id':identifier,'message':message},self.config)

    def delete_tap(self, identifier=100, digest=None):
        digest = digest or self.store.get('specs/article')['hash'][:8]
        return self.store.intake({'update_id':identifier,'callback_query':{'id':str(identifier),'data':'pdel:article:' + digest}},self.config)

    def prepare(self, operation='fix'):
        self.delete_tap() if operation == 'delete' else self.intake('ошибка в посте',20)
        corrected = {'copy':{**self.spec['copy'],'tg-channel':'Здесь исправленный текст.'},'blog':None}
        with patch.object(app,'tell',return_value={'message_id':30}), patch.object(drafter,'ask',return_value=corrected):
            app.fix(self.config,self.store,'fix-100')
        return self.store.get('specs/fix-100')

    def approve(self, saved, verb='fix', identifier=101):
        return self.store.intake({'update_id':identifier,'callback_query':{'id':str(identifier),'data':verb + ':fix-100:' + saved['hash'][:8]}},self.config)

    def test_fix_edits_only_changed_targets_and_never_reposts(self):
        saved = self.prepare()
        self.assertTrue(self.approve(saved))
        with patch.object(p,'change_published',return_value='исправлено') as change, patch.object(p,'post_social') as repost, patch.object(p,'post_telegram') as post, patch.object(app,'tell',return_value={'message_id':31}):
            app.apply_fix(self.config,self.store,None,'fix-100',self.root)
        self.assertEqual([call.args[0] for call in change.call_args_list],['tg-channel'])
        repost.assert_not_called()
        post.assert_not_called()
        original = self.store.get('specs/article')
        self.assertEqual(original['results']['tg-channel']['copy'],'Здесь исправленный текст.')
        self.assertEqual(original['results']['threads'],self.results['threads'])
        self.assertEqual(self.store.get('specs/fix-100')['state'],'done')

    def test_delete_removes_each_target_once_and_partial_keeps_keyboard(self):
        saved = self.prepare('delete')
        self.assertEqual(self.delete_tap(99),'Удаление уже готовится.')
        self.approve(saved,'del')
        def change(target,*args):
            if target == 'threads':
                raise p.Error('permission',403)
            return 'удалено'
        with patch.object(p,'change_published',side_effect=change) as action, patch.object(app,'tell',return_value={'message_id':31}) as tell:
            app.apply_fix(self.config,self.store,None,'fix-100',self.root)
            self.assertEqual(sorted(call.args[0] for call in action.call_args_list),['ig-post','tg-channel','threads'])
            markup = json.dumps(tell.call_args.kwargs['reply_markup'])
            self.assertIn('pfix:article',markup)
            self.assertIn('pdel:article',markup)
            self.assertIn('retry:fix-100',markup)
            self.approve(saved,'retry',102)
            app.apply_fix(self.config,self.store,None,'fix-100',self.root)
            self.assertEqual([call.args[0] for call in action.call_args_list][3:],['threads'])
        self.assertEqual(self.store.get('specs/article')['results']['tg-channel']['deleted'],True)
        self.assertFalse(self.store.get('specs/article').get('deleted'))


if __name__ == '__main__':
    unittest.main()
