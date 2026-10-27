from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fake_firestore import FakeFirestore
from store import Store
import drafter
import main as app


class UxReviewTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(FakeFirestore())
        self.config = {'roman_chat_id':1}

    def message(self, identifier, **fields):
        return self.store.intake({'update_id':identifier,'message':{'message_id':identifier,**fields}},self.config)

    def tap(self, verb, slug, token, identifier):
        return self.store.intake({'update_id':identifier,'callback_query':{'id':str(identifier),'data':verb + ':' + slug + ':' + token}},self.config)

    def previewed(self, targets=('tg-channel','threads')):
        self.store.put('items/item-1',{'status':'previewed','revision':1,'messages':[{'message_id':1,'text':'Тема'}],'slug':'album'})
        self.store.put('specs/album',{'item':'item-1','spec':{'targets':list(targets),'copy':{t:'Текст' for t in targets}},'hash':'abcdef012345','state':'previewed','preview_targets':{'201':['tg-channel'],'202':['threads']}})
        self.store.put('published-messages/201',{'slug':'album'})
        self.store.put('published-messages/202',{'slug':'album'})
        self.store.put('sessions/inbox',{'current':'album'})

    def test_venue_and_bare_location_join_the_open_post(self):
        self.message(1,text='/post',entities=[{'type':'bot_command','offset':0,'length':5}])
        self.message(2,venue={'title':'Suka Espresso Ubud','address':'Jl. Raya','location':{'latitude':1.5,'longitude':2.5}},location={'latitude':1.5,'longitude':2.5})
        self.message(3,location={'latitude':-8.5,'longitude':115.25})
        texts = [m['text'] for m in self.store.get('items/item-1')['messages']]
        self.assertEqual(texts,['Место: Suka Espresso Ubud, Jl. Raya','Место (координаты): -8.5, 115.25'])

    def test_venue_as_feedback_is_an_edit_note(self):
        self.previewed()
        self.message(10,venue={'title':'Suka Espresso Ubud','address':'','location':{'latitude':1,'longitude':2}})
        self.assertEqual(self.store.get('items/item-1')['edits'],['Место: Suka Espresso Ubud'])

    def test_preview_records_targets_per_sent_message(self):
        spec = {'slug':'album','kind':'work','at':'2020-01-01T00:00:00+00:00','targets':['tg-channel','threads'],'copy':{'tg-channel':'Один','threads':'Два'},'approval':{}}
        self.store.put('items/item-1',{'status':'drafting','revision':1,'messages':[{'message_id':1,'text':'Тема'}],'slug':'album'})
        sent = []
        def telegram(method,fields,files=None):
            sent.append(method)
            return {'message_id':100 + len(sent)}
        with patch.object(app.platforms,'telegram',side_effect=telegram), patch.object(app.platforms,'telegram_identity'), tempfile.TemporaryDirectory() as root:
            app.preview({'roman_chat_id':1,'channel_title':'Канал'},self.store,None,'item-1',spec,{'tg-channel':{},'threads':{}},Path(root),revision=1)
        self.assertEqual(sorted(sum(self.store.get('specs/album')['preview_targets'].values(),[])),['tg-channel','threads'])

    def test_reply_to_a_preview_message_scopes_the_edit_by_stored_id(self):
        self.previewed()
        self.message(11,text='короче',reply_to_message={'message_id':202,'text':'Threads\nТекст'})
        self.assertEqual(self.store.get('items/item-1')['edits'],['[threads] короче'])

    def test_anchor_must_lie_in_the_centered_crop(self):
        overlay = lambda anchor:{'overlay':{'tg-story':{'text':'x','place':None,'anchor':anchor,'align':'left'}}}
        images = [{'path':'a.jpg','alt':'a'}]
        self.assertEqual(drafter.overlay_errors(overlay([0.1,0.5]),['tg-story'],images,(4000,3000)),['tg-story: overlay anchor must lie inside the centered 9:16 crop of the photo (x from 0.29 to 0.71)'])
        self.assertEqual(drafter.overlay_errors(overlay([0.5,0.5]),['tg-story'],images,(4000,3000)),[])
        self.assertEqual(drafter.overlay_errors(overlay([0.1,0.5]),['tg-story'],images),[])

    def test_personal_channel_and_threads_copy_must_match(self):
        spec = {'slug':'a','kind':'personal','at':datetime.now(timezone.utc).isoformat(),'targets':['tg-channel','threads'],'media':{},'copy':{'tg-channel':'Привет','threads':'Пока'},'approval':{}}
        self.assertIn('personal: tg-channel and threads copy must be identical',drafter.validate(spec,copy_only=True))
        spec['copy']['threads'] = 'Привет'
        self.assertNotIn('personal: tg-channel and threads copy must be identical',drafter.validate(spec,copy_only=True))

    def test_button_answers_name_the_draft_in_chat(self):
        self.previewed()
        self.tap('pub','album','abcdef01',20)
        self.assertEqual(self.store.get('outbox/callback-' + __import__('hashlib').sha256(b'20').hexdigest())['payload']['say'],'Публикую «Тема».')
        self.previewed()
        self.tap('rej','album','abcdef01',21)
        self.assertEqual(self.store.get('outbox/callback-' + __import__('hashlib').sha256(b'21').hexdigest())['payload']['say'],'Черновик «Тема» отклонён.')

    def test_cancel_says_the_post_name_in_chat(self):
        self.store.put('items/item-1',{'status':'collecting','revision':1,'messages':[{'message_id':1,'text':'Тема'}],'slug':'item-1','batch_token':'tok'})
        self.store.put('sessions/inbox',{'collecting':'item-1'})
        self.assertEqual(self.tap('cncl','item-1','tok',22),'Пост отменён.')
        self.assertEqual(self.store.get('outbox/say-22')['payload'],{'step':'say','say':'Пост «Тема» отменён.'})

    def test_message_during_first_draft_becomes_an_edit(self):
        self.store.put('items/item-1',{'status':'queued','revision':3,'messages':[{'message_id':1,'text':'Тема'}],'slug':'item-1'})
        self.store.put('sessions/inbox',{'current':'item-1'})
        reply = self.message(30,text='добавь про кофе')
        self.assertEqual(reply,'Добавил к черновику «Тема»: добавь про кофе.')
        item = self.store.get('items/item-1')
        self.assertEqual((item['edits'],item['revision'],item['status']),(['добавь про кофе'],4,'queued'))
        self.assertEqual((self.store.get('outbox/edit-30')['payload'],self.store.get('outbox/edit-30')['delay']),({'item':'item-1','step':'draft','revision':4},10))

    def test_failed_item_still_gets_the_post_hint(self):
        self.store.put('items/item-1',{'status':'failed','revision':3,'messages':[{'message_id':1,'text':'Тема'}],'slug':'item-1'})
        self.store.put('sessions/inbox',{'current':'item-1'})
        self.assertEqual(self.message(31,text='ещё'),'Чтобы начать пост, отправь /post.')


if __name__ == '__main__':
    unittest.main()
