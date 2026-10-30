from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

FUNCTIONS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FUNCTIONS))
from fake_firestore import FakeFirestore
import main as app
from store import Store


class FlowFixTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(FakeFirestore())
        self.config = {'roman_chat_id':1}

    def say(self, identifier, text, **extra):
        return self.store.intake({'update_id':identifier,'message':{'message_id':identifier,'text':text,**extra}},self.config)

    def published(self, slug, item, **fields):
        self.store.put('items/' + item,{'status':'done','revision':1,'messages':[],'slug':slug,'title':fields.pop('title','Пост')})
        self.store.put('specs/' + slug,{'state':'done','item':item,'published_at':fields.pop('at',5),'spec':{'targets':['tg-channel','threads']},'results':{'tg-channel':{'url':'https://t.me/x/1','at':1}},'errors':{},**fields})

    def test_typed_retry_after_token_renewal_resets_the_token_target(self):
        self.published('album','item-1',state='partial',errors={'threads':'Нужен новый токен Threads.'},attempts={'tg-channel':1,'threads':3},hash='abcdefgh')
        self.store.put('sessions/inbox',{'current':'album'})
        self.assertIn('Повторяю',self.say(1,'Повторить'))
        saved = self.store.get('specs/album')
        self.assertEqual((saved['state'],saved['attempts']),('approved',{'tg-channel':1}))

    def test_retry_button_after_token_renewal_resets_the_token_target(self):
        self.published('album','item-1',state='partial',errors={'threads':'Нужен новый токен Threads.'},attempts={'threads':3},hash='abcdefgh')
        self.store.intake({'update_id':2,'callback_query':{'id':'2','data':'retry:album:abcdefgh'}},self.config)
        saved = self.store.get('specs/album')
        self.assertEqual((saved['state'],saved['attempts']),('approved',{}))

    def test_typed_accept_never_accepts_an_older_post_while_the_current_one_publishes(self):
        self.published('old','item-old',state='done')
        self.store.put('items/item-1',{'status':'approved','revision':1,'messages':[],'slug':'album','title':'Новый'})
        self.store.put('specs/album',{'state':'running','item':'item-1','spec':{'targets':['tg-channel']},'results':{},'errors':{}})
        self.store.put('sessions/inbox',{'current':'item-1'})
        reply = self.say(3,'принять')
        self.assertIn('Публикую',reply)
        self.assertNotIn('accepted',self.store.get('specs/old'))

    def test_accepted_word_typed_while_collecting_is_material(self):
        self.published('old','item-old',state='done')
        self.store.put('items/item-5',{'status':'collecting','revision':1,'messages':[{'message_id':1,'text':'Тема'}],'slug':'item-5','batch_token':'tok'})
        self.store.put('sessions/inbox',{'collecting':'item-5'})
        self.say(4,'Принято')
        self.assertNotIn('accepted',self.store.get('specs/old'))
        self.assertEqual([m['text'] for m in self.store.get('items/item-5')['messages']],['Тема','Принято'])

    def test_both_skills_orders_at_one_revision_are_delivered(self):
        self.store.put('items/item-1',{'status':'previewed','revision':1,'messages':[],'slug':'album'})
        self.store.put('specs/album',{'state':'previewed','item':'item-1','spec':{'targets':['tg-channel']},'hash':'abcdefgh'})
        delivered, seen, sent = [], set(), []
        def enqueue(payload, identifier, delay=0):
            if identifier not in seen:
                seen.add(identifier)
                delivered.append(payload['text'])
        with patch.object(app,'enqueue',side_effect=enqueue), patch.object(app,'tell',side_effect=lambda config,text,**extra:sent.append(text)):
            for number,text in ((1,'запиши в скиллы А'),(2,'запиши в скиллы Б'),(2,'запиши в скиллы Б')):
                app.take_answer(self.config,self.store,{'item':'item-1','revision':1,'slug':'album','text':text,'update':str(number)},self.store.get('items/item-1'),{'action':'skills'})
        self.assertEqual(delivered,['запиши в скиллы А','запиши в скиллы Б'])
        self.assertEqual(len(sent),3)

    def test_post_narration_never_triggers_a_table_or_a_rule(self):
        item = {'status':'drafting','messages':[]}
        self.store.put('items/item-1',item)
        def classify(config,text,context,rec=None):
            return {'kind':'caption' if context['collecting'] else 'question'}
        with patch.object(app.drafter,'classify',side_effect=classify), patch.object(app,'tell') as tell, patch.object(app,'enqueue') as enqueue:
            app.side_voice(self.config,self.store,{'item':'item-1'},item,'v1','показываю экран настроек')
        tell.assert_not_called()
        enqueue.assert_not_called()

    def test_unsure_text_on_a_previewed_draft_is_not_lost_as_a_question(self):
        self.store.put('items/album',{'status':'previewed','revision':1,'messages':[],'slug':'album'})
        self.store.put('specs/album',{'state':'previewed','item':'album','spec':{'targets':['tg-channel'],'copy':{'tg-channel':'x'}},'hash':'abcdefgh'})
        self.store.put('sessions/inbox',{'current':'album'})
        self.store.classifier = lambda text,context:{'kind':'caption' if context['collecting'] else 'question'}
        self.say(6,'ещё про цены')
        self.assertIsNone(self.store.get('outbox/answer-6'))
        self.assertIn('ещё про цены',str(self.store.get('items/album')) + str(self.store.get('specs/album')) + str(self.store.get('outbox/edit-6')))

    def test_reply_to_a_bot_message_goes_to_the_model_not_the_post_hint(self):
        self.published('album','item-1',state='done',deleted=True)
        self.store.put('published-messages/9',{'slug':'album'})
        self.store.put('sessions/inbox',{'current':'album'})
        reply = self.say(7,'Ужасно это дубликат',reply_to_message={'message_id':9,'text':'Отчёт о публикации','from':{'is_bot':True}})
        self.assertIsNone(reply)
        job = self.store.get('outbox/reply-7')['payload']
        sent = []
        with patch.object(app,'tell',side_effect=lambda config,text,**extra:sent.append(text)), patch.object(app.drafter,'converse',return_value='Понял, смотрю дубликат в отчёте.'):
            app.reply_note(self.config,self.store,job)
        self.assertEqual(sent,['Понял, смотрю дубликат в отчёте.'])

    def test_why_question_about_a_hung_draft_explains_that_draft(self):
        self.published('uniqlo','item-u',title='Uniqlo')
        self.store.put('items/item-b',{'status':'failed','revision':3,'messages':[],'slug':'item-b','title':'Life in Bali','stalled':True,'updated':100})
        self.store.put('sessions/item-b',{'state':'open','steps':[{'id':'draft','label':'Черновик','state':'running','since':time.time() - 3600}]})
        self.store.put('sessions/inbox',{'current':'item-b'})
        self.store.classifier = lambda text,context:{'kind':'question','value':None}
        reply = self.say(8,'Почему завис')
        self.assertIn('Life in Bali',reply)
        self.assertIn('Черновик',reply)
        self.assertIn('https://t.me/x/1',reply)

    def test_old_failed_draft_does_not_hide_the_links_of_the_latest_post(self):
        self.published('uniqlo','item-u',title='Uniqlo',at=100)
        self.store.put('items/item-b',{'status':'failed','revision':3,'messages':[],'slug':'item-b','title':'Life in Bali','updated':1})
        self.store.put('sessions/inbox',{'current':'item-b'})
        self.store.classifier = lambda text,context:{'kind':'question','value':None}
        reply = self.say(12,'покажи ссылки')
        self.assertIn('https://t.me/x/1',reply)

    def test_failed_current_draft_newer_than_the_post_still_shows_the_post_links(self):
        self.published('uniqlo','item-u',title='Uniqlo',at=5)
        self.store.put('items/item-b',{'status':'failed','revision':3,'messages':[],'slug':'item-b','title':'Life in Bali','updated':100})
        self.store.put('sessions/inbox',{'current':'item-b'})
        self.store.classifier = lambda text,context:{'kind':'question','value':None}
        self.assertIn('https://t.me/x/1',self.say(15,'покажи ссылки'))

    def test_reply_to_an_unregistered_bot_message_with_a_fixable_post_current_still_fixes(self):
        self.published('album','item-1',state='done',spec={'targets':['tg-channel'],'copy':{'tg-channel':'x'}})
        self.store.put('sessions/inbox',{'current':'album'})
        self.say(10,'убери хэштеги',reply_to_message={'message_id':77,'text':'Бот','from':{'is_bot':True}})
        self.assertIsNone(self.store.get('outbox/reply-10'))
        self.assertIsNotNone(self.store.get('outbox/fix-10'))

    def test_reply_to_a_human_message_is_not_routed_to_the_reply_model(self):
        self.store.put('sessions/inbox',{})
        self.say(11,'привет',reply_to_message={'message_id':77,'text':'Люди','from':{'is_bot':False}})
        self.assertIsNone(self.store.get('outbox/reply-11'))

    def test_typed_accept_on_a_scheduled_post_still_accepts_the_previous_one(self):
        self.published('old','item-old',state='done')
        self.store.put('items/item-1',{'status':'previewed','revision':1,'messages':[],'slug':'album','title':'Новый'})
        self.store.put('specs/album',{'state':'approved','item':'item-1','spec':{'targets':['tg-channel'],'at':'2999-01-01T00:00:00+00:00'},'results':{},'errors':{}})
        self.store.put('sessions/inbox',{'current':'item-1'})
        self.say(13,'принять')
        self.assertTrue(self.store.get('specs/old').get('accepted'))

    def test_draft_failing_after_another_post_published_is_explained_not_the_post(self):
        self.published('uniqlo','item-u',title='Uniqlo',at=100)
        self.store.put('items/item-b',{'status':'failed','revision':3,'messages':[],'slug':'item-b','title':'Life in Bali','updated':1,'failed_at':200})
        self.store.put('sessions/item-b',{'state':'open','steps':[{'id':'draft','label':'Черновик','state':'failed','error':'Провайдер не ответил'}]})
        self.store.put('sessions/inbox',{'current':'item-b'})
        self.store.classifier = lambda text,context:{'kind':'question','value':None}
        self.assertIn('Life in Bali',self.say(16,'почему завис'))

    def test_reply_failure_asks_to_repeat_the_question(self):
        class Request:
            data = {'step':'reply','slug':'album','text':'?'}
        self.published('album','item-1',state='done')
        with patch.object(app,'runtime',return_value=(self.config,self.store,None)), patch.object(app,'activity'), patch.object(app,'sync_pin'), patch.object(app,'tell') as tell, patch.object(app,'reply_note',side_effect=RuntimeError):
            app.handle_worker(Request)
        self.assertIn('Не смог ответить, повтори вопрос.',tell.call_args.args[1])
        self.assertNotIn('Операция остановлена',tell.call_args.args[1])

    def test_empty_model_reply_never_claims_a_record(self):
        self.published('album','item-1',state='done')
        sent = []
        with patch.object(app,'tell',side_effect=lambda config,text,**extra:sent.append(text)), patch.object(app.drafter,'converse',return_value=''):
            app.reply_note(self.config,self.store,{'slug':'album','text':'?','quoted':'x'})
        self.assertNotIn('Записал',sent[0])

    def test_stopped_draft_explanation_is_russian_for_a_provider_error(self):
        self.store.put('items/item-b',{'status':'failed','revision':3,'messages':[],'slug':'item-b','title':'Life in Bali'})
        self.store.put('sessions/item-b',{'state':'open','steps':[{'id':'draft','label':'Черновик','state':'failed','error':'Provider rejected request: 529'}]})
        self.store.put('sessions/inbox',{'current':'item-b'})
        self.store.classifier = lambda text,context:{'kind':'question','value':None}
        reply = self.say(14,'почему остановился?')
        self.assertNotIn('Provider',reply)
        self.assertIn('Life in Bali',reply)

    def test_failed_step_error_is_part_of_the_explanation(self):
        self.store.put('items/item-b',{'status':'failed','revision':3,'messages':[],'slug':'item-b','title':'Life in Bali'})
        self.store.put('sessions/item-b',{'state':'open','steps':[{'id':'draft','label':'Черновик','state':'failed','error':'Провайдер не ответил'}]})
        self.store.put('sessions/inbox',{'current':'item-b'})
        self.store.classifier = lambda text,context:{'kind':'question','value':None}
        self.assertIn('Провайдер не ответил',self.say(9,'почему остановился?'))

    def test_token_failure_on_an_unknown_target_does_not_print_the_raw_id(self):
        text = app.failure_text(app.Error('API failure: 190'),'blog','x')
        self.assertTrue(text.startswith('Нужен новый токен'))
        self.assertNotIn('blog',text)
        self.assertEqual(app.failure_text(app.Error('API failure: 190'),'threads','x'),'Нужен новый токен Threads.')


if __name__ == '__main__':
    unittest.main()
