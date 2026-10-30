from pathlib import Path
import sys
import time
import unittest
from unittest.mock import Mock, patch

FUNCTIONS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FUNCTIONS))
from fake_firestore import FakeFirestore
import drafter
import main as app
import platforms
import store as store_module
import voice
from store import Store, SHOWING, UNCLEAR, word


class Base(unittest.TestCase):
    def setUp(self):
        self.store = Store(FakeFirestore())
        self.config = {'roman_chat_id':1}

    def say(self, identifier, text, **extra):
        return self.store.intake({'update_id':identifier,'message':{'message_id':identifier,**({'text':text} if text is not None else {}),**extra}},self.config)

    def published(self):
        self.store.put('items/old',{'status':'done','revision':1,'messages':[],'slug':'old','title':'Старый'})
        self.store.put('specs/old',{'state':'done','item':'old','published_at':5,'spec':{'targets':['tg-channel']},'results':{'tg-channel':{'url':'https://t.me/x/1','at':1}},'errors':{}})

    def drafting(self):
        self.store.put('items/item-9',{'status':'drafting','revision':8,'slug':'item-9','messages':[{'message_id':1,'text':'Тема'}]})
        self.store.put('sessions/inbox',{'current':'item-9'})


class StalledDraftTests(Base):
    def test_stopped_draft_keeps_material_and_offers_a_retry(self):
        self.store.put('items/item-1',{'status':'drafting','revision':3,'slug':'item-1','updated':time.time() - 10 ** 6,'messages':[{'message_id':n,'photo':[{'file_id':str(n)}]} for n in range(7)]})
        self.store.put('specs/item-1',{'state':'drafting','item':'item-1','updated':time.time() - 10 ** 6})
        sent = []
        with patch.object(app,'tell',side_effect=lambda config,text,**extra:sent.append((text,extra))):
            app.stop_stale(self.config,self.store)
        self.assertEqual(len(self.store.get('items/item-1')['messages']),7)
        text,extra = sent[0]
        self.assertIn('Материал сохранён',text)
        self.assertEqual(extra['reply_markup']['inline_keyboard'][0][0]['callback_data'],'again:item-1:3')

    def test_retry_rebuilds_a_stopped_draft_from_its_material(self):
        self.store.put('items/item-1',{'status':'failed','revision':3,'slug':'item-1','messages':[{'message_id':1,'text':'Тема'}]})
        taps = []
        result = self.store.intake({'update_id':5,'callback_query':{'id':'5','data':'again:item-1:3','message':{'message_id':9,'text':'x'}}},self.config,taps)
        self.assertTrue(result)
        self.assertEqual(self.store.get('items/item-1')['status'],'queued')
        self.assertEqual(self.store.get('outbox/again-5')['payload']['step'],'draft')

    def test_a_model_answer_with_no_drafting_spec_is_told_not_dropped(self):
        self.store.put('items/item-1',{'status':'drafting','revision':1,'slug':'s','messages':[]})
        self.store.put('specs/s',{'state':'previewed','spec':{}})
        said = []
        with patch.object(app,'tell',side_effect=lambda config,text,**extra:said.append(text)):
            app.answer_edit(self.config,self.store,{'item':'item-1','revision':1},{'slug':'s','messages':[]},'Готово, убрал хэштеги.')
        self.assertEqual(len(said),1)
        self.assertIn('убрал хэштеги',said[0])

    def test_one_draft_starts_one_prepare_render(self):
        item = {'montage':True}
        payload = {'item':'item-1','revision':2}
        inputs = [{'kind':'video','name':'a.mp4','manifest':{}}]
        bucket = Mock()
        bucket.name = 'b'
        config = {'roman_chat_id':1,'worker_url':'u','runtime_service_account':'s'}
        with patch.object(app.firebase_admin,'get_app',return_value=Mock(project_id='p')), patch.object(app,'start_job') as start:
            self.store.put('items/item-1',{'status':'drafting','revision':2})
            app.start_preparation(config,self.store,bucket,payload,item,inputs)
            app.start_preparation(config,self.store,bucket,payload,item,inputs)
        self.assertEqual(start.call_count,1)


