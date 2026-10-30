import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

FUNCTIONS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FUNCTIONS))
from fake_firestore import FakeFirestore
import platforms as p
import main as app
from store import Store
from test_agents import FakeAnthropic, FakeStream
import agents

PLAIN = {'roman_user_id':1,'roman_chat_id':1}
POST = {'type':'bot_command','offset':0,'length':5}


def post_update(identifier):
    message = {'message_id':identifier,'from':{'id':1},'chat':{'id':1,'type':'private'},'text':'/post','entities':[POST]}
    return {'update_id':identifier,'message':message}


class EditFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(FakeFirestore())
        self.config = {'roman_user_id':1, 'roman_chat_id':1}
        p.configure(self.config, self.store, self.root)
        self.spec = json.loads((FUNCTIONS.parent / 'examples/example-work.json').read_text())
        self.spec.update(targets=['tg-channel','threads'], at='2020-01-01T00:00:00+00:00')
        self.assets = {target:{} for target in self.spec['targets']}
        self.digest = app.asset_hash(self.spec,self.assets)
        self.store.put('items/item-1', {'status':'previewed','revision':1,'messages':[]})
        self.store.put('specs/example', {'state':'previewed','hash':self.digest,'item':'item-1','preview_message':77,'spec':self.spec,'assets':self.assets,'results':{},'errors':{}})

    def callback(self, verb='pub', digest=None, identifier=1):
        return self.store.intake({'update_id':identifier,'callback_query':{'id':str(identifier),'data':f'{verb}:example:{(digest or self.digest)[:8]}'}},self.config)


    def test_draft_validation_returns_malformed_model_media_instead_of_crashing(self):
        self.store.put('items/item-2',{'status':'queued','revision':1,'slug':'example','messages':[]})
        seen = []
        def run_draft(config,store,item,revision,source,images,clips,validate,rec,live):
            seen.append(validate({**self.spec,'media':{'clips':True}}))
            return {'answer':'x'}
        with patch.object(app.media,'intake',return_value=[]), patch.object(app,'edit_media',return_value=([],[])), patch.object(app,'restrict',return_value=None), patch.object(app.platforms,'request',return_value=[]), patch.object(app.agents,'run_draft',side_effect=run_draft), patch.object(app,'superseded',return_value=True):
            app.draft(self.config,self.store,None,{'item':'item-2','revision':1},self.root)
        self.assertIn('Unknown media fields',seen[0])


    def test_failed_draft_offers_a_retry_that_requeues_the_same_step(self):
        class Request:
            data = {'step':'montage','item':'item-1','revision':1}
        self.store.put('items/item-1',{'status':'drafting','revision':1,'montage':True,'messages':[]})
        with patch.object(app,'runtime',return_value=(self.config,self.store,None)), patch.object(app,'activity'), patch.object(app,'sync_pin'), patch.object(app,'tell') as tell, patch.object(app,'plan_montage',side_effect=p.Error('Черновик не получился: причина.')):
            app.handle_worker(Request)
        markup = tell.call_args.kwargs['reply_markup']
        data = markup['inline_keyboard'][0][0]['callback_data']
        self.assertEqual(data,'again:item-1:1')
        self.assertTrue(tell.call_args.args[1].startswith('Черновик не получился') or ' не получился' in tell.call_args.args[1])
        self.store.intake({'update_id':60,'callback_query':{'id':'60','data':data}},self.config)
        self.assertEqual(self.store.get('items/item-1')['status'],'queued')
        self.assertEqual(self.store.get('outbox/again-60')['payload'],{'item':'item-1','step':'montage','revision':2})



    def test_edit_invalidates_approval_and_queues_revision(self):
        self.callback('edit')
        self.assertEqual(self.store.get('sessions/inbox')['awaiting'],{'kind':'edit','slug':'example'})
        result = self.store.intake({'update_id':42,'message':{'message_id':10,'text':'Make shorter'}},self.config)
        self.assertIsNone(result)
        self.assertNotIn('awaiting',self.store.get('sessions/inbox'))
        saved = self.store.get('specs/example')
        self.assertEqual((saved['state'],saved['hash']),('drafting',''))
        self.assertEqual(self.store.get('items/item-1')['edits'],['Make shorter'])
        self.assertIsNotNone(self.store.get('outbox/edit-42'))











class AlbumWithoutPostTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(FakeFirestore())
        self.config = {'roman_chat_id':1}
        self.store.put('items/item-1',{'status':'previewed','revision':1,'messages':[],'slug':'album'})
        self.store.put('sessions/inbox',{'current':'album'})

    def album(self, first):
        return [self.store.intake({'update_id':first + n,'message':{'message_id':first + n,'media_group_id':'g1','photo':[{'file_id':'f' + str(n),'file_size':10}]}},self.config) for n in range(3)]

    def test_album_after_publishing_starts_one_post_with_one_collect_message(self):
        self.store.put('specs/album',{'state':'done','item':'item-1','spec':{'targets':['tg-channel']},'results':{},'errors':{}})
        replies = self.album(10)
        self.assertEqual(self.store.get('sessions/inbox')['collecting'],'item-10')
        self.assertEqual(len(self.store.get('items/item-10')['messages']),3)
        self.assertEqual([bool(r and r.get('text')) for r in replies],[True,False,False])
















class PublishOrderTests(unittest.TestCase):
    def test_channel_publishes_before_its_story_and_nothing_else_moves(self):
        self.assertEqual(app.publish_order(['tg-story','ig-post','tg-channel','threads']),['ig-post','tg-channel','threads','tg-story'])
        self.assertEqual(app.publish_order(['tg-story','ig-story']),['tg-story','ig-story'])


class ChainedPublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(FakeFirestore())
        self.config = {'roman_user_id':1,'roman_chat_id':1}
        p.configure(self.config,self.store,self.root)
        self.spec = json.loads((FUNCTIONS.parent / 'examples/example-work.json').read_text())
        self.spec.update(targets=['tg-channel','threads'],at='2020-01-01T00:00:00+00:00')
        self.assets = {target:{} for target in self.spec['targets']}
        self.digest = app.asset_hash(self.spec,self.assets)
        self.store.put('items/item-1',{'status':'previewed','revision':1,'messages':[]})
        self.store.put('specs/example',{'state':'approved','hash':self.digest,'approved_hash':self.digest,'item':'item-1','spec':self.spec,'assets':self.assets,'results':{},'errors':{},'keyboard_removed':True})

    def test_each_invocation_publishes_one_target_and_the_last_sends_one_summary(self):
        with patch.object(app,'tell',return_value={'message_id':9}) as tell, patch.object(app,'enqueue') as enqueue, patch.object(app.media,'materialize'), patch.object(p,'social_session',return_value=('token','123',p.THREADS)), patch.object(p,'link_opens',return_value=True), patch.object(p,'post_telegram',return_value='https://t.me/example/1'), patch.object(p,'post_social',return_value='https://www.threads.com/@x/post/1'):
            app.run(self.config,self.store,None,'example',self.root)
            self.assertEqual(list(self.store.get('specs/example')['results']),['tg-channel'])
            enqueue.assert_called_once()
            self.assertEqual(enqueue.call_args[0][0],{'step':'run','slug':'example','resume':True})
            tell.assert_called_once()
            self.assertIn('Публикую',tell.call_args[0][1])
            self.assertEqual(self.store.get('specs/example')['state'],'running')
            app.run(self.config,self.store,None,'example',self.root,True)
        self.assertEqual([call[0][0]['step'] for call in enqueue.call_args_list],['run','verify'])
        self.assertEqual(tell.call_count,2)
        self.assertIn('Публикую',tell.call_args_list[0][0][1])
        self.assertNotIn('Публикую',tell.call_args_list[1][0][1])
        self.assertEqual((self.store.get('specs/example')['state'],sorted(self.store.get('specs/example')['results'])),('done',['tg-channel','threads']))







    def test_accept_step_closes_the_session_and_lists_the_failed_targets(self):
        class Request:
            data = {'step':'accept','slug':'example'}
        result = {'url':'https://t.me/example/1','verified':True}
        self.store.change('specs/example',lambda value:value.update(state='done',accepted=True,results={'tg-channel':result},errors={'threads':'Платформа не приняла запрос'}))
        with patch.object(app,'runtime',return_value=(self.config,self.store,None)), patch.object(app,'activity'), patch.object(app,'sync_pin') as pin, patch.object(app,'tell') as tell, patch.object(self.store,'close_session') as close:
            app.handle_worker(Request)
        close.assert_called_once_with('item-1','published','принято')
        pin.assert_called()
        self.assertIn('Платформа не приняла запрос',tell.call_args[0][1])




