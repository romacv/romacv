import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

FUNCTIONS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FUNCTIONS))
from fake_firestore import FakeFirestore
import platforms as p
import main as app
from store import Store

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
        self.spec.pop('blog')
        self.assets = {target:{} for target in self.spec['targets']}
        self.digest = app.asset_hash(self.spec,self.assets)
        self.store.put('items/item-1', {'status':'previewed','revision':1,'messages':[]})
        self.store.put('specs/example', {'state':'previewed','hash':self.digest,'item':'item-1','preview_message':77,'spec':self.spec,'assets':self.assets,'results':{},'errors':{}})

    def callback(self, verb='pub', digest=None, identifier=1):
        return self.store.intake({'update_id':identifier,'callback_query':{'id':str(identifier),'data':f'{verb}:example:{(digest or self.digest)[:8]}'}},self.config)

    def test_draft_claim_never_repeats_running_or_old_revision(self):
        self.store.put('items/job',{'status':'queued','revision':2})
        self.assertFalse(self.store.claim_item('job',1))
        self.assertTrue(self.store.claim_item('job',2))
        self.assertFalse(self.store.claim_item('job',2))

    def test_draft_validation_returns_malformed_model_media_instead_of_crashing(self):
        self.store.put('items/item-2',{'status':'queued','revision':1,'slug':'example','messages':[]})
        seen = []
        def ask(config,source,images,validate,**kwargs):
            seen.append(validate({**self.spec,'media':{'clips':True}}))
            return {'answer':'x'}
        with patch.object(app.media,'intake',return_value=[]), patch.object(app,'edit_media',return_value=([],[])), patch.object(app,'restrict',return_value=None), patch.object(app.platforms,'request',return_value=[]), patch.object(app.drafter,'ask',side_effect=ask), patch.object(app,'superseded',return_value=True):
            app.draft(self.config,self.store,None,{'item':'item-2','revision':1},self.root)
        self.assertIn('Unknown media fields',seen[0])

    def test_edit_invalidates_approval_and_queues_revision(self):
        self.callback('edit')
        self.assertEqual(self.store.get('sessions/inbox')['awaiting'],{'kind':'edit','slug':'example'})
        result = self.store.intake({'update_id':42,'message':{'message_id':10,'text':'Make shorter'}},self.config)
        self.assertTrue(result)
        self.assertNotIn('awaiting',self.store.get('sessions/inbox'))
        saved = self.store.get('specs/example')
        self.assertEqual((saved['state'],saved['hash']),('drafting',''))
        self.assertEqual(self.store.get('items/item-1')['edits'],['Make shorter'])
        self.assertIsNotNone(self.store.get('outbox/edit-42'))


class PublishedMessageTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(FakeFirestore())
        self.config = {'roman_chat_id':1}
        self.store.put('items/item-1',{'status':'previewed','revision':1,'messages':[{'message_id':1,'text':'Тема'}],'slug':'album'})
        self.store.put('sessions/inbox',{'current':'album'})

    def say(self, identifier, text):
        return self.store.intake({'update_id':identifier,'message':{'message_id':identifier,'text':text}},self.config)

    def test_message_during_publishing_gets_status_not_the_post_hint(self):
        self.store.put('specs/album',{'state':'running','item':'item-1','spec':{'targets':['ig-story','tg-story']},'results':{'ig-story':{}},'errors':{'tg-story':'API failure: 400'}})
        reply = self.say(10,'ну как, опубликовал?')
        self.assertIn('готово 1 из 2',reply)
        self.assertIn('API failure: 400',reply)
        self.assertIsNone(self.store.get('outbox/fix-10'))

    def test_message_after_publishing_goes_to_the_model_once(self):
        self.store.put('specs/album',{'state':'partial','item':'item-1','spec':{'targets':['tg-channel'],'copy':{'tg-channel':'x'}},'results':{},'errors':{}})
        self.assertIn('Читаю по посту',self.say(10,'почему ошибка?'))
        self.assertIsNotNone(self.store.get('outbox/fix-10'))
        self.assertEqual(self.store.get('specs/album')['state'],'partial')
        self.assertIsNone(self.say(10,'почему ошибка?'))


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

    def test_album_while_publishing_gets_one_status_reply(self):
        self.store.put('specs/album',{'state':'running','item':'item-1','spec':{'targets':['tg-channel']},'results':{},'errors':{}})
        replies = self.album(10)
        self.assertIn('готово',replies[0])
        self.assertEqual(replies[1:],[None,None])

    def test_album_starting_a_post_drops_a_pending_fix_wait(self):
        self.store.put('specs/album',{'state':'done','item':'item-1','spec':{'targets':['tg-channel']},'results':{},'errors':{}})
        self.store.put('sessions/inbox',{'current':'album','awaiting':{'kind':'fix','slug':'album'}})
        replies = self.album(10)
        self.assertNotIn('awaiting',self.store.get('sessions/inbox'))
        self.assertEqual([bool(r and r.get('text')) for r in replies],[True,False,False])

    def test_one_tap_moves_every_photo_of_an_album_sent_to_a_draft(self):
        self.store.put('specs/album',{'state':'previewed','item':'item-1','spec':{'targets':['tg-channel']},'results':{},'errors':{}})
        replies = self.album(10)
        self.assertEqual(replies[1:],[None,None])
        data = replies[0]['reply_markup']['inline_keyboard'][0][0]['callback_data']
        self.store.intake({'update_id':99,'callback_query':{'id':'99','data':data}},self.config)
        self.assertEqual(self.store.get('items/item-1').get('edits',[]),[])
        self.assertEqual([m['message_id'] for m in self.store.get('items/item-gg1')['messages']],[10,11,12])


