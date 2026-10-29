from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

FUNCTIONS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FUNCTIONS))
from fake_firestore import FakeFirestore
import main as app
from store import Store


class DraftProgressTests(unittest.TestCase):
    def test_draft_step_opens_before_media_intake(self):
        store = Store(FakeFirestore())
        store.put('items/item-9',{'status':'queued','revision':1,'slug':'example','messages':[]})
        config = {'roman_user_id':1,'roman_chat_id':1}
        seen = []
        def intake(*args,**kwargs):
            steps = (store.get('sessions/item-9') or {}).get('steps',[])
            seen.append([s['id'] for s in steps if s.get('state') == 'running'])
            return []
        with tempfile.TemporaryDirectory() as temporary, patch.object(app.media,'intake',side_effect=intake), patch.object(app,'edit_media',return_value=([],[])), patch.object(app,'restrict',return_value=None), patch.object(app.platforms,'request',return_value=[]), patch.object(app,'acknowledge_remount'), patch.object(app.agents,'run_draft',return_value={'answer':'x'}), patch.object(app,'take_answer'), patch.object(app,'superseded',return_value=True):
            app.draft(config,store,None,{'item':'item-9','revision':1},Path(temporary))
        self.assertIn('draft',seen[0])


if __name__ == '__main__':
    unittest.main()


class PublishAndAlbumTests(unittest.TestCase):
    def test_publish_progress_message_is_fresh_and_short(self):
        store = Store(FakeFirestore())
        store.put('items/item-9',{'status':'previewed','revision':1,'messages':[]})
        store.put('specs/example',{'item':'item-9','state':'approved','status_message':{'id':5,'text':'x' * 4000}})
        with patch.object(app,'tell',return_value={'message_id':9}) as tell:
            record = app.open_publish_status({'roman_chat_id':1},store,'example')
        self.assertEqual(record['id'],9)
        self.assertLess(len(tell.call_args.args[1]),200)
        self.assertEqual(store.get('specs/example')['publish_message']['id'],9)
        self.assertEqual(store.get('specs/example')['status_message']['id'],5)

    def test_offer_sends_one_album_per_group_with_text_and_a_quoting_reply(self):
        store = Store(FakeFirestore())
        spec = {'slug':'example','targets':['ig-post','threads','ig-story','tg-story'],'copy':{'ig-post':'text','threads':'text','ig-story':'','tg-story':'story text'}}
        photo = {'sha256':'1','object':'o1'}
        other = {'sha256':'2','object':'o2'}
        assets = {'ig-post':{'image0.jpg':photo,'image1.jpg':other},'threads':{'image0.jpg':photo,'image1.jpg':other},'ig-story':{'image0.jpg':photo},'tg-story':{'image0.jpg':photo}}
        sent = []
        def send(method,fields,files=None,targets=None):
            sent.append((method,fields))
            return [{'message_id':10 * len(sent) + i} for i in range(2)] if method == 'sendMediaGroup' else {'message_id':10 * len(sent)}
        def say(text,targets=None,**extra):
            sent.append(('say',{'text':text,**extra}))
            return {'message_id':10 * len(sent)}
        with tempfile.TemporaryDirectory() as temporary, patch.object(app.platforms,'telegram_identity'), patch.object(app.platforms,'live',side_effect=lambda t:list(t)), patch.object(app.media,'materialize'), patch.object(app,'remove_keyboard'):
            app.offer({'roman_chat_id':1},store,None,'item-9',spec,assets,Path(temporary),None,None,{'slug':'example'},'d',{},send,say,[],{})
        methods = [m for m,f in sent]
        self.assertEqual(methods,['sendMediaGroup','say','sendPhoto','say','say'])
        self.assertEqual(sent[0][1]['media'][0]['caption'],'text')
        self.assertNotIn('caption',sent[0][1]['media'][1])
        self.assertEqual(sent[1][1]['text'],'Это пост Instagram и пост Threads')
        self.assertEqual(sent[1][1]['reply_parameters']['message_id'],10)
        self.assertNotIn('caption',sent[2][1])
        self.assertEqual(sent[3][1]['text'],'Это сторис Instagram, Telegram')
        self.assertEqual(sent[3][1]['reply_parameters']['message_id'],30)
        self.assertIn('куда пойдёт',sent[4][1]['text'])
