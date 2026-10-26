import base64
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fake_firestore import FakeFirestore
from store import Store, StateError
import main as app
import platforms as p
import drafter
from blog_commit import build_files, GitHub


class PublishedTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(FakeFirestore())
        self.config = {'roman_user_id':1,'roman_chat_id':1,'telegram_bot_id':2,'channels':{'tg-work':-100123}}
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        p.configure(self.config,self.store,self.root)
        keyboard = patch.object(app,'remove_keyboard')
        self.keyboard = keyboard.start()
        self.addCleanup(keyboard.stop)
        self.spec = {'slug':'article','kind':'work','at':'2020-01-01T00:00:00+00:00','targets':['tg-work','threads','ig-post'],'media':{},'copy':{'tg-work':'Здесь ошибка в тексте.','threads':'A concrete fact.\nWhat next?','ig-post':'A concrete fact. #tools #coding #software'},'approval':{}}
        self.results = {'tg-work':{'url':'https://t.me/example/10','chat_id':-100123,'message_ids':[10,11],'caption':True,'copy':self.spec['copy']['tg-work']},'threads':{'url':'https://www.threads.com/@romacv777/post/Abc','id':'123','copy':self.spec['copy']['threads']},'ig-post':{'url':'https://www.instagram.com/p/Abc/','id':'456','copy':self.spec['copy']['ig-post']}}
        self.source = {'item':'item-1','state':'done','spec':self.spec,'results':self.results,'preview_message':20,'published_at':1}
        self.store.put('specs/article',self.source)
        self.store.put('items/item-1',{'status':'previewed','revision':1})

    def intake(self, text, reply=None, identifier=100):
        message = {'text':text,'message_id':identifier}
        if reply:
            message['reply_to_message'] = {'message_id':reply}
        return self.store.intake({'update_id':identifier,'message':message},self.config)

    def prepare(self, operation='fix'):
        self.intake('удали пост' if operation == 'delete' else 'поправь ошибку в посте',20)
        corrected = {'copy':{**self.spec['copy'],'tg-work':'Здесь исправленный текст.'},'blog':None}
        with patch.object(app,'tell',return_value={'message_id':30}), patch.object(drafter,'ask',return_value=corrected):
            app.fix(self.config,self.store,'fix-100')
        return self.store.get('specs/fix-100')

    def approve(self, saved, verb='fix', identifier=101, digest=None):
        return self.store.intake({'update_id':identifier,'callback_query':{'id':str(identifier),'data':verb + ':fix-100:' + (digest or saved['hash'][:8])}},self.config)

    def test_matching_reply_report_preview_and_link(self):
        self.store.put('published-messages/21',{'slug':'article'})
        for identifier, reply, text in [(100,20,'поправь ошибку'),(101,21,'исправь слово'),(102,None,'поправь https://t.me/example/10')]:
            self.intake(text,reply,identifier)
            self.assertEqual(self.store.get('specs/fix-' + str(identifier))['source'],'article')
        self.assertIsNotNone(self.store.get('outbox/fix-100'))

    def test_unknown_post_never_falls_back_and_new_material_stays_new(self):
        for identifier,text in enumerate(['исправь https://t.me/other/99','удали пост про поездку','поправь пост «другая статья»'],200):
            self.assertIn('Какой пост',self.intake(text,identifier=identifier))
            self.assertIsNone(self.store.get('specs/fix-' + str(identifier)))
        result = self.intake('Новый материал для публикации',identifier=250)
        self.assertEqual(result['item'],'item-250')

    def test_command_requires_explicit_reference(self):
        self.assertIn('Какой пост',self.intake('удали пост'))
        self.assertIsNone(self.store.get('specs/fix-100'))
        self.assertIsNone(self.intake('удали пост'))

    def test_false_positive_material_never_starts_fix(self):
        for identifier,text in enumerate(['Удалось','исправил баг','справка','fixed','поправь удалённое слово'],300):
            if identifier == 304:
                self.intake(text,20,identifier)
                self.assertEqual(self.store.get('specs/fix-304')['operation'],'fix')
            else:
                result = self.intake(text,20,identifier)
                self.assertEqual(result['item'],'item-' + str(identifier))
                self.assertIsNone(self.store.get('specs/fix-' + str(identifier)))

    def test_plain_message_does_not_scan_specs(self):
        with patch.object(self.store.db,'collection',side_effect=AssertionError('scan')):
            self.intake('Удалось опубликовать материал')

    def test_hash_transaction_and_rejection(self):
        saved = self.prepare()
        self.assertIsNone(self.approve(saved,digest='00000000'))
        self.assertTrue(self.approve(saved,identifier=102))
        self.assertIsNone(self.approve(saved,identifier=103))
        self.assertEqual(self.store.get('outbox/callback-' + hashlib.sha256(b'102').hexdigest())['payload']['step'],'apply-fix')
        self.store.change('specs/fix-100',lambda value:value.update(spec={**value['spec'],'copy':{}}))
        with patch.object(p,'change_published') as change, patch.object(app,'remove_keyboard'), patch.object(app,'tell',return_value={'message_id':31}):
            app.apply_fix(self.config,self.store,None,'fix-100',self.root)
        change.assert_not_called()
        self.assertIn('Approval hash mismatch',self.store.get('specs/fix-100')['errors']['tg-work'])
        self.assertEqual(self.store.get('specs/fix-100')['state'],'rejected')

    def test_preview_only_changed_lines_and_language_validator(self):
        corrected = {'copy':{**self.spec['copy'],'tg-work':'Русская строка.\nИсправлено.'},'blog':None}
        self.spec['copy']['tg-work'] = 'Русская строка.\nОшибка.'
        self.source['spec'] = self.spec
        self.source['results']['tg-work']['copy'] = self.spec['copy']['tg-work']
        self.store.put('specs/article',self.source)
        self.intake('поправь ошибку',20)
        def ask(config,payload,images,validator):
            self.assertEqual(payload['mode'],'fix')
            self.assertEqual(validator(corrected),[])
            invalid = copy.deepcopy(corrected)
            invalid['copy']['tg-work'] = 'An English sentence.'
            self.assertTrue(any('language mismatch' in e for e in validator(invalid)))
            return corrected
        with patch.object(drafter,'ask',side_effect=ask), patch.object(app,'tell',return_value={'message_id':30}) as tell:
            app.fix(self.config,self.store,'fix-100')
        text = '\n'.join(call.args[1] for call in tell.call_args_list)
        self.assertIn('- Ошибка.',text)
        self.assertIn('+ Исправлено.',text)
        self.assertNotIn('Русская строка.',text)

    def test_resume_skips_completed_targets_and_pending_never_repeats(self):
        saved = self.prepare('delete')
        self.approve(saved,'del')
        def change(target,*args):
            if target == 'threads':
                raise p.Error('permission',403)
            return 'удалено'
        with patch.object(p,'change_published',side_effect=change) as action, patch.object(app,'tell',return_value={'message_id':31}):
            app.apply_fix(self.config,self.store,None,'fix-100',self.root)
            self.assertEqual(action.call_count,3)
            self.approve(saved,'retry',102)
            app.apply_fix(self.config,self.store,None,'fix-100',self.root)
            self.assertEqual(action.call_count,4)
            self.assertEqual(action.call_args.args[0],'threads')
        self.assertEqual(self.store.get('specs/article')['results']['tg-work']['deleted'],True)
        action = unittest.mock.Mock(side_effect=p.Error('timeout'))
        with self.assertRaises(p.Error):
            self.store.checkpoint('another','tg-work:apply',action)
        with self.assertRaises(StateError):
            self.store.checkpoint('another','tg-work:apply',action)
        action.assert_called_once()

    def test_manual_target_report_has_link_and_no_call(self):
        saved = self.prepare()
        self.store.change('specs/fix-100',lambda value:value['spec']['copy'].update({'ig-post':'A corrected fact. #tools #coding #software'}))
        self.store.change('specs/fix-100',lambda value:value.update(hash=app.fix_hash(value)))
        saved = self.store.get('specs/fix-100')
        self.approve(saved)
        with patch.object(p,'telegram_identity',return_value={'id':-100123}), patch.object(p,'telegram',return_value=True), patch.object(p,'request',side_effect=AssertionError('unsupported network call')), patch.object(app,'tell',return_value={'message_id':31}) as tell:
            app.apply_fix(self.config,self.store,None,'fix-100',self.root)
        report = tell.call_args.args[1]
        self.assertIn('Instagram: правь вручную https://www.instagram.com/p/Abc/',report)
        self.assertEqual(self.store.get('specs/article')['results']['ig-post']['copy'],self.results['ig-post']['copy'])

    def test_missing_channel_rights_does_not_block_other_targets(self):
        saved = self.prepare('delete')
        self.approve(saved,'del')
        def telegram(method,*args):
            return {'getMe':{'id':2},'getChat':{'id':-100123,'type':'channel'},'getChatMember':{'status':'administrator','can_post_messages':True}}[method]
        with patch.object(p,'telegram',side_effect=telegram), patch.object(p,'social_session',return_value=('token','1',p.THREADS)), patch.object(p,'quota'), patch.object(p,'request',return_value={'success':True}), patch.object(app,'tell',return_value={'message_id':31}) as tell:
            app.apply_fix(self.config,self.store,None,'fix-100',self.root)
            self.assertIn('нет права на правку в канале',tell.call_args.args[1])
            self.assertIn('Threads: удалено',tell.call_args.args[1])
            self.assertEqual(p.telegram_identity('tg-work')['id'],-100123)

    def test_album_edit_first_and_delete_all(self):
        with patch.object(p,'telegram_identity',return_value={'id':-100123}), patch.object(p,'telegram',return_value=True) as telegram:
            p.change_published('tg-work',self.results['tg-work'],self.spec,'fix',self.root)
            self.assertEqual(telegram.call_args.args[0],'editMessageCaption')
            self.assertEqual(telegram.call_args.args[1]['message_id'],10)
            p.change_published('tg-work',self.results['tg-work'],self.spec,'delete',self.root)
            self.assertEqual(telegram.call_args.args[1]['message_ids'],[10,11])

    def test_album_publication_checkpoint_keeps_every_message(self):
        spec = {**self.spec,'targets':['tg-work'],'media':{'images':[{'path':'a.jpg','alt':'First'},{'path':'b.jpg','alt':'Second'}]}}
        messages = [{'message_id':10},{'message_id':11}]
        state = {'steps':{}}
        with patch.object(p,'media_files',return_value={'image0':self.root/'a.jpg','image1':self.root/'b.jpg'}), patch.object(p,'telegram_identity',return_value={'id':-100123,'username':'example'}), patch.object(p,'telegram',return_value=messages) as telegram:
            self.assertEqual(p.post_telegram(spec,state,'tg-work'),'https://t.me/example/10')
        self.assertEqual(telegram.call_args.args[0],'sendMediaGroup')
        self.assertEqual(state['steps']['tg-work:publish']['value'],messages)
        self.assertEqual(self.store.checkpoint('article','tg-work:publish',lambda:self.fail('republished')),messages)

    def test_threads_delete_pins_id_and_checks_identity(self):
        with patch.object(p,'social_session',return_value=('token','1',p.THREADS)) as identity, patch.object(p,'quota'), patch.object(p,'request',return_value={'success':True}) as request:
            p.change_published('threads',self.results['threads'],self.spec,'delete',self.root)
            identity.assert_called_once_with('threads',deleting=True)
            self.assertEqual(request.call_args.args,('DELETE','https://graph.threads.net/v1.0/123'))

    def test_threads_replacement_warns_resumes_and_saves_new_identity(self):
        self.intake('поправь ошибку',20)
        corrected = {'copy':{**self.spec['copy'],'threads':'A corrected fact. What next?'},'blog':None}
        with patch.object(drafter,'ask',return_value=corrected), patch.object(app,'tell',return_value={'message_id':30}) as tell:
            app.fix(self.config,self.store,'fix-100')
        self.assertIn('будет удалён и опубликован заново, лайки и комментарии пропадут','\n'.join(c.args[1] for c in tell.call_args_list))
        saved = self.store.get('specs/fix-100')
        self.approve(saved)
        def post(spec,state,*args):
            state['steps']['threads:publish'] = {'status':'done','value':'999'}
            return 'https://www.threads.com/@romacv777/post/New'
        with patch.object(p,'social_session',return_value=('token','1',p.THREADS)), patch.object(p,'quota'), patch.object(app.media,'materialize'), patch.object(p,'change_published',return_value='удалено') as delete, patch.object(p,'post_social',side_effect=p.Error('rejected',403)) as publish, patch.object(app,'tell',return_value={'message_id':31}):
            app.apply_fix(self.config,self.store,None,'fix-100',self.root)
            source = self.store.get('specs/article')
            self.assertTrue(source['published']['threads']['deleted'])
            self.assertEqual(source['published']['threads']['url'],'')
            self.assertEqual(source['published']['threads']['awaiting_republish'],'fix-100')
            self.approve(saved,'retry',102)
            publish.side_effect = post
            app.apply_fix(self.config,self.store,None,'fix-100',self.root)
        delete.assert_called_once()
        result = self.store.get('specs/article')['results']['threads']
        self.assertEqual(result['id'],'999')
        self.assertTrue(result['url'].endswith('/New'))
        self.assertEqual(self.store.get('items/item-1')['published']['threads'],result)

    def test_delete_quota_exhausted_and_scope_missing(self):
        session = ('token','1',p.THREADS)
        with patch.object(p,'request',return_value={'data':[{'delete_quota_usage':100,'delete_config':{'quota_total':100}}]}) as request:
            with self.assertRaises(p.Error):
                p.quota(session,False,deleting=True)
            self.assertEqual(request.call_args.args[2],{'fields':'delete_quota_usage,delete_config'})
        self.store.put('tokens/threads',{'access_token':'token','user_id':'1','scopes':['threads_basic']})
        with patch.object(p,'personal_identity',return_value={'id':'1'}), patch.object(p,'request') as request:
            with self.assertRaisesRegex(p.Error,'threads_delete'):
                p.social_session('threads',deleting=True)
            request.assert_not_called()

    def test_stale_source_refuses_application(self):
        saved = self.prepare('delete')
        self.approve(saved,'del')
        self.store.change('specs/article',lambda value:value['published']['tg-work'].update(copy='Другая версия'))
        with patch.object(p,'change_published',return_value='удали вручную') as action, patch.object(app,'tell',return_value={'message_id':31}):
            app.apply_fix(self.config,self.store,None,'fix-100',self.root)
        self.assertNotIn('tg-work',[call.args[0] for call in action.call_args_list])

    def test_legacy_checkpoint_backfill_and_incomplete_album(self):
        legacy = copy.deepcopy(self.source)
        legacy['spec']['media'] = {'images':[{'path':'a.jpg','alt':'First'},{'path':'b.jpg','alt':'Second'}]}
        legacy['results']['tg-work'] = {'url':self.results['tg-work']['url'],'verified':True}
        self.store.put('specs/article',legacy)
        checkpoint = 'specs/article/checkpoints/' + hashlib.sha256(b'tg-work:publish').hexdigest()
        self.store.put(checkpoint,{'state':'done','value':{'message_id':10,'chat':{'id':-100123}}})
        self.prepare('delete')
        published = self.store.get('specs/fix-100')['before']['tg-work']
        self.assertEqual(published['message_ids'],[10])
        with patch.object(p,'telegram_identity',return_value={'id':-100123}), patch.object(p,'telegram') as telegram:
            result = p.change_published('tg-work',published,legacy['spec'],'delete',self.root)
        self.assertIn('вручную',result)
        telegram.assert_not_called()

    def test_delete_manual_instagram_finishes_item(self):
        saved = self.prepare('delete')
        self.approve(saved,'del')
        def change(target,*args):
            return 'удали вручную' if target.startswith('ig-') else 'удалено'
        with patch.object(p,'change_published',side_effect=change), patch.object(app,'tell',return_value={'message_id':31}) as tell:
            app.apply_fix(self.config,self.store,None,'fix-100',self.root)
        self.assertTrue(self.store.get('specs/article')['deleted'])
        self.assertTrue(self.store.get('items/item-1')['deleted'])
        self.assertIn('Instagram: удали вручную https://www.instagram.com/p/Abc/',tell.call_args.args[1])
        self.keyboard.assert_called_once()
        self.assertTrue(self.store.get('specs/fix-100')['keyboard_removed'])

    def test_preview_manual_missing_id_and_registered_chunks(self):
        self.source['results']['tg-work'].pop('message_ids')
        self.store.put('specs/article',self.source)
        self.intake('удали пост',20)
        with patch.object(app,'tell',side_effect=[{'message_id':29},{'message_id':30}]) as tell:
            app.fix(self.config,self.store,'fix-100')
        self.assertIn('tg-work: удали вручную: ID не сохранён',tell.call_args_list[0].args[1])
        self.assertEqual(self.store.get('published-messages/29'),{'slug':'article'})
        self.assertEqual(self.store.get('published-messages/30'),{'slug':'article'})

    def test_null_blog_keeps_published_blog_during_validation(self):
        blog = {'language':'en','title':'A concrete title','description':'A detailed description. ' * 6,'body':'## First\n\n' + 'Concrete useful words. ' * 100 + '\n\n## Second\n\n' + 'More detailed words. ' * 100}
        self.source['spec']['targets'].append('blog')
        self.source['spec']['blog'] = blog
        self.source['results']['blog'] = {'url':'https://www.resrom.com/blog/article','path':'blog/article.html','blog':blog}
        self.store.put('specs/article',self.source)
        self.intake('поправь ошибку',20)
        corrected = {'copy':self.spec['copy'],'blog':None}
        def ask(config,payload,images,validator):
            self.assertEqual(validator(corrected),[])
            return corrected
        with patch.object(drafter,'ask',side_effect=ask), patch.object(app,'tell',return_value={'message_id':30}):
            app.fix(self.config,self.store,'fix-100')
        self.assertEqual(self.store.get('specs/fix-100')['spec']['blog'],blog)

    def test_threads_preflight_uses_manifest_content_type(self):
        self.source['spec']['media'] = {'images':[{'path':'photo.jpg','alt':'Photo'}]}
        assets = {'image0.jpg':{'object':'photo','generation':1,'content_type':'image/jpeg'}}
        self.source['results']['threads']['assets'] = assets
        self.store.put('specs/article',self.source)
        self.intake('поправь ошибку',20)
        corrected = {'copy':{**self.spec['copy'],'threads':'A corrected fact. What next?'},'blog':None}
        with patch.object(drafter,'ask',return_value=corrected), patch.object(app,'tell',return_value={'message_id':30}):
            app.fix(self.config,self.store,'fix-100')
        saved = self.store.get('specs/fix-100')
        self.approve(saved)
        with patch.object(p,'social_session',return_value=('token','1',p.THREADS)), patch.object(p,'quota'), patch.object(app.media,'materialize'), patch.object(app.media,'signed',return_value='https://asset'), patch.object(p,'probe') as probe, patch.object(p,'change_published',return_value='удалено'), patch.object(p,'post_social',side_effect=p.Error('rejected',403)), patch.object(app,'tell',return_value={'message_id':31}) as tell:
            app.apply_fix(self.config,self.store,None,'fix-100',self.root)
        probe.assert_called_once_with('https://asset',self.root / 'image0.jpg','image/jpeg')
        report = tell.call_args.args[1]
        self.assertIn('ожидает повторной публикации',report)
        self.assertNotIn(self.results['threads']['url'],report)
        self.assertIsNone(self.store.published_match({'text':'удали ' + self.results['threads']['url']},None,True))
        self.intake('удали пост',20,103)
        with patch.object(app,'tell',return_value={'message_id':32}):
            app.fix(self.config,self.store,'fix-103')
        self.assertNotIn('threads',self.store.get('specs/fix-103')['spec']['targets'])

    def test_business_story_identity_requires_manage_stories(self):
        business = {'id':'business','is_enabled':True,'user':{'id':1},'rights':{'can_manage_stories':True}}
        def telegram(method, fields=None):
            if method == 'getMe':
                return {'id':2}
            self.assertEqual(method,'getBusinessConnection')
            self.assertEqual(fields,{'business_connection_id':'business'})
            return business
        with patch.object(p,'key',return_value={'business_connection_id':'business'}), patch.object(p,'telegram',side_effect=telegram):
            for operation in (None,'fix','delete'):
                self.assertEqual(p.telegram_identity('tg-story',operation),business)
            business['rights'] = {'can_post_stories':True,'can_edit_stories':True,'can_delete_stories':True}
            for operation in (None,'fix','delete'):
                with self.assertRaises(p.Error):
                    p.telegram_identity('tg-story',operation)

    def test_story_edit_reuses_manifest_and_delete_needs_no_media(self):
        published = {'id':77,'business_connection_id':'business','assets':{'image0.jpg':{'object':'story-photo'}}}
        spec = {**self.spec,'copy':{'tg-story':'Исправленная подпись'}}
        with patch.object(p,'telegram_identity',return_value={'id':'business'}), patch.object(app.media,'materialize') as materialize, patch.object(p,'telegram',return_value=True) as telegram:
            p.change_published('tg-story',published,spec,'fix',self.root,'bucket')
            materialize.assert_called_once_with('bucket',published['assets'],self.root)
            self.assertEqual(telegram.call_args.args[0],'editStory')
            self.assertEqual(telegram.call_args.args[1]['story_id'],77)
            self.assertEqual(telegram.call_args.args[1]['business_connection_id'],'business')
            self.assertNotIn('chat_id',telegram.call_args.args[1])
            self.assertEqual(telegram.call_args.args[1]['caption'],'Исправленная подпись')
            self.assertEqual(telegram.call_args.args[1]['content'],{'type':'photo','photo':'attach://asset'})
            materialize.reset_mock()
            p.change_published('tg-story',published,spec,'delete',self.root)
            materialize.assert_not_called()
            self.assertEqual(telegram.call_args.args[0],'deleteStory')
            self.assertEqual(telegram.call_args.args[1],{'business_connection_id':'business','story_id':77})

    def test_run_keeps_published_id_when_link_lookup_fails(self):
        spec = {**self.spec,'targets':['threads']}
        assets = {'threads':{}}
        digest = app.asset_hash(spec,assets)
        self.store.put('specs/article',{**self.source,'spec':spec,'state':'approved','hash':digest,'approved_hash':digest,'assets':assets,'results':{},'errors':{}})
        def post(spec,state,*args):
            state['steps']['threads:publish'] = {'status':'done','value':'123'}
            raise p.Error('Shortcode unavailable')
        with patch.object(app,'remove_keyboard'), patch.object(app.media,'materialize'), patch.object(p,'social_session',return_value=('token','1',p.THREADS)), patch.object(p,'post_social',side_effect=post), patch.object(app,'tell',return_value={'message_id':31}):
            app.run(self.config,self.store,None,'article',self.root)
        self.assertEqual(self.store.get('items/item-1')['published']['threads']['id'],'123')
        original_results = copy.deepcopy(self.store.get('specs/article')['results'])
        self.intake('удали пост',20)
        with patch.object(app,'tell',return_value={'message_id':32}):
            app.fix(self.config,self.store,'fix-100')
        self.assertEqual(self.store.get('specs/fix-100')['before']['threads']['id'],'123')
        self.assertEqual(self.store.get('specs/article')['results'],original_results)

    def test_run_saves_every_album_id_in_item(self):
        spec = {**self.spec,'targets':['tg-work'],'media':{'images':[{'path':'a.jpg','alt':'First'},{'path':'b.jpg','alt':'Second'}]}}
        assets = {'tg-work':{}}
        digest = app.asset_hash(spec,assets)
        self.store.put('specs/article',{**self.source,'spec':spec,'state':'approved','hash':digest,'approved_hash':digest,'assets':assets,'results':{},'errors':{}})
        def post(spec,state,target):
            state['steps'][target + ':publish'] = {'value':[{'message_id':10},{'message_id':11}]}
            return self.results['tg-work']['url']
        with patch.object(app,'remove_keyboard'), patch.object(app.media,'materialize'), patch.object(p,'post_telegram',side_effect=post), patch.object(p,'link_opens',return_value=True), patch.object(app,'tell',return_value={'message_id':31}):
            app.run(self.config,self.store,None,'article',self.root)
        self.assertEqual(self.store.get('items/item-1')['published']['tg-work']['message_ids'],[10,11])


class BlogChangeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(FakeFirestore())
        p.configure({},self.store,self.root)
        self.spec = {'slug':'article','at':'2020-01-01T00:00:00+00:00','media':{},'blog':{'title':'Original title','description':'Original description','body':'## First\n\nOriginal body.\n\n## Second\n\nOther paragraph.'}}
        (self.root/'project.html').write_text('<header>Header</header>')
        plan = build_files(self.spec,self.root)
        for name, content in plan['files'].items():
            path = self.root/name
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(content)

    def test_fix_preserves_figures_and_updates_index_feed(self):
        path = self.root/'blog/article.html'
        figure = '<figure><img src="/blog/article/0.jpg" alt="Existing image"></figure>'
        path.write_text(path.read_text().replace('<article>','<article>' + figure))
        spec = copy.deepcopy(self.spec)
        spec['blog']['title'] = 'Corrected title'
        spec['media'] = {'images':[{'path':'expired.jpg','alt':'Existing image'}]}
        plan = build_files(spec,self.root,'fix')
        self.assertIn(figure,plan['files']['blog/article.html'])
        self.assertIn('Corrected title',plan['files']['blog/index.html'])
        self.assertIn('Corrected title',plan['files']['blog/feed.xml'])
        self.assertEqual(plan['assets'],{})

    def test_delete_commit_removes_post_index_feed_sitemap_atomically(self):
        files = {name:(self.root/name).read_bytes() for name in ('project.html','blog/article.html','blog/index.html','sitemap.xml')}
        files['blog/article/0.jpg'] = b'image'
        files['blog/article/1.jpg'] = b'image'
        files['blog/other/0.jpg'] = b'other'
        client = object.__new__(GitHub)
        client.email = 'author@users.noreply.github.com'
        calls, blobs, nodes = [], {}, []
        def api(method,path,value=None):
            calls.append((method,path,value))
            if path == '/git/ref/heads/main':
                return {'object':{'sha':'parent'}}
            if path == '/git/commits/parent':
                return {'tree':{'sha':'base'}}
            if path == '/git/trees/base?recursive=1':
                return {'tree':[{'path':name,'type':'blob','sha':name} for name in files]}
            if method == 'GET' and path.startswith('/git/blobs/'):
                return {'content':base64.b64encode(files[path.removeprefix('/git/blobs/')]).decode()}
            if path == '/git/blobs':
                sha = 'blob-' + str(len(blobs))
                blobs[sha] = base64.b64decode(value['content']).decode()
                return {'sha':sha}
            if path == '/git/trees':
                nodes.extend(value['tree'])
                return {'sha':'tree'}
            if path == '/git/commits':
                self.assertEqual(value['parents'],['parent'])
                self.assertEqual(value['author']['name'],'Roman Resenchuk')
                self.assertEqual(value['author']['email'],client.email)
                return {'sha':'commit'}
            if method == 'PATCH':
                self.assertEqual(value,{'sha':'commit','force':False})
                return {}
            self.fail((method,path))
        with patch.object(client,'api',side_effect=api):
            self.assertEqual(client.change_post(self.spec,'blog/article.html','delete',self.root),'commit')
        self.assertEqual(sum(method == 'PATCH' for method,path,value in calls),1)
        self.assertIn({'path':'blog/article.html','mode':'100644','type':'blob','sha':None},nodes)
        self.assertIn({'path':'blog/article/0.jpg','mode':'100644','type':'blob','sha':None},nodes)
        self.assertIn({'path':'blog/article/1.jpg','mode':'100644','type':'blob','sha':None},nodes)
        self.assertNotIn('blog/other/0.jpg',[node['path'] for node in nodes])
        changed = {node['path']:blobs[node['sha']] for node in nodes if node['sha']}
        self.assertNotIn('Original title',changed['blog/index.html'])
        self.assertNotIn('Original title',changed['blog/feed.xml'])
        self.assertNotIn('https://www.resrom.com/blog/article',changed['sitemap.xml'])


if __name__ == '__main__':
    unittest.main()