class CollectKeyboardTests(Base):
    def test_typed_done_removes_the_collect_keyboard(self):
        self.store.put('items/item-5',{'status':'collecting','revision':1,'messages':[{'message_id':1,'text':'Тема'}],'slug':'item-5','batch_token':'tok','keyboard_message':77})
        self.store.put('sessions/inbox',{'collecting':'item-5'})
        self.say(4,'Готово')
        jobs = [v['payload'] for k,v in self.store.db.values.items() if k.startswith('outbox/unkey-collect')]
        self.assertEqual([(j['step'],j['message']) for j in jobs],[('remove-keyboard',77)])


class TypedWordsTests(Base):
    def test_leading_particles_do_not_hide_the_accept_word(self):
        for text in ('Тогда принять','ну принять','ок, публикуй','Окей, принять.'):
            self.assertEqual(word(text),'accept',text)
        self.assertIsNone(word('ну давай короче'))

    def test_particle_accept_accepts_the_published_post(self):
        self.published()
        self.assertEqual(self.say(3,'Тогда принять'),'')
        self.assertTrue(self.store.get('specs/old')['accepted'])


class RoutingTests(Base):
    def test_show_while_drafting_says_it_is_still_being_prepared(self):
        self.drafting()
        self.store.classifier = lambda text,context:{'kind':'caption','value':None}
        self.assertEqual(self.say(1,'покажи'),SHOWING)
        self.assertEqual(self.store.get('items/item-9')['revision'],8)

    def test_when_questions_are_not_table_requests(self):
        self.published()
        self.drafting()
        self.store.classifier = lambda text,context:{'kind':'question','value':None}
        reply = self.say(2,'когда будет готово?')
        self.assertNotIn('t.me/x/1',reply)

    def test_link_questions_get_the_table_even_while_a_new_post_drafts(self):
        self.published()
        self.drafting()
        self.store.classifier = lambda text,context:{'kind':'question','value':None}
        reply = self.say(2,'где ссылки на прошлый пост?')
        self.assertIn('t.me/x/1',reply)

    def test_failed_classifier_asks_instead_of_guessing(self):
        self.drafting()
        def broken(text,context):
            raise RuntimeError
        self.store.classifier = broken
        self.assertEqual(self.say(3,'сделай короче'),UNCLEAR)
        self.assertNotIn('edits',self.store.get('items/item-9'))

    def test_a_captioned_photo_with_a_question_is_answered_not_collected(self):
        self.published()
        self.store.classifier = lambda text,context:{'kind':'question','value':None}
        reply = self.say(4,None,caption='покажи таблицу постов',photo=[{'file_id':'f','file_unique_id':'u','file_size':10}])
        self.assertIn('t.me/x/1',reply)

    def test_a_captioned_photo_that_is_material_is_collected(self):
        self.store.classifier = lambda text,context:{'kind':'caption','value':None}
        reply = self.say(5,None,caption='Гонконг ночью',photo=[{'file_id':'f','file_unique_id':'u','file_size':10}])
        self.assertIn('Добавил в пост',reply['text'])


