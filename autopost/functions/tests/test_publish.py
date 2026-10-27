import copy
import json
from pathlib import Path
import sys
import tempfile
import time
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
        self.spec.update(targets=['tg-channel','threads'], at='2020-01-01T00:00:00+00:00')
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

    def test_retry_deleted_source_reports_and_stops(self):
        self.callback()
        self.store.change('specs/example',lambda value:value.update(state='partial',deleted=True,errors={'threads':'setup'}))
        self.store.change('items/item-1',lambda value:value.update(deleted=True))
        self.assertTrue(self.callback('retry',identifier=2))
        item = self.store.get('items/item-1')
        with patch.object(app,'remove_keyboard') as keyboard, patch.object(app,'tell',return_value={'message_id':999}) as tell, patch.object(app.media,'materialize') as materialize, patch.object(p,'post_telegram') as tg, patch.object(p,'post_social') as th:
            app.run(self.config,self.store,None,'example',self.root)
        keyboard.assert_not_called()
        materialize.assert_not_called()
        tg.assert_not_called()
        th.assert_not_called()
        self.assertIn('удалена',tell.call_args.args[1])
        self.assertEqual(self.store.get('specs/example')['state'],'rejected')
        self.assertTrue(self.store.get('specs/example')['deleted'])
        self.assertEqual(self.store.get('items/item-1'),item)

    def test_pending_creation_never_repeated(self):
        action = Mock(side_effect=p.Error('timeout'))
        for _ in range(2):
            with self.assertRaises((p.Error,StateError)):
                self.store.checkpoint('example','threads:create',action)
        action.assert_called_once()

    def test_failed_target_caps_at_three_independent_success_saved(self):
        self.callback()
        with patch.object(app,'remove_keyboard'), patch.object(app,'tell',return_value={'message_id':999}), patch.object(app.media,'materialize'), patch.object(p,'social_session',return_value=('token','123',p.THREADS)), patch.object(p,'link_opens',return_value=True), patch.object(p,'post_telegram',return_value='https://t.me/example/1') as tg, patch.object(p,'post_social',side_effect=p.Error('setup')) as th:
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
        self.assertEqual(self.store.get('sessions/inbox')['awaiting'],{'kind':'edit','slug':'example'})
        result = self.store.intake({'update_id':42,'message':{'message_id':10,'text':'Make shorter'}},self.config)
        self.assertTrue(result.startswith('Правлю черновик') and result.endswith('Новое превью пришлю сюда.'))
        self.assertNotIn('awaiting',self.store.get('sessions/inbox'))
        saved = self.store.get('specs/example')
        self.assertEqual((saved['state'],saved['hash']),('drafting',''))
        self.assertEqual(self.store.get('items/item-1')['edits'],['Make shorter'])
        self.assertIsNotNone(self.store.get('outbox/edit-42'))

    def test_text_after_edit_tap_without_reply_is_the_edit_and_stale_tap_is_material(self):
        self.callback('edit')
        self.store.change('specs/example',lambda value:value.update(state='previewed'))
        result = self.store.intake({'update_id':43,'message':{'message_id':10,'text':'Make shorter'}},self.config)
        self.assertEqual(result['item'],'item-43')
        self.assertEqual(self.store.get('items/item-1').get('edits'),None)

    def test_quoted_reply_to_preview_copy_queues_edit_with_target(self):
        self.store.put('published-messages/7',{'slug':'example'})
        reply = {'message_id':7,'text':'tg-channel\nБольшой портрет в раме'}
        self.store.intake({'update_id':50,'message':{'message_id':11,'text':'убери это','quote':{'text':'портрет в раме','position':8},'reply_to_message':reply}},self.config)
        self.assertEqual(self.store.get('items/item-1')['edits'],['[tg-channel] цитата: "портрет в раме" — правка: убери это'])
        self.assertEqual(self.store.get('specs/example')['state'],'drafting')
        self.assertIsNotNone(self.store.get('outbox/edit-50'))
        self.store.put('published-messages/8',{'slug':'example'})
        self.store.put('specs/example',{**self.store.get('specs/example'),'state':'previewed'})
        self.store.intake({'update_id':51,'message':{'message_id':12,'text':'короче','reply_to_message':{'message_id':8,'text':'Черновик example'}}},self.config)
        self.assertEqual(self.store.get('items/item-1')['edits'][-1],'короче')
        self.store.put('published-messages/9',{'slug':'example'})
        self.store.put('specs/example',{**self.store.get('specs/example'),'state':'previewed'})
        self.store.intake({'update_id':53,'message':{'message_id':14,'text':'ярче','reply_to_message':{'message_id':9,'caption':'tg-channel, threads'}}},self.config)
        self.assertEqual(self.store.get('items/item-1')['edits'][-1],'[tg-channel, threads] ярче')

    def edit_and_follow_up(self):
        self.store.put('items/item-1',{'status':'previewed','revision':1,'messages':[{'message_id':1,'text':'Чайнатаун в Джакарте'}]})
        self.callback('edit')
        self.store.intake({'update_id':42,'message':{'message_id':10,'text':'На видео надпись'}},self.config)
        return self.store.intake({'update_id':43,'message':{'message_id':11,'text':'надо надпись наложить'}},self.config)

    def test_follow_up_text_joins_the_edit_with_new_post_button(self):
        result = self.edit_and_follow_up()
        self.assertEqual(result['text'],'Добавил к правке черновика «Чайнатаун в Джакарте».')
        self.assertEqual(result['reply_markup']['inline_keyboard'][0][0]['text'],'Это новый пост')
        self.assertLessEqual(len(result['reply_markup']['inline_keyboard'][0][0]['callback_data'].encode()),64)
        item = self.store.get('items/item-1')
        self.assertEqual((item['edits'],item['revision'],item['status']),(['На видео надпись','надо надпись наложить'],3,'queued'))
        self.assertEqual(self.store.get('outbox/edit-43')['payload'],{'item':'item-1','step':'draft','revision':3})
        self.assertIsNone(self.store.get('items/item-43'))
        self.assertEqual(self.store.get('specs/example')['state'],'drafting')

    def test_new_post_button_moves_the_follow_up_into_its_own_item(self):
        data = self.edit_and_follow_up()['reply_markup']['inline_keyboard'][0][0]['callback_data']
        result = self.store.intake({'update_id':44,'callback_query':{'id':'q44','data':data}},self.config)
        self.assertEqual(result,'Готовлю пост из текста «надо надпись наложить». Превью пришлю сюда.')
        self.assertEqual(self.store.get('items/item-1')['edits'],['На видео надпись'])
        self.assertEqual(self.store.get('items/item-43')['messages'][0]['text'],'надо надпись наложить')
        self.assertEqual(self.store.get('outbox/draft-43')['payload'],{'item':'item-43','step':'draft','revision':1})
        self.assertEqual(self.store.get('outbox/unmove-43')['payload'],{'item':'item-1','step':'draft','revision':4})
        self.assertIsNone(self.store.intake({'update_id':45,'callback_query':{'id':'q45','data':data}},self.config))

    def test_new_post_button_is_stale_once_the_draft_is_previewed(self):
        data = self.edit_and_follow_up()['reply_markup']['inline_keyboard'][0][0]['callback_data']
        self.store.change('specs/example',lambda value:value.update(state='previewed'))
        self.assertIsNone(self.store.intake({'update_id':44,'callback_query':{'id':'q44','data':data}},self.config))
        self.assertEqual(len(self.store.get('items/item-1')['edits']),2)

    def test_text_after_window_or_preview_is_a_new_post(self):
        self.edit_and_follow_up()
        self.store.change('sessions/inbox',lambda value:value['editing'].update(until=0))
        self.assertEqual(self.store.intake({'update_id':50,'message':{'message_id':12,'text':'поздно'}},self.config)['item'],'item-50')
        self.assertNotIn('editing',self.store.get('sessions/inbox'))
        self.store.change('sessions/inbox',lambda value:value.update(editing={'slug':'example','until':time.time() + 600}))
        self.store.change('specs/example',lambda value:value.update(state='previewed'))
        self.assertEqual(self.store.intake({'update_id':51,'message':{'message_id':13,'text':'после превью'}},self.config)['item'],'item-51')
        self.assertEqual(len(self.store.get('items/item-1')['edits']),2)

    def test_preview_ends_the_follow_up_window(self):
        self.edit_and_follow_up()
        spec = {**self.spec,'slug':'example'}
        with patch.object(app.platforms,'telegram',return_value={'message_id':200}), patch.object(app.platforms,'telegram_identity'), patch.object(app.media,'materialize'):
            app.preview(self.config,self.store,None,'item-1',spec,self.assets,self.root)
        self.assertNotIn('editing',self.store.get('sessions/inbox'))

    def test_edit_jobs_are_delayed_like_draft_jobs(self):
        self.edit_and_follow_up()
        self.assertEqual((self.store.get('outbox/edit-42')['delay'],self.store.get('outbox/edit-43')['delay']),(10,10))

    def test_preview_header_has_no_id_and_names_match_the_ack(self):
        ack = self.edit_and_follow_up()['text']
        spec = {**self.spec,'slug':'example'}
        with patch.object(app.platforms,'telegram',return_value={'message_id':200}) as telegram, patch.object(app.platforms,'telegram_identity'), patch.object(app.media,'materialize'):
            app.preview(self.config,self.store,None,'item-1',spec,self.assets,self.root)
        header = telegram.call_args.args[1]['text']
        self.assertEqual(header,'Черновик «Чайнатаун в Джакарте» готов. Опубликовать, править или отклонить?')
        self.assertNotIn('example',header)
        self.assertNotIn('item-',header)
        self.assertIn('«Чайнатаун в Джакарте»',ack)
        self.assertEqual(app.post_name(self.store,self.store.get('specs/example')),' «Чайнатаун в Джакарте»')

    def test_stale_draft_result_is_not_previewed(self):
        self.store.put('items/item-1',{'status':'drafting','revision':3,'messages':[]})
        self.assertTrue(app.superseded(self.store,{'item':'item-1','revision':2}))
        self.assertFalse(app.superseded(self.store,{'item':'item-1','revision':3}))

    def test_reply_to_unknown_message_stays_new_material(self):
        result = self.store.intake({'update_id':52,'message':{'message_id':13,'text':'идея','reply_to_message':{'message_id':99,'text':'x'}}},self.config)
        self.assertEqual(result['text'],'Готовлю пост из текста «идея». Превью пришлю сюда.')

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

    def test_album_acknowledged_once(self):
        replies = [self.store.intake({'update_id':identifier,'message':{'message_id':identifier,'media_group_id':'123','photo':[{'file_id':'photo'}]}},self.config) for identifier in (10,11)]
        self.assertEqual(replies[0]['text'],'Готовлю пост из 1 фото. Превью пришлю сюда.')
        self.assertIsNone(replies[1]['text'])
        self.assertEqual(self.store.get('items/album-123')['revision'],2)
        self.assertIsNotNone(self.store.get('outbox/draft-11'))

    def test_preview_sends_shared_photos_once(self):
        photos = {'image0.jpg':{'object':'a'},'image1.jpg':{'object':'b'}}
        spec = {**self.spec,'slug':'album','targets':['tg-channel','threads'],'copy':{'tg-channel':'Channel copy','threads':'Threads copy'}}
        sent = []
        def telegram(method,fields,files=None):
            sent.append((method,fields,files))
            base = 100 + len(sent)
            return [{'message_id':base},{'message_id':base + 50}] if method == 'sendMediaGroup' else {'message_id':base}
        with patch.object(app.platforms,'telegram',side_effect=telegram), patch.object(app.platforms,'telegram_identity'), patch.object(app.media,'materialize'):
            app.preview(self.config,self.store,None,'item-1',spec,{'tg-channel':photos,'threads':photos},self.root)
        self.assertEqual([call[0] for call in sent[:-1]],['sendMediaGroup','sendMessage','sendMessage'])
        group = sent[0][1]['media']
        self.assertEqual((len(group),group[0]['caption']),(2,'tg-channel, threads'))
        self.assertEqual(sorted(sent[0][2]),['photo0','photo1'])
        self.assertEqual([call[1]['text'] for call in sent[1:3]],['tg-channel\nChannel copy','threads\nThreads copy'])
        for identifier in (101,151,102,103,104):
            self.assertEqual(self.store.get('published-messages/' + str(identifier)),{'slug':'album'})

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
