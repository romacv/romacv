from datetime import datetime, timezone, timedelta
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fake_firestore import FakeFirestore
from store import Store, StateError
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

    def test_finished_video_session_starts_new_batch(self):
        for status in ('previewed','done','failed','drafting'):
            with self.subTest(status=status):
                store = Store(FakeFirestore())
                store.put('sessions/inbox',{'video_batch':'old','upload_item':'old'})
                store.put('items/old',{'status':status})
                result = store.intake({'update_id':10,'message':{'message_id':10,'video':{'file_id':'v','file_size':100}}},self.config)
                self.assertEqual(result['item'],'item-10')
                self.assertEqual(store.get('sessions/inbox'),{'video_batch':'item-10'})

    def test_english_montage_caption_and_text_close_batch(self):
        store = Store(FakeFirestore())
        result = store.intake({'update_id':10,'message':{'message_id':10,'video':{'file_id':'v','file_size':100},'caption':'Montage reels about Jakarta Chinatown'}},self.config)
        self.assertEqual(store.get('items/' + result['item'])['montage'],True)
        self.assertEqual(store.get('items/' + result['item'])['status'],'queued')
        store.intake({'update_id':11,'message':{'message_id':11,'video':{'file_id':'v','file_size':100}}},self.config)
        result = store.intake({'update_id':12,'message':{'message_id':12,'text':'Montage','reply_to_message':{'message_id':11}}},self.config)
        item = store.get('items/item-11')
        self.assertEqual((result['item'],item['montage'],item['status'],len(item['messages'])),('item-11',True,'queued',2))
        self.assertEqual(store.get('outbox/draft-12')['payload']['step'],'montage')

    def test_montage_word_without_batch_explains(self):
        store = Store(FakeFirestore())
        result = store.intake({'update_id':10,'message':{'message_id':10,'text':'Montage'}},self.config)
        self.assertIn('Нет клипов',result)
        self.assertIsNone(store.get('items/item-10'))

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

    def test_render_recovery_uses_fresh_task_names(self):
        self.store.put('renders/render-1',{'state':'done','item':'item-1','revision':1})
        with patch.object(app.platforms,'telegram',return_value={'url':'test'}), patch.object(app.platforms,'request',return_value={}), patch.object(app,'enqueue') as enqueue, patch.object(app,'tell'), patch('urllib.request.urlopen',side_effect=OSError()):
            config = {**self.config,'webhook_url':'test','github_pat':'test'}
            app.maintenance(config,self.store,Mock())
            self.store.change('renders/render-1',lambda value:value.update(delivered=False))
            app.maintenance(config,self.store,Mock())
        self.assertNotEqual(enqueue.call_args_list[0].args[1],enqueue.call_args_list[1].args[1])

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