class FixAnswerTests(unittest.TestCase):
    def test_answer_is_sent_before_the_fix_is_closed(self):
        store = Store(FakeFirestore())
        spec = {'slug':'album','kind':'work','targets':['tg-channel'],'copy':{'tg-channel':'x'}}
        store.put('items/item-1',{'slug':'album'})
        store.put('specs/album',{'state':'done','item':'item-1','spec':spec,'results':{'tg-channel':{'url':'u','message_ids':[1],'copy':'x'}},'errors':{}})
        store.put('specs/fix-1',{'operation':'fix','source':'album','item':'item-1','instruction':'почему?','state':'drafting','results':{},'errors':{},'hash':''})
        with patch.object(app.drafter,'ask',return_value={'answer':'Потому что так.'}), patch.object(app,'tell',side_effect=RuntimeError('telegram down')):
            with self.assertRaises(RuntimeError):
                app.fix({'roman_chat_id':1},store,'fix-1')
        self.assertEqual(store.get('specs/fix-1')['state'],'drafting')


class PublishOrderTests(unittest.TestCase):
    def test_channel_publishes_before_its_story_and_nothing_else_moves(self):
        self.assertEqual(app.publish_order(['tg-story','ig-post','tg-channel','threads']),['ig-post','tg-channel','threads','tg-story'])
        self.assertEqual(app.publish_order(['tg-story','ig-story']),['tg-story','ig-story'])


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

    def test_late_preview_of_a_discarded_draft_is_dropped(self):
        self.store.intake(post_update(20),PLAIN)
        with patch.object(app.platforms,'telegram') as telegram:
            app.preview(PLAIN,self.store,None,'item-b',{'slug':'beta','targets':['tg-channel'],'copy':{'tg-channel':'x'}},{},Path('.'))
        telegram.assert_not_called()
        self.assertEqual(self.store.get('specs/beta')['state'],'rejected')


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
                self.tap(store,'done','item-10',token,12)
                item = store.get('items/item-10')
                self.assertEqual((item['status'],item['montage']),('queued',clips))
                self.assertEqual(store.get('outbox/draft-12')['payload'],{'item':'item-10','step':step,'revision':item['revision']})
                self.assertNotIn('collecting',store.get('sessions/inbox'))
                self.assertIsNone(self.tap(store,'done','item-10',token,13))

    def test_done_accepts_photos_with_clips_and_pins_then_unpins_the_status(self):
        store = Store(FakeFirestore())
        self.post(store,10,text='/post')
        self.video(store,11,'Jakarta Chinatown')
        store.intake({'update_id':12,'message':{'message_id':12,'photo':[{'file_id':'p','file_size':5}]}},self.config)
        calls = []
        def telegram(method,fields=None,files=None):
            calls.append((method,fields))
            return {'message_id':500}
        with patch.object(app.platforms,'telegram',side_effect=telegram):
            app.sync_pin(self.config,store)
            app.sync_pin(self.config,store)
            self.assertEqual([m for m,f in calls],['sendMessage','pinChatMessage'])
            self.assertIn('1 фото, 1 клип',calls[0][1]['text'])
            self.assertTrue(calls[1][1]['disable_notification'])
            token = store.get('items/item-10')['batch_token']
            self.tap(store,'done','item-10',token,13)
            app.sync_pin(self.config,store)
        item = store.get('items/item-10')
        self.assertEqual((item['status'],item['montage']),('queued',False))
        self.assertEqual(store.get('outbox/draft-13')['payload']['step'],'draft')
        self.assertEqual([m for m,f in calls][2:],['unpinChatMessage','deleteMessage'])
        self.assertEqual(calls[2][1]['message_id'],500)

    def test_pin_failure_keeps_the_status_message_and_retries_only_the_pin(self):
        store = Store(FakeFirestore())
        self.post(store,10,text='/post')
        calls = []
        def telegram(method,fields=None,files=None):
            calls.append(method)
            if method == 'pinChatMessage' and calls.count(method) == 1:
                raise p.Error('pin')
            return {'message_id':500}
        with patch.object(app.platforms,'telegram',side_effect=telegram):
            app.sync_pin(self.config,store)
            app.sync_pin(self.config,store)
        self.assertEqual(calls,['sendMessage','pinChatMessage','pinChatMessage'])

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


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(FakeFirestore())
        self.config = {'roman_chat_id':1}
        self.store.put('items/item-1',{'status':'previewed','revision':1,'messages':[{'message_id':1,'text':'Тема'}],'slug':'album','edits':[]})
        self.store.put('specs/album',{'state':'previewed','hash':'abcdefgh1','item':'item-1','spec':{'targets':['tg-channel']},'preview_message':100})
        self.store.put('published-messages/100',{'slug':'album'})

    def test_stuck_running_spec_survives_a_failed_notification(self):
        self.store.put('specs/album',{'state':'running','hash':'abcdefgh1','started_at':0,'item':'item-1','spec':{'copy':{}}})
        with patch.object(app,'tell',side_effect=RuntimeError('telegram down')):
            with self.assertRaises(RuntimeError):
                app.recover_running(self.config,self.store)
        with patch.object(app,'tell') as tell:
            app.recover_running(self.config,self.store)
        tell.assert_called_once()
        self.assertEqual(self.store.get('specs/album')['state'],'partial')


if __name__ == '__main__':
    unittest.main()
