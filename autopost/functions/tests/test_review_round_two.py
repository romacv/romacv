from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fake_firestore import FakeFirestore
from store import Store
import main as app
import platforms


class RoundTwoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(FakeFirestore())
        self.config = {'roman_chat_id':1}
        self.spec = {'slug':'album','kind':'work','at':'2020-01-01T00:00:00+00:00','targets':['tg-channel'],'copy':{'tg-channel':'Text'},'approval':{}}
        self.assets = {'tg-channel':{}}
        self.store.put('items/item-1',{'status':'drafting','revision':1,'messages':[{'message_id':1,'text':'Тема'}],'slug':'album'})

    def message(self, identifier, **fields):
        return self.store.intake({'update_id':identifier,'message':{'message_id':identifier,**fields}},self.config)

    def tap(self, verb, item, token, identifier):
        return self.store.intake({'update_id':identifier,'callback_query':{'id':str(identifier),'data':verb + ':' + item + ':' + token}},self.config)

    def preview(self, during=None, revision=1):
        sent = []
        def telegram(method,fields,files=None):
            sent.append((method,fields))
            if method == 'sendMessage' and during:
                during()
            return {'message_id':100 + len(sent)}
        with patch.object(app.platforms,'telegram',side_effect=telegram), patch.object(app.platforms,'telegram_identity'), patch.object(app.media,'materialize'):
            app.preview(self.config,self.store,None,'item-1',self.spec,self.assets,self.root,revision=revision)
        return sent

    def queue_edit(self):
        self.store.atomic(lambda tx:self.store.queue_edit(tx,'album',self.store.get('specs/album',tx),'правка',None,'e1',{}))

    def test_edit_queued_during_preview_wins_and_the_stale_keyboard_is_removed(self):
        sent = self.preview(self.queue_edit)
        saved = self.store.get('specs/album')
        self.assertEqual((saved['state'],saved['hash']),('drafting',''))
        item = self.store.get('items/item-1')
        self.assertEqual((item['status'],item['revision']),('queued',2))
        self.assertIsNotNone(self.store.get('outbox/edit-e1'))
        self.assertEqual(sent[-1][0],'editMessageReplyMarkup')
        self.assertEqual(sent[-1][1]['reply_markup'],{'inline_keyboard':[]})
        self.assertNotIn('current',self.store.get('sessions/inbox') or {})

    def test_matching_revision_commits_the_preview_and_clears_edit_photos(self):
        self.store.change('items/item-1',lambda value:value.update(edit_photos=[{'message_id':5}]))
        self.preview()
        self.assertEqual(self.store.get('specs/album')['state'],'previewed')
        item = self.store.get('items/item-1')
        self.assertEqual(item['status'],'previewed')
        self.assertNotIn('edit_photos',item)
        self.assertEqual(self.store.get('sessions/inbox')['current'],'album')

    def final_spec(self, **extra):
        self.store.put('specs/example',{'state':'rendering','hash':'abcdefgh1','final_render':'f1','started_at':time.time(),'item':'item-1','spec':{'copy':{}},**extra})

    def test_failed_final_render_sends_one_message_for_duplicate_deliveries(self):
        self.final_spec()
        payload = {'render':'f1','revision':1}
        with patch.object(app,'tell') as tell:
            for _ in range(2):
                app.finish_final(self.config,self.store,payload,{'slug':'example','revision':1},{'state':'failed'})
        tell.assert_called_once()
        self.assertEqual(self.store.get('specs/example')['state'],'partial')

    def test_final_render_handler_failure_sets_partial_with_retry(self):
        self.final_spec()
        self.store.put('renders/f1',{'mode':'final','state':'done'})
        payload = {'render':'f1','item':'item-1','revision':1}
        self.store.change('items/item-1',lambda value:value.update(slug='example'))
        with patch.object(app,'finish_final',side_effect=RuntimeError('boom')), patch.object(app,'tell') as tell:
            app.rendered(self.config,self.store,None,payload,self.root)
        self.assertEqual(self.store.get('specs/example')['state'],'partial')
        self.assertIn('reply_markup',tell.call_args.kwargs)

    def test_recover_running_moves_stuck_final_render_to_partial(self):
        self.final_spec(started_at=time.time() - app.FINAL_DEADLINE - 5)
        self.store.put('renders/f1',{'mode':'final','state':'queued'})
        with patch.object(app,'tell') as tell:
            app.recover_running(self.config,self.store)
        self.assertEqual(self.store.get('specs/example')['state'],'partial')
        self.assertIn('retry:example:abcdefgh',str(tell.call_args.kwargs['reply_markup']))

    def test_recover_running_leaves_fresh_or_finished_final_renders(self):
        self.final_spec()
        self.store.put('renders/f1',{'mode':'final','state':'queued'})
        with patch.object(app,'tell') as tell:
            app.recover_running(self.config,self.store)
            self.store.change('specs/example',lambda value:value.update(started_at=0))
            self.store.put('renders/f1',{'mode':'final','state':'done'})
            app.recover_running(self.config,self.store)
        tell.assert_not_called()
        self.assertEqual(self.store.get('specs/example')['state'],'rendering')

    def test_start_final_keeps_the_render_when_the_message_fails(self):
        self.store.put('items/item-1',{'status':'previewed','revision':1,'messages':[],'render':'draft-render'})
        self.store.put('renders/draft-render',{key:'x' for key in ('item','revision','sources','bucket','project','worker_url','service_account')})
        saved = {'item':'item-1','spec':{'slug':'example'},'basis':{'edl':{}}}
        self.store.put('specs/example',{'state':'running'})
        with patch.object(app,'start_job') as start, patch.object(app,'tell',side_effect=platforms.Error('down')):
            app.start_final(self.config,self.store,None,saved)
        start.assert_called_once()
        self.assertEqual(self.store.get('specs/example')['state'],'rendering')

    def edit_voices(self, count):
        voices = [{'message_id':40 + index,'voice':{'file_id':'a','file_size':10},'note':'[голосовое сообщение ' + str(40 + index) + ']'} for index in range(count)]
        audio = {str(voice['message_id']) + '.ogg':{'object':'x','audio':True,'duration':3} for voice in voices}
        self.store.put('items/item-1',{'status':'drafting','revision':2,'messages':[],'edits':[voice['note'] for voice in voices],'edit_voices':voices,'edit_prepared':{'audio':audio}})
        attachments = [{'name':str(voice['message_id']) + '.ogg','kind':'voice','path':self.root/(str(voice['message_id']) + '.ogg'),'manifest':{}} for voice in voices]
        return attachments

    def test_edit_voice_transcripts_persist_so_a_retry_never_pays_twice(self):
        attachments = self.edit_voices(2)
        payload = {'item':'item-1','revision':2}
        with patch.object(app.media,'intake',return_value=attachments), patch.object(app.media,'transcribe',side_effect=[{'text':'Первое'},RuntimeError('down')]):
            with self.assertRaises(RuntimeError):
                app.edit_media(self.config,self.store,None,payload,self.store.get('items/item-1'),self.root)
        self.assertEqual(self.store.get('items/item-1')['edit_transcripts'],{'40.ogg':'Первое'})
        with patch.object(app.media,'intake',return_value=attachments), patch.object(app.media,'transcribe',return_value={'text':'Второе'}) as transcribe:
            edits,shots = app.edit_media(self.config,self.store,None,payload,self.store.get('items/item-1'),self.root)
        self.assertEqual(transcribe.call_count,1)
        self.assertEqual(edits,['Первое','Второе'])
        saved = self.store.get('items/item-1')
        self.assertEqual((saved['edits'],saved['edit_voices'],saved['edit_transcripts']),(['Первое','Второе'],[],{}))

    def current_draft(self):
        self.store.put('items/item-1',{'status':'previewed','revision':1,'messages':[{'message_id':1,'text':'Тема'}],'slug':'example'})
        self.store.put('specs/example',{'state':'previewed','item':'item-1','spec':{'copy':{}},'preview_message':77})
        self.store.put('sessions/inbox',{'current':'example'})

    def open_post(self, identifier, **fields):
        return self.message(identifier,text='/post',entities=[{'type':'bot_command','offset':0,'length':5}],**fields)

    def test_done_moves_feedback_off_the_old_preview(self):
        self.current_draft()
        self.open_post(10)
        self.message(11,text='Новый пост')
        self.tap('done','item-10',self.store.get('items/item-10')['batch_token'],12)
        self.assertEqual(self.store.get('sessions/inbox')['current'],'item-10')
        reply = self.message(13,text='правка')
        self.assertEqual(reply,'Черновик ещё готовится. Правку пришли, когда придёт превью.')
        self.assertNotIn('edits',self.store.get('items/item-1'))
        self.assertEqual(self.store.get('specs/example')['state'],'previewed')

    def test_done_with_photos_and_video_keeps_the_post_open(self):
        self.open_post(10)
        self.message(11,photo=[{'file_id':'p','file_size':1}])
        self.message(12,video={'file_id':'v','file_size':1})
        token = self.store.get('items/item-10')['batch_token']
        answer = self.tap('done','item-10',token,13)
        self.assertEqual(answer,'В монтаж идут видео, текст и голос. Фото пришли отдельным /post.')
        self.assertEqual(self.store.get('items/item-10')['status'],'collecting')
        self.assertEqual(self.store.get('sessions/inbox')['collecting'],'item-10')
        self.assertIsNone(self.store.get('outbox/draft-13'))

    def test_voice_with_caption_is_a_voice_edit_plus_the_caption(self):
        self.current_draft()
        self.message(20,voice={'file_id':'a','file_size':1},caption='Короче')
        item = self.store.get('items/item-1')
        self.assertEqual(item['edits'],['Короче [голосовое сообщение 20]'])
        self.assertEqual([v['message_id'] for v in item['edit_voices']],[20])
        attachments = [{'name':'20.ogg','kind':'voice','path':self.root/'20.ogg','manifest':{}}]
        item['edit_prepared'] = {'audio':{'20.ogg':{'object':'x','audio':True,'duration':3}}}
        self.store.put('items/item-1',item)
        with patch.object(app.media,'intake',return_value=attachments), patch.object(app.media,'transcribe',return_value={'text':'ещё ярче'}):
            edits,shots = app.edit_media(self.config,self.store,None,{'item':'item-1','revision':item['revision']},item,self.root)
        self.assertEqual(edits,['Короче ещё ярче'])

    def test_video_with_caption_sent_to_a_draft_is_a_hint_not_an_edit(self):
        self.current_draft()
        reply = self.message(21,video={'file_id':'v','file_size':1},caption='смотри')
        self.assertEqual(reply,'К черновику принимаю текст, фото и голос. Новый пост начинается с /post.')
        item = self.store.get('items/item-1')
        self.assertEqual((item.get('edits'),item['revision']),(None,1))

    def test_montage_hears_voices_and_passes_them_as_notes(self):
        video = {'name':'12.mp4','kind':'video','path':self.root/'12.mp4','manifest':{}}
        voice = {'name':'13.ogg','kind':'voice','path':self.root/'13.ogg','manifest':{}}
        self.store.put('items/item-1',{'status':'queued','revision':3,'messages':[],'slug':'album','montage':True,'prepared':{'durations':{'12':5},'sources':{'12':{'object':'v'}},'sheets':[],'audio':{}}})
        spoken = {'12.mp4':{'text':'клип','words':[]},'13.ogg':{'text':' голос ','words':[]}}
        asked = {}
        def ask(config,payload,images,validator,montage=False):
            asked.update(payload)
            return {'kind':'work','targets':['ig-reel'],'copy':{}}
        config = {**self.config,'worker_url':'u','runtime_service_account':'s'}
        with patch.object(app.media,'intake',return_value=[video,voice]), patch.object(app.media,'transcribe',side_effect=lambda bucket,store,path,source,config:spoken[path.name]), patch.object(app.drafter,'ask',side_effect=ask), patch.object(app,'start_job'), patch.object(app,'tell'), patch.object(app.firebase_admin,'get_app',return_value=SimpleNamespace(project_id='p')):
            app.plan_montage(config,self.store,SimpleNamespace(name='b'),{'item':'item-1','revision':3},self.root)
        self.assertEqual(asked['notes'],['голос'])
        self.assertEqual(asked['transcripts'],{'12':spoken['12.mp4']})
        self.assertEqual(self.store.get('items/item-1')['transcripts'],{'12':spoken['12.mp4'],'13.ogg':spoken['13.ogg']})

    def second_draft(self):
        self.current_draft()
        self.store.put('items/item-2',{'status':'previewed','revision':1,'messages':[{'message_id':2,'text':'Другая'}],'slug':'other'})
        self.store.put('specs/other',{'state':'previewed','item':'item-2','spec':{'copy':{},'targets':[]},'preview_message':88})
        self.store.put('published-messages/88',{'slug':'other'})

    def test_reply_with_a_voice_to_another_preview_edits_that_draft(self):
        self.second_draft()
        self.message(30,voice={'file_id':'a','file_size':1},reply_to_message={'message_id':88,'text':'Черновик'})
        self.assertEqual(self.store.get('items/item-2')['edits'],['[голосовое сообщение 30]'])
        self.assertNotIn('edits',self.store.get('items/item-1'))

    def test_post_clears_a_pending_edit_prompt(self):
        self.current_draft()
        self.store.put('sessions/inbox',{'current':'example','awaiting':{'kind':'edit','slug':'example'}})
        self.open_post(31)
        self.assertNotIn('awaiting',self.store.get('sessions/inbox'))
        self.message(32,text='Новый пост')
        self.assertNotIn('edits',self.store.get('items/item-1'))

    def test_repeated_edit_taps_succeed_with_one_prompt_job(self):
        self.current_draft()
        self.store.change('specs/example',lambda value:value.update(hash='abcdefgh1'))
        first = self.tap('edit','example','abcdefgh',40)
        second = self.tap('edit','example','abcdefgh',41)
        self.assertEqual(first,'Напиши, что поменять в черновике «Тема».')
        self.assertEqual(second,'Жду правку: напиши, что поменять.')
        self.assertEqual(self.store.get('specs/example')['state'],'needs-edit')
        self.assertEqual(len([key for key in self.store.db.values if key.startswith('outbox/callback-')]),1)

    def test_start_final_records_the_render_before_the_spec_enters_rendering(self):
        self.store.put('items/item-1',{'status':'previewed','revision':1,'messages':[],'render':'draft-render'})
        self.store.put('renders/draft-render',{key:'x' for key in ('item','revision','sources','bucket','project','worker_url','service_account')})
        self.store.put('specs/example',{'state':'running'})
        saved = {'item':'item-1','spec':{'slug':'example'},'basis':{'edl':{}}}
        def check(project,identifier):
            self.assertIsNotNone(self.store.get('renders/' + identifier))
            self.assertEqual(self.store.get('specs/example')['final_render'],identifier)
        with patch.object(app,'start_job',side_effect=check), patch.object(app,'tell'):
            app.start_final(self.config,self.store,None,saved)

    def test_montage_ack_truncates_a_long_edit(self):
        long = 'слово ' * 300
        self.store.put('items/item-1',{'status':'queued','revision':3,'messages':[],'slug':'album','montage':True,'edits':[long],'edl':{'x':1},'prepared':{'durations':{'12':5},'sources':{'12':{'object':'v'}},'sheets':[],'audio':{}}})
        sent = []
        config = {**self.config,'worker_url':'u','runtime_service_account':'s'}
        with patch.object(app.media,'intake',return_value=[{'name':'12.mp4','kind':'video','path':self.root/'12.mp4','manifest':{}}]), patch.object(app.media,'transcribe',return_value={'text':'клип','words':[]}), patch.object(app.drafter,'ask',return_value={'kind':'work','targets':['ig-reel'],'copy':{}}), patch.object(app,'start_job'), patch.object(app,'tell',side_effect=lambda c,text,**k:sent.append(text)), patch.object(app.firebase_admin,'get_app',return_value=SimpleNamespace(project_id='p')):
            app.plan_montage(config,self.store,SimpleNamespace(name='b'),{'item':'item-1','revision':3},self.root)
        self.assertTrue(all(len(text) < 600 for text in sent))
        self.assertTrue(any('…' in text for text in sent))

    def test_story_overlay_composes_a_1080x1920_asset_distinct_from_the_post_photo(self):
        from PIL import Image
        Image.new('RGB',(800,600),(40,90,160)).save(self.root/'p.jpg')
        spec = {'slug':'album','kind':'work','at':'2020-01-01T00:00:00+00:00','targets':['tg-channel','tg-story'],'copy':{'tg-channel':'T'},'media':{'images':[{'path':'p.jpg'}]},'overlay':{'tg-story':{'text':'Бали','place':'Бали','anchor':[0.5,0.8],'align':'center'}},'approval':{}}
        previewed = {}
        composed = []
        def compose(source,destination,overlay):
            Image.new('RGB',(1080,1920),(1,2,3)).save(destination)
            composed.append(Path(destination))
        self.store.change('items/item-1',lambda value:value.update(status='queued'))
        with patch.object(app,'superseded',return_value=False), patch.object(app.drafter,'ask',return_value=spec), patch.object(app.media,'compose_story',side_effect=compose), patch.object(app.media,'upload',side_effect=lambda bucket,path,name:{'path':str(path)}), patch.object(app,'preview',side_effect=lambda config,store,bucket,item_id,value,assets,directory,revision=None:previewed.update(assets)), patch.object(app.media,'intake',return_value=[]), patch.object(app.platforms,'request',return_value={}):
            app.draft(self.config,self.store,None,{'item':'item-1','revision':1},self.root)
        self.assertEqual(len(composed),1)
        self.assertEqual(Image.open(composed[0]).size,(1080,1920))
        self.assertNotEqual(previewed['tg-story']['image0.jpg']['path'],previewed['tg-channel']['image0.jpg']['path'])


if __name__ == '__main__':
    unittest.main()
