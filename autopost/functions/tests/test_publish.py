import copy
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
from blog_commit import build_files, GitHub


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(FakeFirestore())
        self.config = {'roman_user_id':1, 'roman_chat_id':1}
        p.configure(self.config, self.store, self.root)
        self.spec = json.loads((FUNCTIONS.parent / 'examples/example-work.json').read_text())
        self.spec.update(targets=['tg-work','threads'], at='2020-01-01T00:00:00+00:00')
        self.spec.pop('blog')
        self.assets = {target:{} for target in self.spec['targets']}
        self.digest = app.asset_hash(self.spec,self.assets)
        self.store.put('items/item-1', {'status':'previewed','revision':1,'messages':[]})
        self.store.put('specs/example', {'state':'previewed','hash':self.digest,'item':'item-1','spec':self.spec,'assets':self.assets,'results':{},'errors':{}})

    def callback(self, verb='pub', digest=None, identifier=1):
        return self.store.intake({'update_id':identifier,'callback_query':{'id':str(identifier),'data':f'{verb}:example:{(digest or self.digest)[:8]}'}},self.config)

    def test_offline_example(self):
        example = json.loads((FUNCTIONS.parent/'examples/example-work.json').read_text())
        with patch.object(p,'request',side_effect=AssertionError('network')), patch.object(p,'key',side_effect=AssertionError('credentials')):
            self.assertEqual(drafter.validate(example),[])

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
        with patch.object(app,'remove_keyboard'), patch.object(app,'tell'), patch.object(app.media,'materialize'), patch.object(p,'social_session',return_value=('token','123',p.THREADS)), patch.object(p,'link_opens',return_value=True), patch.object(p,'post_telegram',return_value='https://t.me/example/1') as tg, patch.object(p,'post_social',return_value='https://www.threads.com/@romacv777/post/Abc') as th:
            app.run(self.config,self.store,None,'example',self.root)
            app.run(self.config,self.store,None,'example',self.root)
        self.assertEqual((tg.call_count,th.call_count),(1,1))

    def test_pending_creation_never_repeated(self):
        action = Mock(side_effect=p.Error('timeout'))
        for _ in range(2):
            with self.assertRaises((p.Error,StateError)):
                self.store.checkpoint('example','threads:create',action)
        action.assert_called_once()

    def test_failed_target_caps_at_three_independent_success_saved(self):
        self.callback()
        with patch.object(app,'remove_keyboard'), patch.object(app,'tell'), patch.object(app.media,'materialize'), patch.object(p,'social_session',return_value=('token','123',p.THREADS)), patch.object(p,'link_opens',return_value=True), patch.object(p,'post_telegram',return_value='https://t.me/example/1') as tg, patch.object(p,'post_social',side_effect=p.Error('setup')) as th:
            for identifier in range(2,7):
                app.run(self.config,self.store,None,'example',self.root)
                self.callback('retry',identifier=identifier)
        self.assertEqual((tg.call_count,th.call_count),(1,3))
        self.assertEqual(self.store.get('specs/example')['attempts']['threads'],3)

    def test_threads_uses_utf8_bytes_and_question(self):
        self.spec['copy']['threads'] = 'Я'*251 + '?'
        self.assertTrue(any('exceeds' in e for e in drafter.validate(self.spec)))
        self.spec['copy']['threads'] = 'A fact.\n#one #two'
        self.assertTrue(any('question' in e for e in drafter.validate(self.spec)))

    def test_personal_blog_forbidden(self):
        spec = json.loads((FUNCTIONS.parent/'examples/example-work.json').read_text())
        spec['kind'] = 'personal'
        self.assertTrue(any('work' in e for e in drafter.validate(spec)))

    def test_blog_plan_has_all_files_without_writing_public_tree(self):
        (self.root/'project.html').write_text('<header><div>Roman</div></header>')
        spec = json.loads((FUNCTIONS.parent/'examples/example-work.json').read_text())
        plan = build_files(spec,self.root)
        article = plan['files']['blog/example.html']
        self.assertIn('BlogPosting',article)
        self.assertIn('/css/style.css',article)
        self.assertEqual(article.count('<h2>'),3)
        self.assertIn('blog/feed.xml',plan['files'])
        self.assertIn(p.SITE+'/blog/example',plan['files']['sitemap.xml'])
        self.assertFalse((self.root/'blog').exists())
        (self.root/'blog').mkdir()
        (self.root/'blog/example.html').write_text(article)
        with self.assertRaises(p.Error):
            build_files(spec,self.root)

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

    def test_instagram_reel_public_url_and_shortcodes(self):
        self.spec.update(targets=['ig-reel'],copy={'ig-reel':'A fact. #ios #swift #apps'},media={'video':'unused','cover':'unused'})
        calls = []
        replies = iter([{'id':'111'},{'id':'222'},{'shortcode':'Ab_C'}])
        def api(method,url,fields=None,**kwargs):
            calls.append((url,fields))
            return next(replies)
        with patch.object(p,'request',side_effect=api), patch.object(p,'ready'), patch.object(p,'quota'), patch.object(p,'media_files',return_value={'video':Path('video.mp4'),'cover':Path('cover.jpg')}), patch.object(p,'probe'):
            result = p.post_social(self.spec,{'steps':{}},'ig-reel',{'video':'https://media.example/video.mp4','cover':'https://media.example/cover.jpg'},('token','123',p.IG))
        self.assertEqual(result,'https://www.instagram.com/reel/Ab_C/')
        self.assertEqual(calls[0][1]['video_url'],'https://media.example/video.mp4')
        self.assertNotIn('upload_type',calls[0][1])

    def test_media_bytes_change_approval_hash(self):
        image = self.root/'asset.png'
        image.write_bytes(b'original')
        self.spec['media'] = {'images':[{'path':'asset.png','alt':'Example'}]}
        before = p.fingerprint(self.spec)
        image.write_bytes(b'changed')
        self.assertNotEqual(before,p.fingerprint(self.spec))

    def test_instagram_pins_professional_user_id(self):
        with patch.object(p,'request',return_value={'data':[{'id':'11','user_id':'22','username':'romacv777','account_type':'Media_Creator'}]}):
            self.assertEqual(p.personal_identity('instagram','token')['id'],'22')
        with patch.object(p,'request',return_value={'user_id':'22','username':'someone-else','account_type':'Media_Creator'}):
            with self.assertRaises(p.Error):
                p.personal_identity('instagram','token')

    def test_draft_claim_never_repeats_running_or_old_revision(self):
        self.store.put('items/job',{'status':'queued','revision':2})
        self.assertFalse(self.store.claim_item('job',1))
        self.assertTrue(self.store.claim_item('job',2))
        self.assertFalse(self.store.claim_item('job',2))

    def test_edit_invalidates_approval_and_queues_revision(self):
        self.callback('edit')
        self.store.put('edits/9',{'slug':'example'})
        self.store.change('specs/example',lambda value:value.update(edit_prompt='9'))
        self.store.intake({'update_id':42,'message':{'message_id':10,'text':'Make shorter','reply_to_message':{'message_id':9}}},self.config)
        saved = self.store.get('specs/example')
        self.assertEqual((saved['state'],saved['hash']),('drafting',''))
        self.assertEqual(self.store.get('items/item-1')['edits'],['Make shorter'])
        self.assertIsNotNone(self.store.get('outbox/edit-42'))

    def test_album_revision_and_speech_limit(self):
        for identifier in (10,11):
            self.store.intake({'update_id':identifier,'message':{'message_id':identifier,'media_group_id':'123','photo':[{'file_id':'photo'}]}},self.config)
        self.assertFalse(self.store.claim_item('album-123',1))
        self.assertTrue(self.store.claim_item('album-123',2))
        self.store.reserve_speech(55*60,'source')
        with self.assertRaises(StateError):
            self.store.reserve_speech(1,'other')
        with self.assertRaises(StateError):
            self.store.reserve_speech(1,'source')

    def test_transaction_failure_rolls_back(self):
        def operation(tx):
            self.store.put('specs/rollback',{'state':'approved'},tx)
            raise ValueError('abort')
        with self.assertRaises(ValueError):
            self.store.atomic(operation)
        self.assertIsNone(self.store.get('specs/rollback'))


class DraftCorrectionTests(unittest.TestCase):
    def test_packaged_prompt_and_exactly_one_correction(self):
        response = Mock(content=[Mock(type='text',text='{}')])
        client = Mock()
        client.messages.create.return_value = response
        import anthropic
        with patch.object(anthropic,'Anthropic',return_value=client), patch.object(drafter,'language_errors'):
            validator = Mock(side_effect=[['missing field'],[]])
            self.assertEqual(drafter.ask({'anthropic_key':'unused'}, {'text':'input'}, [], validator),{})
        self.assertEqual(client.messages.create.call_count,2)
        self.assertIn('EDL JSON schema',client.messages.create.call_args.kwargs['system'])
        client.reset_mock()
        with patch.object(anthropic,'Anthropic',return_value=client):
            with self.assertRaises(p.Error):
                drafter.ask({'anthropic_key':'unused'},{},[],lambda value:['invalid'])
        self.assertEqual(client.messages.create.call_count,2)


if __name__ == '__main__':
    unittest.main()