class ReviewFixTests(Base):
    def collecting(self):
        self.store.put('items/item-5',{'status':'collecting','revision':1,'messages':[{'message_id':1,'text':'Тема'}],'slug':'item-5','batch_token':'tok'})
        self.store.put('sessions/inbox',{'collecting':'item-5'})

    def test_retry_after_a_finished_render_prepares_again(self):
        config = {'roman_chat_id':1,'worker_url':'u','runtime_service_account':'s'}
        inputs = [{'kind':'video','name':'a.mp4','manifest':{}}]
        bucket = Mock()
        bucket.name = 'b'
        self.store.put('items/item-1',{'status':'drafting','revision':3,'slug':'item-1','messages':[]})
        with patch.object(app.firebase_admin,'get_app',return_value=Mock(project_id='p')), patch.object(app,'start_job') as start:
            app.start_preparation(config,self.store,bucket,{'item':'item-1','revision':3},{},inputs)
            for key,value in list(self.store.db.values.items()):
                if key.startswith('renders/'):
                    self.store.put(key,{**value,'state':'done'})
            self.store.put('items/item-1',{'status':'failed','revision':3,'slug':'item-1','messages':[]})
            self.store.intake({'update_id':7,'callback_query':{'id':'7','data':'again:item-1:3','message':{'message_id':9,'text':'x'}}},config,[])
            self.assertEqual(self.store.get('items/item-1')['revision'],4)
            app.start_preparation(config,self.store,bucket,{'item':'item-1','revision':4},{},inputs)
        self.assertEqual(start.call_count,2)

    def test_captioned_photo_while_collecting_is_kept_and_not_classified(self):
        self.collecting()
        self.store.classifier = lambda text,context:{'kind':'question','value':None}
        with patch.object(app.drafter,'classify',side_effect=AssertionError):
            self.say(6,None,caption='где ссылки?',photo=[{'file_id':'f','file_unique_id':'u','file_size':10}])
        messages = self.store.get('items/item-5')['messages']
        self.assertEqual(len(messages),2)
        self.assertTrue(messages[1]['photo'])

    def test_publish_word_with_a_previewed_draft_does_not_accept_an_old_post(self):
        self.published()
        self.store.put('items/item-9',{'status':'previewed','revision':2,'slug':'item-9','messages':[{'message_id':1,'text':'Тема'}]})
        self.store.put('specs/item-9',{'state':'previewed','item':'item-9','spec':{'targets':['tg-channel']}})
        self.store.put('sessions/inbox',{'current':'item-9'})
        self.assertIn('Опубликовать',self.say(3,'ок, публикуй'))
        self.assertFalse(self.store.get('specs/old').get('accepted'))

    def test_publish_word_with_a_drafting_draft_does_not_accept_an_old_post(self):
        self.published()
        self.drafting()
        reply = self.say(3,'публикуй')
        self.assertIn('готовится',reply)
        self.assertFalse(self.store.get('specs/old').get('accepted'))

    def test_show_with_content_is_an_edit(self):
        self.drafting()
        self.store.classifier = lambda text,context:{'kind':'fix','value':None}
        self.say(4,'покажи цены в тексте')
        item = self.store.get('items/item-9')
        self.assertEqual((item['revision'],item['edits']),(9,['покажи цены в тексте']))

    def test_classifier_failure_while_collecting_keeps_collecting(self):
        self.collecting()
        def broken(text,context):
            raise RuntimeError
        self.store.classifier = broken
        self.say(8,'сделай короче')
        self.assertEqual(len(self.store.get('items/item-5')['messages']),2)

    def test_unclear_is_asked_once_and_pending_text_applies_on_in_post(self):
        self.drafting()
        def broken(text,context):
            raise RuntimeError
        self.store.classifier = broken
        self.assertEqual(self.say(1,'сделай короче'),UNCLEAR)
        self.assertFalse(self.say(2,'и без хэштегов'))
        self.assertEqual(self.store.get('items/item-9')['pending_text'],['сделай короче','и без хэштегов'])
        self.assertFalse(self.say(3,'в пост'))
        item = self.store.get('items/item-9')
        self.assertEqual(item['edits'],['сделай короче\nи без хэштегов'])
        self.assertNotIn('pending_text',item)

    def test_particles_do_not_turn_post_text_into_commands(self):
        self.assertIsNone(word('Ну и готово'))
        self.assertEqual(word('Готово'),'done')


