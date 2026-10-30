from pathlib import Path
import json
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

    def test_retry_button_after_token_renewal_resets_the_token_target(self):
        self.published('album','item-1',state='partial',errors={'threads':'Нужен новый токен Threads.'},attempts={'threads':3},hash='abcdefgh')
        self.store.intake({'update_id':2,'callback_query':{'id':'2','data':'retry:album:abcdefgh'}},self.config)
        saved = self.store.get('specs/album')
        self.assertEqual((saved['state'],saved['attempts']),('approved',{}))

    def test_typed_accept_question_never_accepts_an_older_post_while_the_current_one_publishes(self):
        self.published('old','item-old',state='done')
        self.store.put('items/item-1',{'status':'approved','revision':1,'messages':[],'slug':'album','title':'Новый'})
        self.store.put('specs/album',{'state':'running','item':'item-1','spec':{'targets':['tg-channel']},'results':{},'errors':{}})
        self.store.put('sessions/inbox',{'current':'item-1'})
        self.store.classifier = lambda text,context:{'kind':'question','value':None}
        self.say(3,'принять')
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

    def test_complaints_while_drafting_are_edits_and_only_nudges_get_one_card(self):
        self.store.put('items/item-9',{'status':'drafting','revision':8,'slug':'item-9','messages':[{'message_id':1,'text':'Тема'}]})
        self.store.put('sessions/inbox',{'current':'item-9'})
        self.store.classifier = lambda text,context:{'kind':'instruction'}
        for number,text in enumerate(('Плохо оч плохо','Зачем ты дублировал?'),1):
            self.assertNotIn('идёт',str(self.say(number,text)))
        self.store.classifier = lambda text,context:{'kind':'question'}
        self.assertIn('идёт',self.say(3,'ну?')['text'])
        self.assertFalse(self.say(4,'долго?'))

    def test_status_card_of_a_working_draft_never_offers_an_older_posts_table(self):
        self.published('old','item-old',state='done',hash='abcdefgh')
        self.store.put('items/item-9',{'status':'drafting','revision':3,'slug':'item-9','created':50,'messages':[{'message_id':1,'text':'Тема'}]})
        self.store.put('sessions/inbox',{'current':'item-9'})
        self.store.classifier = lambda text,context:{'kind':'question'}
        card = self.say(1,'как там?')
        self.assertNotIn('tbl:',str(card))
        self.assertEqual(self.store.intake({'update_id':2,'callback_query':{'id':'2','data':'tbs:old:abcdefgh'}},self.config),'Эта таблица от прошлого поста.')
        self.assertIsNone(self.store.get('outbox/table-' + __import__('hashlib').sha256(b'2').hexdigest()))
        self.assertTrue(self.store.intake({'update_id':3,'callback_query':{'id':'3','data':'tbl:old:abcdefgh'}},self.config))
        self.store.change('items/item-9',lambda value:value.update(status='rejected'))
        self.assertTrue(self.store.intake({'update_id':4,'callback_query':{'id':'4','data':'tbs:old:abcdefgh'}},self.config))

    def test_learn_order_stores_the_rule_from_recent_messages_never_the_command(self):
        self.store.put('items/item-9',{'status':'previewed','revision':8,'slug':'item-9','messages':[],'edits':['На рилс обложке надо делать надписи с блюром градиентом']})
        self.store.put('sessions/inbox',{'current':'item-9'})
        self.store.classifier = lambda text,context:{'kind':'rule','value':'Выучи скилл создания обложек'}
        reply = self.say(3,'Выучи скилл создания обложек')
        self.assertNotIn('Правило записал',reply)
        text = self.store.get('outbox/skills-3')['payload']['text']
        self.assertIn('блюром градиентом',text)
        self.store.put('items/item-9',{'status':'previewed','revision':9,'slug':'item-9','messages':[]})
        self.assertEqual(self.say(4,'Выучи скилл создания обложек'),'Что именно записать в навыки?')
        self.assertIsNone(self.store.get('outbox/skills-4'))

    def test_reference_photos_route_to_refs_only_when_clearly_a_style_reference(self):
        def album(number, caption, group=None):
            return self.store.intake({'update_id':number,'message':{'message_id':number,'caption':caption,'media_group_id':group or str(number),'photo':[{'file_id':'f' + str(number),'file_size':10}]}},self.config)
        album(1,'Вот тебе рефы как обложки делать')
        self.assertEqual(self.store.get('outbox/refs-1')['payload']['photo']['message_id'],1)
        self.assertIsNone(self.store.get('items/item-1'))
        self.store.intake({'update_id':2,'message':{'message_id':2,'media_group_id':'1','photo':[{'file_id':'f2','file_size':10}]}},self.config)
        self.assertIsNotNone(self.store.get('outbox/refs-2'))
        album(3,'Рефлексия о стиле жизни')
        self.assertIsNone(self.store.get('outbox/refs-3'))
        self.store.put('albums/9',{'item':'item-7','at':1})
        album(4,'рефы обложка','9')
        self.assertIsNone(self.store.get('outbox/refs-4'))
        self.store.put('sessions/inbox',{'collecting':'item-8'})
        self.store.put('items/item-8',{'status':'collecting','revision':1,'slug':'item-8','messages':[],'batch_token':'t'})
        album(5,'рефы обложка')
        self.assertIsNone(self.store.get('outbox/refs-5'))
        self.store.put('sessions/inbox',{})
        self.store.intake({'update_id':6,'message':{'message_id':6,'media_group_id':'7','photo':[{'file_id':'f6','file_size':10}]}},self.config)
        self.assertEqual(self.store.get('sessions/inbox')['collecting'],'item-6')
        album(7,'Вот рефы как обложки делать','7')
        self.assertEqual(self.store.get('outbox/refs-6')['payload']['photo']['message_id'],6)
        self.assertEqual(self.store.get('outbox/refs-7')['payload']['photo']['message_id'],7)
        self.assertEqual(self.store.get('items/item-6')['status'],'rejected')
        self.assertNotIn('collecting',self.store.get('sessions/inbox'))

    def test_rule_from_the_draft_model_can_only_add_examples_or_append_lines(self):
        body = 'Cover headline text uses a purple gradient blur.'
        self.store.put('skills/old',{'title':'Old','body':'x'*30,'examples':[],'revision':1})
        self.store.put('skills/covers',{'title':'Обложки','body':body,'examples':[],'revision':1})
        def apply(ops):
            with patch.object(app.drafter,'converse',return_value=json.dumps({'ops':ops,'reply':'ok'})), patch.object(app,'tell'):
                app.apply_rule(self.config,self.store,{'text':'rule'},app.tell)
        apply([{'op':'delete','id':'old'},{'op':'set','id':'fresh','title':'Новый','body':'A brand new skill body over twenty chars.'}])
        self.assertIsNotNone(self.store.get('skills/old'))
        self.assertIsNone(self.store.get('skills/fresh'))
        apply([{'op':'set','id':'covers','title':'Обложки','body':'Totally different replacement body for the cover skill.'}])
        self.assertEqual(self.store.get('skills/covers')['body'],body)
        apply([{'op':'set','id':'covers','title':'Обложки','body':body + '\n- Headline sits in the upper third.'}])
        self.assertIn('upper third',self.store.get('skills/covers')['body'])

    def test_clips_lead_every_target_unless_photos_are_asked_first(self):
        photos = [(5,0),(6,1)]
        self.assertEqual([k for _,k,_ in app.clip_order(photos,['9'],[])],['clip','image','image'])
        self.assertEqual([k for _,k,_ in app.clip_order(photos,['9'],['Фото надо первым'])],['image','image','clip'])
        for edit in ('сначала фото, потом видео','фото перед видео','первым фото'):
            self.assertEqual([k for _,k,_ in app.clip_order(photos,['9'],[edit])],['image','image','clip'])
        self.assertEqual([k for _,k,_ in app.clip_order(photos,['9'],['сначала фото, потом видео','сначала видео, потом фото'])],['clip','image','image'])
        self.assertEqual([k for _,k,_ in app.clip_order(photos,['9'],['видео первым','фото перед видео'])],['image','image','clip'])
        self.assertEqual([k for _,k,_ in app.clip_order(photos,['9'],['сначала фото, потом видео','короче текст'])],['image','image','clip'])
        for edit in ('сначала фото обрежь','первое фото убери','первую картинку замени','сначала убери фото'):
            self.assertEqual([k for _,k,_ in app.clip_order(photos,['9'],[edit])],['clip','image','image'])

    def test_closing_question_material_ignores_complaints_and_keeps_requests(self):
        messages = [{'text':'Завтрак'}]
        self.assertNotIn('Вы бы',app.drafter.closing_material(messages,['Вы бы совмещали? - ужасно, не пиши такое']))
        self.assertIn('Закончи словами: Как вам?',app.drafter.closing_material(messages,['Закончи словами: Как вам?']))
        self.assertIn('Добавь вопрос в конце: Вы бы пошли?',app.drafter.closing_material(messages,['Добавь вопрос в конце: Вы бы пошли?']))

    def test_reel_cover_text_is_a_clean_short_headline(self):
        text = app.reel_cover_text({'copy':{'ig-reel':'\U0001f305 Завтрак {в} Arctic с сауной и бассейном на крыше\n#bali'}})
        self.assertTrue(1 <= len(text) <= 40 and not any(c in text for c in '{}\\\U0001f305'))
        self.assertEqual(app.reel_cover_text({'copy':{}}),'')

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
        self.assertIn('Life in Bali',reply["text"])
        self.assertIn('Черновик',reply["text"])
        self.assertIn('https://t.me/x/1',reply["text"])

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
        self.assertIn('https://t.me/x/1',self.say(15,'покажи ссылки')["text"])

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

    def test_draft_failing_after_another_post_published_is_explained_not_the_post(self):
        self.published('uniqlo','item-u',title='Uniqlo',at=100)
        self.store.put('items/item-b',{'status':'failed','revision':3,'messages':[],'slug':'item-b','title':'Life in Bali','updated':1,'failed_at':200})
        self.store.put('sessions/item-b',{'state':'open','steps':[{'id':'draft','label':'Черновик','state':'failed','error':'Провайдер не ответил'}]})
        self.store.put('sessions/inbox',{'current':'item-b'})
        self.store.classifier = lambda text,context:{'kind':'question','value':None}
        self.assertIn('Life in Bali',self.say(16,'почему завис')["text"])

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
        self.assertNotIn('Provider',reply["text"])
        self.assertIn('Life in Bali',reply["text"])

    def test_failed_step_error_is_part_of_the_explanation(self):
        self.store.put('items/item-b',{'status':'failed','revision':3,'messages':[],'slug':'item-b','title':'Life in Bali'})
        self.store.put('sessions/item-b',{'state':'open','steps':[{'id':'draft','label':'Черновик','state':'failed','error':'Провайдер не ответил'}]})
        self.store.put('sessions/inbox',{'current':'item-b'})
        self.store.classifier = lambda text,context:{'kind':'question','value':None}
        self.assertIn('Провайдер не ответил',self.say(9,'почему остановился?')["text"])

    def test_token_failure_on_an_unknown_target_does_not_print_the_raw_id(self):
        text = app.failure_text(app.Error('API failure: 190'),'blog','x')
        self.assertTrue(text.startswith('Нужен новый токен'))
        self.assertNotIn('blog',text)
        self.assertEqual(app.failure_text(app.Error('API failure: 190'),'threads','x'),'Нужен новый токен Threads.')


if __name__ == '__main__':
    unittest.main()
