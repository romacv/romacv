from datetime import datetime, timezone, timedelta
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fake_firestore import FakeFirestore
from store import Store, StateError, collect_keyboard
import main as app
import platforms


class ReviewFixTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(FakeFirestore())
        self.config = {'roman_chat_id':1,'webhook_secret':'test-secret'}

    def test_run_twice_same_scheduled_slug_removes_keyboard_once(self):
        spec = {'at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),'targets':[]}
        digest = app.asset_hash(spec,{})
        self.store.put('specs/example',{'state':'approved','hash':digest,'approved_hash':digest,'spec':spec,'assets':{},'preview_message':1})
        with patch.object(app,'remove_keyboard') as remove, patch.object(app,'enqueue') as enqueue:
            app.run(self.config,self.store,None,'example',Path('/tmp'))
            app.run(self.config,self.store,None,'example',Path('/tmp'))
        remove.assert_called_once()
        self.assertEqual(enqueue.call_count,2)
        self.assertEqual(self.store.get('specs/example')['state'],'approved')

    def test_outbox_filters_and_deletes_only_after_enqueue(self):
        self.store.put('outbox/old',{'sent':True})
        self.store.put('outbox/new',{'sent':False,'payload':{'step':'draft'}})
        enqueue = Mock(side_effect=RuntimeError('queue unavailable'))
        with self.assertRaises(RuntimeError):
            self.store.flush(enqueue)
        self.assertIsNotNone(self.store.get('outbox/new'))
        enqueue.side_effect = None
        self.store.flush(enqueue)
        enqueue.reset_mock()
        self.store.flush(enqueue)
        enqueue.assert_not_called()
        self.assertIsNone(self.store.get('outbox/new'))
        self.assertIsNotNone(self.store.get('outbox/old'))

    def test_definite_rejection_can_retry_but_network_stays_pending(self):
        action = Mock(side_effect=platforms.Error('rejected',status_code=400))
        with self.assertRaises(platforms.Error):
            self.store.checkpoint('example','threads:publish',action)
        action.side_effect = None
        action.return_value = '123'
        self.assertEqual(self.store.checkpoint('example','threads:publish',action),'123')
        action.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            self.store.checkpoint('example','ig-post:publish',action)
        with self.assertRaises(StateError):
            self.store.checkpoint('example','ig-post:publish',action)

    def test_edit_failure_preserves_state_and_has_no_retry_button(self):
        self.store.put('specs/example',{'state':'needs-edit','hash':'abcdefgh','preview_message':1})
        request = SimpleNamespace(data={'step':'edit-prompt','slug':'example'})
        with patch.object(app,'runtime',return_value=(self.config,self.store,None)), patch.object(app,'remove_keyboard',side_effect=platforms.Error('failure')), patch.object(app,'tell') as tell:
            app.handle_worker(request)
        self.assertEqual(self.store.get('specs/example')['state'],'needs-edit')
        self.assertNotIn('reply_markup',tell.call_args.kwargs)

    def test_probe_uses_single_get_and_never_head(self):
        body = b'media bytes'
        response = Mock(status=200,headers={'Content-Length':str(len(body)),'Content-Type':'image/jpeg'})
        response.read.side_effect = io.BytesIO(body).read
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'media.jpg'
            path.write_bytes(body)
            with patch.object(platforms.urllib.request,'urlopen',return_value=response) as urlopen:
                platforms.probe('https://example.com/media?signature=get',path)
        urlopen.assert_called_once_with('https://example.com/media?signature=get',timeout=60)

    def test_probe_rejects_invalid_get_response(self):
        body = b'media bytes'
        for status, length, content in ((403,len(body),body),(200,len(body)+1,body),(200,len(body),b'other bytes')):
            with self.subTest(status=status,length=length,content=content):
                response = Mock(status=status,headers={'Content-Length':str(length),'Content-Type':'image/jpeg'})
                response.read.side_effect = io.BytesIO(content).read
                response.__enter__ = Mock(return_value=response)
                response.__exit__ = Mock(return_value=False)
                with tempfile.TemporaryDirectory() as temporary:
                    path = Path(temporary)/'media.jpg'
                    path.write_bytes(body)
                    with patch.object(platforms.urllib.request,'urlopen',return_value=response):
                        with self.assertRaises(platforms.Error):
                            platforms.probe('https://example.com/media',path)

    def video(self, store, identifier, caption=None):
        message = {'message_id':identifier,'video':{'file_id':'v','file_size':100}}
        if caption:
            message['caption'] = caption
        return store.intake({'update_id':identifier,'message':message},self.config)

    def post(self, store, identifier, **extra):
        message = {'message_id':identifier,'entities' if 'text' in extra else 'caption_entities':[{'type':'bot_command','offset':0,'length':5}],**extra}
        return store.intake({'update_id':identifier,'message':message},self.config)

    def tap(self, store, verb, item, token, identifier):
        return store.intake({'update_id':identifier,'callback_query':{'id':str(identifier),'data':verb + ':' + item + ':' + token}},self.config)

    def test_post_opens_item_and_following_messages_join_it(self):
        store = Store(FakeFirestore())
        result = self.post(store,10,text='/post')
        self.assertEqual(result['text'],'Новый пост. Присылай текст, фото, видео или голос, потом нажми кнопку.')
        token = store.get('items/item-10')['batch_token']
        self.assertEqual([(b['text'],b['callback_data']) for b in result['reply_markup']['inline_keyboard'][0]],[('Готово','done:item-10:' + token),('Отменить','cncl:item-10:' + token)])
        self.assertEqual(store.get('sessions/inbox')['collecting'],'item-10')
        text = store.intake({'update_id':11,'message':{'message_id':11,'text':'Монтаж про Джакарту'}},self.config)
        self.assertEqual((text['item'],text['text']),('item-10','Добавил в пост «Монтаж про Джакарту»: текст.'))
        photo = store.intake({'update_id':12,'message':{'message_id':12,'photo':[{'file_id':'p','file_size':10}]}},self.config)
        self.assertEqual(photo['text'],'Добавил в пост «Монтаж про Джакарту»: 1 фото, текст.')
        clip = self.video(store,13)
        self.assertEqual(clip['text'],'Добавил в пост «Монтаж про Джакарту»: 1 фото, 1 клип, текст.')
        self.assertEqual(clip['reply_markup'],collect_keyboard('item-10',token))
        item = store.get('items/item-10')
        self.assertEqual((item['status'],len(item['messages'])),('collecting',3))
        self.assertEqual([path for path in store.db.values if path.startswith('outbox/')],[])

    def test_post_with_attached_media_joins_the_item(self):
        store = Store(FakeFirestore())
        result = self.post(store,10,caption='/post Рынок',photo=[{'file_id':'p','file_size':10}])
        self.assertEqual(result['item'],'item-10')
        self.assertEqual(len(store.get('items/item-10')['messages']),1)
        self.assertEqual(result['text'],'Добавил в пост «Рынок»: 1 фото, текст.')

    def test_done_queues_draft_or_montage_by_video(self):
        for clips,step in ((False,'draft'),(True,'montage')):
            with self.subTest(step=step):
                store = Store(FakeFirestore())
                self.post(store,10,text='/post')
                if clips:
                    self.video(store,11,'Montage reels about Jakarta Chinatown')
                else:
                    store.intake({'update_id':11,'message':{'message_id':11,'text':'Привет'}},self.config)
                token = store.get('items/item-10')['batch_token']
                answer = self.tap(store,'done','item-10',token,12)
                item = store.get('items/item-10')
                self.assertEqual((item['status'],item['montage']),('queued',clips))
                self.assertEqual(store.get('outbox/draft-12')['payload'],{'item':'item-10','step':step,'revision':item['revision']})
                self.assertNotIn('collecting',store.get('sessions/inbox'))
                self.assertIsNone(self.tap(store,'done','item-10',token,13))
                if clips:
                    self.assertEqual(answer,'Монтирую 1 клип «Montage reels about Jakarta Chinatown». Превью пришлю сюда.')

    def test_done_on_empty_item_asks_for_material(self):
        store = Store(FakeFirestore())
        self.post(store,10,text='/post')
        token = store.get('items/item-10')['batch_token']
        self.assertEqual(self.tap(store,'done','item-10',token,11),'Пост пустой. Пришли текст, фото, видео или голос.')
        self.assertEqual(store.get('items/item-10')['status'],'collecting')

    def test_cancel_drops_item_and_stale_tokens_are_rejected(self):
        store = Store(FakeFirestore())
        self.post(store,10,text='/post')
        token = store.get('items/item-10')['batch_token']
        self.assertIsNone(self.tap(store,'done','item-10','wrongtok',11))
        self.assertEqual(store.get('items/item-10')['status'],'collecting')
        self.assertEqual(self.tap(store,'cncl','item-10',token,12),'Пост отменён.')
        self.assertEqual(store.get('items/item-10')['status'],'rejected')
        self.assertNotIn('collecting',store.get('sessions/inbox'))
        self.assertIsNone(self.tap(store,'done','item-10',token,13))
        self.assertIsNone(store.get('outbox/draft-13'))
        self.assertEqual(store.intake({'update_id':14,'message':{'message_id':14,'text':'Привет'}},self.config),'Чтобы начать пост, отправь /post.')

    def test_no_open_item_and_no_draft_gives_hint_and_stores_nothing(self):
        store = Store(FakeFirestore())
        for update in ({'update_id':10,'message':{'message_id':10,'text':'Аи слоп'}},{'update_id':11,'message':{'message_id':11,'photo':[{'file_id':'p','file_size':10}]}},{'update_id':12,'message':{'message_id':12,'video':{'file_id':'v','file_size':100}}}):
            result = store.intake(update,self.config)
            self.assertEqual(result,'Чтобы начать пост, отправь /post.')
        self.assertEqual([path for path in store.db.values if path.startswith('items/') or path.startswith('outbox/')],[])

    def test_rejected_item_media_is_removed_by_maintenance(self):
        store = Store(FakeFirestore())
        store.put('items/item-10',{'status':'rejected'})
        blob = Mock(generation=3)
        bucket = Mock(list_blobs=Mock(return_value=[blob]))
        with patch.object(app.platforms,'telegram',return_value={'url':'test'}), patch.object(app.platforms,'token',return_value=None), patch.object(app,'enqueue'):
            with patch('urllib.request.urlopen',side_effect=OSError), patch.object(app,'tell'):
                app.maintenance({'webhook_url':'test','github_pat':'test'},store,bucket)
        bucket.list_blobs.assert_called_with(prefix='inbox/item-10/')
        blob.delete.assert_called_once_with(if_generation_match=3)

    def test_invalid_draft_logs_errors_and_tells_reason(self):
        import json
        import drafter
        response = SimpleNamespace(content=[SimpleNamespace(type='text',text='{}')])
        client = SimpleNamespace(messages=SimpleNamespace(create=Mock(return_value=response)))
        with patch.dict(sys.modules,{'anthropic':SimpleNamespace(Anthropic=Mock(return_value=client),APIStatusError=Exception)}):
            with self.assertLogs(level='ERROR') as logs:
                with self.assertRaises(platforms.Error) as raised:
                    drafter.ask({'anthropic_key':'k'},{},[],lambda value:['tg-channel: copy is required','threads: banned phrase','third'])
        self.assertEqual(str(raised.exception),'Черновик не готов: tg-channel: copy is required; threads: banned phrase')
        self.assertEqual(json.loads(logs.records[-1].getMessage()),{'code':'draft_invalid','errors':['tg-channel: copy is required','threads: banned phrase','third']})

    def test_repeated_upload_receipts_have_unique_task_names(self):
        self.store.put('items/item-1',{'revision':1,'waiting_upload':True})
        expires = int(app.time.time())+900
        value = {'action':'uploaded','item':'item-1','revision':1,'expires':expires,'token':app.upload_mac(self.config,'item-1',1,expires)}
        request = Mock(method='POST',headers={'Origin':'https://www.resrom.com'})
        request.get_json.return_value = value
        with patch.object(app,'runtime',return_value=(self.config,self.store,None)), patch.object(app,'enqueue') as enqueue:
            app.webhook(request)
            app.webhook(request)
        self.assertNotEqual(enqueue.call_args_list[0].args[1],enqueue.call_args_list[1].args[1])

    def test_render_recovery_uses_one_task_name_and_stays_undelivered(self):
        self.store.put('renders/render-1',{'state':'done','item':'item-1','revision':1})
        with patch.object(app.platforms,'telegram',return_value={'url':'test'}), patch.object(app.platforms,'request',return_value={}), patch.object(app,'enqueue') as enqueue, patch.object(app,'tell'), patch('urllib.request.urlopen',side_effect=OSError()):
            config = {**self.config,'webhook_url':'test','github_pat':'test'}
            app.maintenance(config,self.store,Mock())
            app.maintenance(config,self.store,Mock())
        self.assertEqual(enqueue.call_args_list[0].args[1],'render-recovery-render-1')
        self.assertEqual(enqueue.call_args_list[0].args[1],enqueue.call_args_list[1].args[1])
        self.assertFalse(self.store.get('renders/render-1').get('delivered',False))

    def test_expired_material_is_terminal_without_retry(self):
        for during_download in (False,True):
            with self.subTest(during_download=during_download):
                spec = {'at':datetime.now(timezone.utc).isoformat(),'targets':['threads']}
                assets = {'threads':{'image.jpg':{'object':'media/image.jpg','generation':'1'}}}
                digest = app.asset_hash(spec,assets)
                self.store.put('specs/example',{'state':'approved','hash':digest,'approved_hash':digest,'spec':spec,'assets':assets,'results':{},'errors':{}})
                bucket = Mock()
                bucket.blob.return_value.exists.return_value = during_download
                with patch.object(app,'runtime',return_value=(self.config,self.store,bucket)), patch.object(app.media,'materialize',side_effect=platforms.ExpiredMaterial('Материал истёк. Пришли файлы заново.')), patch.object(app,'tell') as tell, patch.object(app.platforms,'post_social') as publish:
                    app.handle_worker(SimpleNamespace(data={'step':'run','slug':'example'}))
                self.assertEqual(self.store.get('specs/example')['state'],'failed')
                self.assertNotIn('reply_markup',tell.call_args.kwargs)
                self.assertIn('Материал истёк',tell.call_args.args[1])
                publish.assert_not_called()

    def test_probe_requires_manifest_content_type(self):
        body = b'media bytes'
        for content_type in ('video/mp4',None,'image/jpeg; charset=utf-8','image/jpeg'):
            with self.subTest(content_type=content_type):
                response = Mock(status=200,headers={'Content-Length':str(len(body)),'Content-Type':content_type})
                response.read.side_effect = io.BytesIO(body).read
                response.__enter__ = Mock(return_value=response)
                response.__exit__ = Mock(return_value=False)
                with tempfile.TemporaryDirectory() as temporary:
                    path = Path(temporary)/'media.bin'
                    path.write_bytes(body)
                    with patch.object(platforms.urllib.request,'urlopen',return_value=response):
                        if content_type == 'image/jpeg':
                            platforms.probe('https://example.com/media',path,'image/jpeg')
                        else:
                            with self.assertRaises(platforms.Error):
                                platforms.probe('https://example.com/media',path,'image/jpeg')

    def test_montage_plan_message_names_the_draft(self):
        item = {'status':'queued','revision':1,'slug':'example','messages':[{'video':{},'caption':'Jakarta Chinatown'}],'prepared':{'durations':{'clip':5},'sheets':[],'sources':{'clip':{}}},'transcripts':{'clip':''}}
        inputs = [{'kind':'video','path':Path('clip.mp4'),'name':'clip.mp4','manifest':{}}]
        for edits,expected in (([],'Монтирую «Jakarta Chinatown»: план готов, рендерю черновик в низком качестве. Превью пришлю сюда.'),(['короче'],'Перемонтирую «Jakarta Chinatown» с правкой: короче. Рендерю черновик в низком качестве. Превью пришлю сюда.')):
            with self.subTest(edits=edits):
                self.store.put('items/item-1',{**item,'edits':edits,'edl':{'kind':'video'} if edits else None})
                config = {**self.config,'worker_url':'test','runtime_service_account':'test'}
                with patch.object(app,'runtime',return_value=(config,self.store,Mock())), patch.object(app.media,'intake',return_value=inputs), patch.object(app.drafter,'ask',return_value={'kind':'video'}), patch.object(app.firebase_admin,'get_app'), patch.dict(sys.modules,{'google.cloud.run_v2':Mock()}), patch.object(app,'tell') as tell:
                    app.plan_montage(config,self.store,Mock(name='bucket'),{'item':'item-1','revision':1},Path('/tmp'))
                tell.assert_called_once_with(ANY,expected)

    def test_finished_render_callbacks_mark_delivery(self):
        for step in ('prepared','rendered'):
            for state in ('done','failed'):
                for stale in (False,True):
                    with self.subTest(step=step,state=state,stale=stale):
                        payload = {'step':step,'item':'item-1','revision':1,'render':'render-1'}
                        self.store.put('items/item-1',{'revision':2 if stale else 1,'render':'render-1','status':'drafting','slug':'example'})
                        edl = {'kind':'video','targets':{'threads':{}},'copy':{}}
                        self.store.put('renders/render-1',{'state':state,'outputs':{'videos':{},'cover':{}},'edl':edl})
                        with patch.object(app,'runtime',return_value=(self.config,self.store,Mock())), patch.object(app,'preview') as preview, patch.object(app,'tell'), patch.object(app,'enqueue'):
                            app.handle_worker(SimpleNamespace(data=payload))
                        self.assertTrue(self.store.get('renders/render-1')['delivered'])
                        config = {**self.config,'webhook_url':'test','github_pat':'test'}
                        with patch.object(app.platforms,'telegram',return_value={'url':'test'}), patch.object(app.platforms,'request',return_value={}), patch.object(app,'enqueue') as enqueue, patch.object(app,'tell'), patch('urllib.request.urlopen',side_effect=OSError()):
                            app.maintenance(config,self.store,Mock())
                        enqueue.assert_not_called()
                        if step == 'rendered':
                            self.assertEqual(preview.call_count,int(state == 'done' and not stale))

    def test_failed_preview_keeps_render_undelivered(self):
        self.store.put('items/item-1',{'revision':1,'render':'render-1','status':'drafting','slug':'example'})
        self.store.put('renders/render-1',{'state':'done','edl':{'kind':'video','targets':{},'copy':{}},'outputs':{'videos':{},'cover':{}}})
        with patch.object(app,'preview',side_effect=RuntimeError('unavailable')):
            with self.assertRaises(RuntimeError):
                app.rendered(self.config,self.store,Mock(),{'item':'item-1','revision':1,'render':'render-1'},Path('/tmp'))
        self.assertFalse(self.store.get('renders/render-1').get('delivered',False))