class ClassifierRetryTests(unittest.TestCase):
    def test_classifier_is_retried_once(self):
        good = Mock(content=[Mock(type='text',text='{"kind":"question","value":null}')],usage=None)
        client = Mock()
        client.with_options.return_value.messages.create.side_effect = [RuntimeError(),good]
        with patch.object(drafter,'connect',return_value=client), patch.object(drafter,'note'), patch.object(drafter,'final_text',return_value='{"kind":"question","value":null}'):
            self.assertEqual(drafter.classify({},'x',{})['kind'],'question')
        self.assertEqual(client.with_options.return_value.messages.create.call_count,2)

    def test_two_failures_raise(self):
        client = Mock()
        client.with_options.return_value.messages.create.side_effect = RuntimeError()
        with patch.object(drafter,'connect',return_value=client):
            with self.assertRaises(RuntimeError):
                drafter.classify({},'x',{})


class FirstDraftAnswerTests(Base):
    def test_skills_action_records_the_skill_and_still_demands_the_draft(self):
        item = {'messages':[{'text':'запомни: без хэштегов'}]}
        with patch.object(app,'enqueue') as queued:
            first = app.first_draft_skill({'item':'item-1'},item,'a draft')
            app.first_draft_skill({'item':'item-1'},item,'a draft')
        self.assertIn('now return a draft',first[0])
        self.assertEqual(queued.call_args_list[0].args[0],{'step':'skills','text':'запомни: без хэштегов'})
        self.assertEqual(queued.call_args_list[0].args[1],queued.call_args_list[1].args[1])


class RoutedVoiceTests(Base):
    def setUp(self):
        super().setUp()
        self.store.put('items/item-1',{'status':'drafting','revision':1,'messages':[]})

    def run_voice(self, kind):
        item = {'status':'drafting'}
        with patch.object(app.drafter,'classify',return_value={'kind':kind}), patch.object(app,'enqueue'), patch.object(app,'tell'):
            first = app.side_voice(self.config,self.store,{'item':'item-1'},item,'1.ogg','запиши в скиллы')
            again = app.side_voice(self.config,self.store,{'item':'item-1'},item,'1.ogg','запиши в скиллы')
        return first,again

    def test_rule_and_question_voices_stay_out_of_the_post_material(self):
        for kind in ('rule','question'):
            self.assertEqual(self.run_voice(kind),(True,True))

    def test_caption_voice_stays_in_the_post_material(self):
        self.assertEqual(self.run_voice('caption'),(False,False))


class ExpiredTokenNoticeTests(Base):
    def test_expired_instagram_token_is_reported_once(self):
        told = []
        with patch.object(voice,'notify',told.append), patch.object(voice,'graph',side_effect=platforms.Error('API failure: 190')), patch.object(voice,'channel',return_value=[]):
            voice.refresh(self.config,self.store,networks={'instagram'})
            voice.refresh(self.config,self.store,networks={'instagram'})
        self.assertEqual(told,['instagram'])

    def test_notice_text_reuses_the_token_message(self):
        sent = []
        with patch.object(app,'tell',side_effect=lambda config,text:sent.append(text)):
            app.token_notice(self.config)('instagram')
        self.assertEqual(sent,['Нужен новый токен Instagram.'])


class SupersededDraftTests(unittest.TestCase):
    def test_superseded_draft_does_not_claim_roman_cancelled_it(self):
        store = Store(FakeFirestore())
        store.put('items/item-1',{'status':'queued','revision':2})
        store.put('specs/s',{'state':'drafting','hash':'h'})
        sent = []
        with patch.object(app.platforms,'telegram',side_effect=lambda method,fields,files=None:sent.append(fields)):
            self.assertTrue(app.cancel_draft({'roman_chat_id':1},store,'item-1','s','h',[(5,'sendMessage')],1))
        self.assertNotIn('отменён',sent[0]['text'])


if __name__ == '__main__':
    unittest.main()