class PreviewRaceTests(unittest.TestCase):
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

    def test_preview_is_html_with_escaped_copy(self):
        self.spec['copy']['tg-channel'] = 'a <b> & c'
        sent = self.preview()
        fields = next(f for m,f in sent if m == 'sendMessage' and 'parse_mode' in f)
        self.assertEqual(fields['parse_mode'],'HTML')
        self.assertIn('<b>Черновик',fields['text'])
        self.assertIn('куда пойдёт</b>',fields['text'])
        self.assertNotIn('a <b> & c',fields['text'])
        shown = [f for m,f in sent if 'parse_mode' not in f and 'a <b> & c' in (f.get('caption') or f.get('text') or ''.join(x.get('caption','') for x in f.get('media',[])))]
        self.assertTrue(shown)








class DiscardTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(FakeFirestore())
        self.store.put('sessions/inbox',{'current':'alpha','awaiting':{'kind':'edit','slug':'alpha'}})
        self.store.put('items/item-a',{'status':'previewed','revision':1,'messages':[{'message_id':1,'text':'Alpha'}]})
        self.store.put('specs/alpha',{'item':'item-a','state':'previewed','spec':{'slug':'alpha','copy':{}},'preview_message':30,'hash':'abcdefgh'})
        self.store.put('items/item-b',{'status':'drafting','revision':1,'messages':[{'message_id':2,'text':'Beta'}]})
        self.store.put('specs/beta',{'item':'item-b','state':'drafting','spec':{'slug':'beta','copy':{}},'hash':'12345678'})

    def test_post_announces_and_rejects_open_drafts(self):
        result = self.store.intake(post_update(20),PLAIN)
        for slug, item in (('alpha','item-a'),('beta','item-b')):
            self.assertEqual(self.store.get('specs/' + slug)['state'],'rejected')
            self.assertEqual(self.store.get('items/' + item)['status'],'rejected')
        session = self.store.get('sessions/inbox')
        self.assertEqual(session['collecting'],'item-20')
        self.assertNotIn('awaiting',session)
        self.assertNotIn('current',session)
        job = self.store.get('outbox/discard-20-alpha')['payload']
        self.assertEqual((job['step'],job['message']),('remove-keyboard',30))




class CollectingTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(FakeFirestore())
        self.config = {'roman_chat_id':1,'webhook_secret':'test-secret'}

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
                self.assertIsNone(self.tap(store,'done','item-10',token,12))
                self.assertEqual(store.get('items/item-10')['status'],'collecting')
                self.assertIsNone(self.tap(store,'noin','item-10',token,14))
                item = store.get('items/item-10')
                self.assertEqual((item['status'],item['montage']),('queued',clips))
                self.assertEqual(store.get('outbox/draft-14')['payload'],{'item':'item-10','step':step,'revision':item['revision']})
                self.assertNotIn('collecting',store.get('sessions/inbox'))
                self.assertIsNone(self.tap(store,'done','item-10',token,13))

    def test_post_text_goes_to_copy_and_the_next_text_is_the_agent_instruction(self):
        store = Store(FakeFirestore())
        self.post(store,10,text='/post')
        store.intake({'update_id':11,'message':{'message_id':11,'text':'Завтрак в Arctic'}},self.config)
        token = store.get('items/item-10')['batch_token']
        self.assertIsNone(self.tap(store,'done','item-10',token,12))
        self.assertEqual(store.get('items/item-10')['status'],'collecting')
        store.intake({'update_id':13,'message':{'message_id':13,'text':'Соедини первые два видео в рилс, остальное как сторис'}},self.config)
        item = store.get('items/item-10')
        self.assertEqual(([m['text'] for m in item['messages']],item['instructions'],item['status']),(['Завтрак в Arctic'],['Соедини первые два видео в рилс, остальное как сторис'],'queued'))
        self.assertEqual(store.get('outbox/draft-13')['payload']['step'],'draft')










    def test_cancel_drops_item_and_stale_tokens_are_rejected(self):
        store = Store(FakeFirestore())
        self.post(store,10,text='/post')
        token = store.get('items/item-10')['batch_token']
        self.assertIsNone(self.tap(store,'done','item-10','wrongtok',11))
        self.assertEqual(store.get('items/item-10')['status'],'collecting')
        self.assertTrue(self.tap(store,'cncl','item-10',token,12))
        self.assertEqual(store.get('items/item-10')['status'],'rejected')
        self.assertNotIn('collecting',store.get('sessions/inbox'))
        self.assertIsNone(self.tap(store,'done','item-10',token,13))
        self.assertIsNone(store.get('outbox/draft-13'))
        store.intake({'update_id':14,'message':{'message_id':14,'text':'Привет'}},self.config)
        self.assertIsNone(store.get('items/item-14'))




class SupersededRevisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(FakeFirestore())
        self.config = {'anthropic_key':'k','roman_user_id':1,'roman_chat_id':1}
        p.configure(self.config,self.store,self.root)
        self.store.put('items/i',{'revision':1,'status':'queued','slug':'trip','messages':[]})

    def test_superseded_revision_stops_agents_and_writes_nothing(self):
        store = self.store
        class Bumping(FakeStream):
            def __iter__(self):
                yield SimpleNamespace(type='message_start')
                store.change('items/i',lambda value:value.update(revision=2))
                for _ in range(100):
                    time.sleep(0.05)
                    yield SimpleNamespace(type='content_block_stop',index=0)
        class Fake(FakeAnthropic):
            def stream(self, **kwargs):
                if 'Role: places agent' in kwargs['messages'][0]['content'][-1]['text']:
                    return Bumping('{}')
                return super().stream(**kwargs)
        fake = Fake({'media':[],'copy':[]})
        with patch.dict(sys.modules,{'anthropic':fake}), patch.object(agents,'POLL',0.05), patch.object(agents,'FLUSH',0.05), patch.object(app.media,'intake',return_value=[]), patch.object(app,'edit_media',return_value=([],[])), patch.object(app,'restrict',return_value=None), patch.object(app.platforms,'request',return_value=[]), patch.object(app,'preview') as preview, patch.object(app,'show_texts') as texts:
            app.draft(self.config,self.store,None,{'item':'i','revision':1},self.root)
        preview.assert_not_called()
        texts.assert_not_called()
        self.assertIsNone(self.store.get('specs/trip'))
        self.assertEqual(self.store.get('items/i')['status'],'drafting')
        entry = next(e for e in self.store.get('sessions/i')['steps'] if e['id'] == 'draft')
        self.assertEqual((entry['agents']['places']['status'],entry['agents']['copy']['status']),('cancelled','cancelled'))



class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(FakeFirestore())
        self.config = {'roman_user_id':1,'roman_chat_id':1}
        self.store.put('items/item-1',{'status':'previewed','revision':1,'messages':[],'slug':'example'})
        result = lambda target:{'url':'https://example.com/' + target,'verified':False}
        self.store.put('specs/example',{'state':'done','item':'item-1','spec':{'targets':['tg-channel','threads']},'results':{t:result(t) for t in ('tg-channel','threads')},'published':{t:result(t) for t in ('tg-channel','threads')}})

    def test_verify_closes_the_session_only_after_all_targets(self):
        found = lambda target,published,url:{'api':'ok','link':True,'url':url,'why':None} if target == 'tg-channel' else {'api':'missing','link':None,'url':url,'why':'API не нашёл пост'}
        with patch.object(p,'confirm',side_effect=found), patch.object(app,'enqueue') as enqueue:
            for attempt in (1,2):
                app.verify(self.config,self.store,'example',attempt)
                self.assertEqual(self.store.get('sessions/item-1')['state'],'open')
                self.assertEqual(enqueue.call_args[0][0],{'step':'verify','slug':'example','attempt':attempt + 1})
                self.assertEqual(enqueue.call_args[0][2],60 * attempt)
            enqueue.reset_mock()
            app.verify(self.config,self.store,'example',3)
        enqueue.assert_not_called()
        saved = self.store.get('specs/example')
        self.assertEqual((saved['results']['tg-channel']['verified'],saved['results']['threads']['verified']),(True,False))
        session = self.store.get('sessions/item-1')
        self.assertEqual((session['state'],session['outcome']['kind']),('published','published'))
        rows = next(e for e in session['steps'] if e['id'] == 'verify')['targets']
        self.assertEqual((rows['tg-channel']['status'],rows['threads']['status']),('verified','unverified'))
        self.assertTrue(rows['threads']['why'])
        self.assertFalse(self.store.close_session('item-1','published','again'))





if __name__ == '__main__':
    unittest.main()
