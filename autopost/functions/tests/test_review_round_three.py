from pathlib import Path
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fake_firestore import FakeFirestore
from store import Store
import main as app
import media
import drafter
from store import titled


class RoundThreeTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(FakeFirestore())
        self.config = {'roman_chat_id':1}
        self.store.put('items/item-1',{'status':'previewed','revision':1,'messages':[{'message_id':1,'text':'Тема'}],'slug':'album','edits':[]})
        self.store.put('specs/album',{'state':'previewed','hash':'abcdefgh1','item':'item-1','spec':{'targets':['tg-channel']},'preview_message':100})
        self.store.put('published-messages/100',{'slug':'album'})

    def post_reply(self, identifier=10):
        message = {'message_id':identifier,'text':'/post','entities':[{'type':'bot_command','offset':0,'length':5}],'reply_to_message':{'message_id':100,'text':'Черновик: куда пойдёт'}}
        return self.store.intake({'update_id':identifier,'message':message},self.config)

    def test_post_command_replying_to_preview_opens_a_post_and_clears_awaiting(self):
        self.store.put('sessions/inbox',{'awaiting':{'kind':'edit','slug':'album'}})
        self.post_reply()
        session = self.store.get('sessions/inbox')
        self.assertNotIn('awaiting',session)
        self.assertEqual(session['collecting'],'item-10')
        self.assertEqual(self.store.get('specs/album')['state'],'previewed')
        self.assertEqual(self.store.get('items/item-1')['revision'],1)

    def test_post_command_replying_to_published_message_does_not_start_fix(self):
        self.store.put('specs/album',{'state':'done','item':'item-1','hash':'abcdefgh1','spec':{'targets':['tg-channel']}})
        self.post_reply()
        self.assertIsNone(self.store.get('specs/fix-10'))
        self.assertEqual(self.store.get('sessions/inbox')['collecting'],'item-10')

    def test_repeated_fix_tap_enqueues_one_prompt(self):
        self.store.put('specs/album',{'state':'done','item':'item-1','hash':'abcdefgh1','spec':{'targets':['tg-channel']}})
        taps = [self.store.intake({'update_id':n,'callback_query':{'id':str(n),'data':'pfix:album:abcdefgh'}},self.config) for n in (20,21)]
        self.assertEqual(taps,[True,'Жду правку: напиши, что поменять.'])
        prompts = [k for k,v in self.store.db.values.items() if k.startswith('outbox/') and v['payload']['step'] == 'fix-prompt']
        self.assertEqual(len(prompts),1)

    def test_partial_final_retry_survives_a_failed_notification(self):
        self.store.put('specs/album',{'state':'rendering','hash':'abcdefgh1','final_render':'f1','started_at':time.time(),'item':'item-1','spec':{'copy':{}}})
        with patch.object(app,'tell',side_effect=RuntimeError('telegram down')):
            with self.assertRaises(RuntimeError):
                app.fail_final(self.config,self.store,'album','f1')
        saved = self.store.get('specs/album')
        self.assertEqual((saved['state'],saved['retry_pending']),('partial','final'))
        with patch.object(app,'tell') as tell:
            app.recover_running(self.config,self.store)
            app.recover_running(self.config,self.store)
        tell.assert_called_once()
        self.assertIn('retry:album:abcdefgh',str(tell.call_args.kwargs['reply_markup']))
        self.assertNotIn('retry_pending',self.store.get('specs/album'))

    def test_stuck_running_spec_survives_a_failed_notification(self):
        self.store.put('specs/album',{'state':'running','hash':'abcdefgh1','started_at':0,'item':'item-1','spec':{'copy':{}}})
        with patch.object(app,'tell',side_effect=RuntimeError('telegram down')):
            with self.assertRaises(RuntimeError):
                app.recover_running(self.config,self.store)
        with patch.object(app,'tell') as tell:
            app.recover_running(self.config,self.store)
        tell.assert_called_once()
        self.assertEqual(self.store.get('specs/album')['state'],'partial')

    def test_story_block_is_centred_on_the_anchor_for_every_alignment(self):
        directory = Path(tempfile.mkdtemp())
        source = directory/'plate.jpg'
        Image.new('RGB',(1080,1920),(120,120,120)).save(source,'JPEG')
        for align in ('left','center','right'):
            x0,_,x1,_ = media.compose_story(source,directory/'story.jpg',{'text':'Утро','place':None,'anchor':[0.5,0.5],'align':align})
            self.assertAlmostEqual((x0+x1)/2,540,delta=1)

    def test_fix_drops_overlay_of_unpublished_story(self):
        spec = {'slug':'album','kind':'personal','at':'2020-01-01T00:00:00+00:00','targets':['tg-channel','tg-story'],'copy':{'tg-channel':'Текст'},'media':{'images':[{'path':'p.jpg','alt':'Кофейня'}]},'overlay':{'tg-story':{'text':'Бали','place':None,'anchor':[0.5,0.5],'align':'center'}},'approval':{}}
        self.store.put('specs/album',{'state':'partial','item':'item-1','spec':spec,'results':{'tg-channel':{'url':'u','message_ids':[1],'copy':'Текст'}},'errors':{'tg-story':'x'},'hash':'abcdefgh1'})
        self.store.put('specs/fix-1',{'operation':'fix','source':'album','item':'item-1','instruction':'правка','state':'drafting','results':{},'errors':{},'hash':''})
        seen = {}
        def ask(config,payload,images,validator):
            seen['errors'] = validator({'copy':{'tg-channel':'Новый текст'},'blog':None})
            return {'copy':{'tg-channel':'Новый текст'},'blog':None}
        with patch.object(drafter,'ask',side_effect=ask), patch.object(app,'tell',return_value={'message_id':30}):
            app.fix(self.config,self.store,'fix-1')
        self.assertEqual(seen['errors'],[])
        self.assertNotIn('tg-story',self.store.get('specs/fix-1')['spec'].get('overlay',{}))

    def test_overlay_text_must_equal_the_story_copy(self):
        spec = {'copy':{'tg-story':'Утро в Убуде'},'overlay':{'tg-story':{'text':'Другая строка','place':None,'anchor':[0.5,0.5],'align':'center'}}}
        images = [{'path':'a.jpg','alt':'a'}]
        self.assertEqual(drafter.overlay_errors(spec,['tg-story'],images),['tg-story: overlay text must equal the target copy'])
        spec['overlay']['tg-story']['text'] = 'Утро в Убуде'
        self.assertEqual(drafter.overlay_errors(spec,['tg-story'],images),[])

    def run_worker(self, payload, tell):
        with patch.object(app,'runtime',return_value=(self.config,self.store,Mock())), patch.object(app,'tell',side_effect=tell):
            app.handle_worker(SimpleNamespace(data=payload))

    def test_fix_prompt_names_the_post(self):
        self.store.put('specs/album',{'state':'done','item':'item-1','spec':{},'hash':'abcdefgh1'})
        self.store.change('items/item-1',lambda value:value.update(messages=[{'message_id':1,'text':'Кофе в Убуде'}]))
        sent = []
        self.run_worker({'step':'fix-prompt','slug':'album'},lambda config,text,**fields:sent.append(text))
        self.assertEqual(sent,['Напиши, что поправить в посте «Кофе в Убуде».'])

    def test_failed_publishing_notice_does_not_touch_run_state(self):
        self.store.put('specs/album',{'state':'approved','item':'item-1','spec':{},'hash':'abcdefgh1'})
        def tell(config,text,**fields):
            raise RuntimeError('telegram down')
        with patch.object(app,'run') as run:
            self.run_worker({'step':'run','slug':'album','say':'Публикую.'},tell)
        run.assert_called_once()
        self.assertEqual(self.store.get('specs/album')['state'],'approved')

    def test_venue_message_does_not_become_the_title(self):
        place = {'message_id':1,'text':'Место: Suka Espresso, Ubud'}
        text = {'message_id':2,'caption':'Утренний кофе'}
        self.assertEqual(titled({'messages':[place,text]}),' «Утренний кофе»')
        self.assertEqual(titled({'messages':[place]}),' «Место: Suka Espresso, Ubud»')
