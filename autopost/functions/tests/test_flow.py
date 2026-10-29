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
from store import Store, newest_states, pin_stage
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

    def test_draft_claim_never_repeats_running_or_old_revision(self):
        self.store.put('items/job',{'status':'queued','revision':2})
        self.assertFalse(self.store.claim_item('job',1))
        self.assertTrue(self.store.claim_item('job',2))
        self.assertFalse(self.store.claim_item('job',2))

    def test_draft_validation_returns_malformed_model_media_instead_of_crashing(self):
        self.store.put('items/item-2',{'status':'queued','revision':1,'slug':'example','messages':[]})
        seen = []
        def run_draft(config,store,item,revision,source,images,clips,validate,rec,live):
            seen.append(validate({**self.spec,'media':{'clips':True}}))
            return {'answer':'x'}
        with patch.object(app.media,'intake',return_value=[]), patch.object(app,'edit_media',return_value=([],[])), patch.object(app,'restrict',return_value=None), patch.object(app.platforms,'request',return_value=[]), patch.object(app.agents,'run_draft',side_effect=run_draft), patch.object(app,'superseded',return_value=True):
            app.draft(self.config,self.store,None,{'item':'item-2','revision':1},self.root)
        self.assertIn('Unknown media fields',seen[0])

    def test_worker_failure_fails_only_the_current_drafting_revision(self):
        class Request:
            data = {'step':'draft','item':'item-1','revision':1}
        for stored,expected in (({'status':'drafting','revision':1},'failed'),({'status':'drafting','revision':2},'drafting'),({'status':'collecting','revision':1},'collecting')):
            self.store.put('items/item-1',{**stored,'messages':[]})
            with patch.object(app,'runtime',return_value=(self.config,self.store,None)), patch.object(app,'activity'), patch.object(app,'sync_pin'), patch.object(app,'tell') as tell, patch.object(app,'draft',side_effect=RuntimeError):
                app.handle_worker(Request)
            self.assertEqual(self.store.get('items/item-1')['status'],expected)
            self.assertEqual(tell.call_count,1 if expected == 'failed' else 0)

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
        self.assertEqual(self.store.get('outbox/again-60')['payload'],{'item':'item-1','step':'montage','revision':1})

    def test_scheduled_post_is_not_running_media_starts_a_new_post_and_text_names_the_time(self):
        self.store.put('specs/example',{**self.store.get('specs/example'),'state':'approved','spec':{**self.spec,'at':'2099-01-01T10:00:00+00:00'}})
        self.store.put('sessions/inbox',{'current':'example'})
        reply = self.store.intake({'update_id':50,'message':{'message_id':50,'text':'ещё'}},self.config)
        self.assertIn('запланирован на 01.01 18:00',reply)
        started = self.store.intake({'update_id':51,'message':{'message_id':51,'photo':[{'file_id':'f','file_size':10}]}},self.config)
        self.assertEqual(started['item'],'item-51')

    def test_video_only_post_reaches_instagram(self):
        self.store.put('items/item-3',{'status':'drafting','revision':1,'chosen':['ig-post','ig-reel'],'messages':[]})
        config = {'channels':{'tg-channel':1},'instagram':{'user_id':1}}
        allowed = app.restrict(config,self.store,{'item':'item-3'},self.store.get('items/item-3'),False,True)
        self.assertEqual(allowed,['ig-post','ig-reel'])
        self.assertEqual(app.reeled(allowed),['ig-reel'])
        self.assertEqual(self.store.get('items/item-3')['skipped'],[])

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

    def test_nudge_during_drafting_gets_status_without_superseding_the_draft(self):
        self.store.put('items/item-9',{'status':'drafting','revision':8,'slug':'item-9','messages':[{'message_id':1,'text':'Тема'}]})
        self.store.put('sessions/inbox',{'current':'item-9'})
        self.store.put('sessions/item-9',{'state':'open','steps':[{'id':'draft','label':'Черновик','state':'running','since':time.time() - 200,'agents':{'places':{'status':'running','action':'ищу'},'media':{'status':'done'},'copy':{'status':'waiting'}}}]})
        reply = self.store.intake({'update_id':90,'message':{'message_id':90,'text':'ну'}},self.config)
        self.assertEqual(reply,'Черновик пишется 3 мин: места — ищу, медиа — готово, текст — ждёт.')
        item = self.store.get('items/item-9')
        self.assertEqual(item['revision'],8)
        self.assertNotIn('followup',item)
        self.assertIsNone(self.store.get('outbox/edit-90'))

    def drafting_with(self, kind, number, value='короче'):
        self.store.put('items/item-9',{'status':'drafting','revision':8,'slug':'item-9','messages':[{'message_id':1,'text':'Тема'}]})
        self.store.put('sessions/inbox',{'current':'item-9'})
        self.store.put('sessions/item-9',{'state':'open','steps':[{'id':'draft','label':'Черновик','state':'running','since':time.time(),'agents':{'copy':{'status':'running','action':'пишу'}}}]})
        def classify(text,context):
            if kind is None:
                raise RuntimeError
            return {'kind':kind,'value':value}
        self.store.classifier = classify
        return self.store.intake({'update_id':number,'message':{'message_id':number,'text':'сделай текст короче'}},self.config)

    def test_classifier_fix_and_failure_during_drafting_edit_but_question_gets_status(self):
        for number,kind in ((91,'fix'),(92,None)):
            self.assertIsNone(self.drafting_with(kind,number))
            item = self.store.get('items/item-9')
            self.assertEqual((item['revision'],item['edits']),(9,['сделай текст короче']))
        self.setUp()
        reply = self.drafting_with('question',93)
        self.assertIn('текст — пишу',reply)
        self.assertEqual(self.store.get('items/item-9')['revision'],8)


class PublishedMessageTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(FakeFirestore())
        self.config = {'roman_chat_id':1}
        self.store.put('items/item-1',{'status':'previewed','revision':1,'messages':[{'message_id':1,'text':'Тема'}],'slug':'album'})
        self.store.put('sessions/inbox',{'current':'album'})

    def say(self, identifier, text):
        return self.store.intake({'update_id':identifier,'message':{'message_id':identifier,'text':text}},self.config)

    def test_typed_accept_accepts_the_latest_published_post(self):
        self.store.put('specs/album',{'state':'partial','item':'item-1','published_at':5,'spec':{'targets':['tg-channel','threads']},'results':{'tg-channel':{'url':'u','at':1}},'errors':{'threads':'x'}})
        self.assertEqual(self.say(14,'Принять'),'')
        self.assertTrue(self.store.get('specs/album')['accepted'])

    def test_question_on_a_previewed_draft_is_kept_without_a_read_after_write(self):
        self.store.put('items/item-2',{'status':'previewed','revision':2,'messages':[{'message_id':1,'text':'x'}],'slug':'item-2'})
        self.store.put('specs/item-2',{'state':'previewed','item':'item-2','spec':{'targets':['tg-channel']},'hash':'abcdefgh'})
        self.store.put('sessions/inbox',{'current':'item-2'})
        self.store.classifier = lambda text,context:{'kind':'question','value':None}
        self.assertEqual(self.say(15,'ты поправил? покажи'),'')
        self.assertTrue(self.store.get('followups/15')['answered'])

    def test_repeated_publishing_status_is_sent_once(self):
        self.store.put('specs/album',{'state':'running','item':'item-1','spec':{'targets':['ig-story','tg-story']},'results':{'ig-story':{}},'errors':{}})
        self.assertIn('готово 1 из 2',self.say(11,'ну как?'))
        self.assertIsNone(self.say(12,'и что?'))

    def test_question_while_publishing_shows_the_current_post_not_the_previous(self):
        self.store.put('items/old',{'status':'done','revision':1,'messages':[],'slug':'old','title':'Старый'})
        self.store.put('specs/old',{'state':'done','item':'old','published_at':99,'spec':{'targets':['tg-channel']},'results':{'tg-channel':{'url':'https://t.me/old/1','at':1}}})
        self.store.put('specs/album',{'state':'running','item':'item-1','spec':{'targets':['tg-channel']},'results':{},'errors':{}})
        self.store.put('items/album',{'status':'approved','revision':1,'messages':[],'slug':'album'})
        self.store.classifier = lambda text,context:{'kind':'question','value':None}
        reply = self.say(13,'покажи ссылки')
        self.assertNotIn('t.me/old',reply)
        self.assertIn('Пост',reply)

    def test_message_during_publishing_gets_status_not_the_post_hint(self):
        self.store.put('specs/album',{'state':'running','item':'item-1','spec':{'targets':['ig-story','tg-story']},'results':{'ig-story':{}},'errors':{'tg-story':'API failure: 400'}})
        reply = self.say(10,'ну как, опубликовал?')
        self.assertIn('готово 1 из 2',reply)
        self.assertIn('API failure: 400',reply)
        self.assertIsNone(self.store.get('outbox/fix-10'))

    def test_message_after_publishing_goes_to_the_model_once(self):
        self.store.put('specs/album',{'state':'partial','item':'item-1','spec':{'targets':['tg-channel'],'copy':{'tg-channel':'x'}},'results':{},'errors':{}})
        first = self.say(10,'почему ошибка?')
        self.assertIn('Читаю по посту',first)
        self.assertIsNotNone(self.store.get('outbox/fix-10'))
        self.assertEqual(self.store.get('specs/album')['state'],'partial')
        self.assertEqual(self.say(10,'почему ошибка?'),first)


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

    def test_album_while_drafting_opens_a_new_post(self):
        self.store.put('specs/album',{'state':'drafting','item':'item-1','spec':{'targets':['tg-channel']},'results':{},'errors':{}})
        for n in range(3):
            self.store.intake({'update_id':10 + n,'message':{'message_id':10 + n,'media_group_id':'g1','caption':'Новый','photo':[{'file_id':'f' + str(n),'file_size':10}]}},self.config)
        self.assertEqual(self.store.get('sessions/inbox')['collecting'],'item-10')
        self.assertEqual(self.store.get('items/item-1').get('edits',[]),[])

    def test_bot_forward_is_ignored(self):
        config = {'roman_chat_id':1,'telegram_bot_id':'55'}
        reply = self.store.intake({'update_id':10,'message':{'message_id':10,'photo':[{'file_id':'f','file_size':10}],'forward_origin':{'type':'user','sender_user':{'id':55}}}},config)
        self.assertIn('пропускаю',reply)
        self.assertIsNone(self.store.get('sessions/inbox').get('collecting'))

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

    def test_album_sent_to_a_draft_opens_one_new_post_with_every_photo(self):
        self.store.put('specs/album',{'state':'previewed','item':'item-1','spec':{'targets':['tg-channel']},'results':{},'errors':{}})
        self.album(10)
        self.assertEqual(self.store.get('items/item-1').get('edits',[]),[])
        self.assertEqual([m['message_id'] for m in self.store.get('items/item-10')['messages']],[10,11,12])


class QuestionToDraftTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(FakeFirestore())
        self.config = {'roman_chat_id':1}
        self.store.put('items/item-1',{'status':'previewed','revision':1,'messages':[{'message_id':1,'text':'Тема'}],'slug':'album'})
        self.store.put('sessions/inbox',{'current':'album'})
        self.store.put('specs/album',{'state':'previewed','item':'item-1','spec':{'slug':'album','targets':['tg-channel'],'copy':{'tg-channel':'x'}},'assets':{},'preview_message':5,'hash':'abcdefgh'})

    def ask(self, text):
        return self.store.intake({'update_id':20,'message':{'message_id':20,'text':text}},self.config)

    def test_question_gets_exactly_one_message_the_answer_with_the_new_post_button(self):
        self.assertIsNone(self.ask('какая модель?'))
        self.assertEqual(self.store.get('items/item-1').get('edits',[]),[])
        sent = []
        with patch.object(app,'tell',side_effect=lambda config,text,**extra:sent.append((text,extra)) or {'message_id':50}), patch.object(app.drafter,'converse',return_value='{"answer":"Модель Claude Sonnet 5.5"}'):
            app.question(self.config,self.store,{'slug':'album','item':'item-1','text':'какая модель?'})
        self.assertEqual([text for text,_ in sent],['Черновик «x»: Модель Claude Sonnet 5.5'])
        self.assertEqual([row[0]['text'] for row in sent[0][1]['reply_markup']['inline_keyboard'][-2:]],['Это новый пост','Отменить черновик'])
        self.assertEqual(self.store.get('specs/album')['state'],'previewed')
        data = sent[0][1]['reply_markup']['inline_keyboard'][-2][0]['callback_data']
        self.store.intake({'update_id':21,'callback_query':{'id':'21','data':data}},self.config)
        item = self.store.get('items/item-1')
        self.assertEqual((item['status'],item['edits'] if 'edits' in item else []),('previewed',[]))
        self.assertEqual(self.store.get('items/item-20')['messages'][0]['message_id'],20)

    def test_reply_question_writes_its_own_followup(self):
        self.store.put('published-messages/5',{'slug':'album'})
        self.store.intake({'update_id':20,'message':{'message_id':20,'text':'почему так?','reply_to_message':{'message_id':5,'text':'Черновик'}}},self.config)
        self.assertEqual(self.store.get('items/item-1')['followup'],'20')
        self.assertTrue(self.store.get('followups/20')['answered'])
        self.assertEqual(self.store.get('items/item-1').get('edits',[]),[])

    def test_model_cancel_is_a_rejection_with_learning(self):
        payload = {'item':'item-1','revision':1,'slug':'album','text':'отмени'}
        self.store.change('items/item-1',lambda value:value.update(status_message={'id':9,'text':'Монтирую…','line':'Принял'}))
        with patch.object(app,'tell') as tell:
            app.take_answer(self.config,self.store,payload,self.store.get('items/item-1'),{'action':'cancel'})
        self.assertEqual(self.store.get('specs/album')['state'],'rejected')
        self.assertIn('отклонён',tell.call_args.args[1])
        self.assertEqual([snapshot.to_dict()['payload']['step'] for snapshot in self.store.db.collection('outbox').stream() if snapshot.to_dict()['payload']['step'] == 'learn'],['learn'])
        finish = [snapshot.to_dict()['payload'] for snapshot in self.store.db.collection('outbox').stream() if snapshot.to_dict()['payload']['step'] == 'finish-status']
        self.assertEqual([(p['status']['id'],p['line']) for p in finish],[(9,'Отклонено.')])
        self.assertNotIn('status_message',self.store.get('items/item-1'))

    def test_a_question_shaped_request_becomes_a_normal_edit(self):
        payload = {'item':'item-1','revision':1,'slug':'album','text':'Сделаешь короче?'}
        app.take_answer(self.config,self.store,payload,self.store.get('items/item-1'),{'action':'edit'})
        self.assertEqual(self.store.get('items/item-1')['edits'],['Сделаешь короче?'])
        self.assertEqual(self.store.get('specs/album')['state'],'drafting')

    def test_edit_line_carries_the_new_post_button(self):
        self.assertIsNone(self.ask('локация джакарта'))
        item = self.store.get('items/item-1')
        self.assertEqual(app.newp_markup(self.store,'item-1')[0]['text'],'Это новый пост')
        self.assertTrue(app.feedback(self.store.get('specs/album'),item,item['edits'][-1]).startswith('Правлю'))


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

    def test_overdue_running_spec_records_reasons_and_resumes_until_attempts_run_out(self):
        self.store.change('specs/example',lambda value:value.update(state='running',started_at=0,attempts={'tg-channel':1}))
        with patch.object(app,'tell') as tell, patch.object(app,'enqueue') as enqueue, patch.object(app,'sync_pin'):
            app.recover_running(self.config,self.store)
            saved = self.store.get('specs/example')
            self.assertEqual((saved['state'],saved['errors']),('running',{'tg-channel':'Прервано по таймауту, повторю.','threads':'Не начато.'}))
            self.assertGreater(saved['started_at'],0)
            enqueue.assert_called_once()
            tell.assert_not_called()
            self.store.change('specs/example',lambda value:value.update(started_at=0,attempts={'tg-channel':3,'threads':3}))
            app.recover_running(self.config,self.store)
        saved = self.store.get('specs/example')
        self.assertEqual(saved['state'],'partial')
        self.assertTrue(all(saved['errors'].get(target) for target in self.spec['targets']))
        enqueue.assert_called_once()
        tell.assert_called_once()
        self.assertIn('Telegram-канал — прервано',tell.call_args[0][1])
        self.assertNotIn('повторю',tell.call_args[0][1])

    def test_nothing_published_skips_learning_and_says_so(self):
        self.store.change('specs/example',lambda value:value.update(attempts={'tg-channel':3,'threads':3},errors={'tg-channel':'x','threads':'y'}))
        with patch.object(app,'tell',return_value={'message_id':9}), patch.object(app,'finish_status') as status, patch.object(app.learning,'schedule') as schedule, patch.object(app,'remove_keyboard'):
            app.run(self.config,self.store,None,'example',self.root)
        schedule.assert_not_called()
        self.assertEqual(status.call_args[0][2],'Ничего не опубликовано.')

    def test_overdue_operation_spec_is_announced_not_resumed(self):
        self.store.change('specs/example',lambda value:value.update(state='running',started_at=0,operation='fix',attempts={}))
        with patch.object(app,'tell') as tell, patch.object(app,'enqueue') as enqueue, patch.object(app,'sync_pin'):
            app.recover_running(self.config,self.store)
        enqueue.assert_not_called()
        tell.assert_called_once()
        self.assertEqual(self.store.get('specs/example')['state'],'partial')

    def test_retry_gives_unfinished_targets_fresh_attempts(self):
        result = {'url':'https://t.me/example/1','verified':True}
        self.store.change('specs/example',lambda value:value.update(state='partial',results={'tg-channel':result},errors={'threads':'x'},attempts={'tg-channel':1,'threads':3}))
        self.store.intake({'update_id':1,'callback_query':{'id':'1','data':'retry:example:' + self.digest[:8]}},self.config)
        saved = self.store.get('specs/example')
        self.assertEqual((saved['state'],saved['attempts']),('approved',{'tg-channel':1}))

    def test_accept_ends_a_partial_post_as_done_with_the_failure_kept(self):
        result = {'url':'https://t.me/example/1','verified':True}
        self.store.change('specs/example',lambda value:value.update(state='partial',results={'tg-channel':result},errors={'threads':'x'},attempts={'tg-channel':1,'threads':3}))
        self.store.intake({'update_id':1,'callback_query':{'id':'1','data':'acc:example:' + self.digest[:8]}},self.config)
        saved = self.store.get('specs/example')
        self.assertEqual((saved['state'],saved['accepted'],saved['errors']),('done',True,{'threads':'x'}))

    def test_accept_is_refused_when_nothing_was_published(self):
        self.store.change('specs/example',lambda value:value.update(state='partial',results={},errors={'threads':'x'}))
        self.store.intake({'update_id':1,'callback_query':{'id':'1','data':'acc:example:' + self.digest[:8]}},self.config)
        self.assertEqual(self.store.get('specs/example')['state'],'partial')

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


class BatchedPollingTests(unittest.TestCase):
    def test_containers_are_polled_together_each_round(self):
        rounds = {}
        def fake(method,url,fields=None,token=None,**rest):
            rounds[url] = rounds.get(url,0) + 1
            return {'status_code':'FINISHED' if rounds[url] >= 2 else 'IN_PROGRESS'}
        with patch.object(p,'request',fake), patch.object(p.time,'sleep') as sleep:
            p.ready_all(('tok','1','https://graph'),['11','12','13'],True,lambda position:None)
        self.assertEqual(sorted(rounds.values()),[2,2,2])
        sleep.assert_called_once_with(30)


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

    def test_a_long_copy_continues_without_a_separator_and_keeps_its_targets(self):
        parts = app.pieces('<b>h</b>','x'*7000,None,['tg-channel']) + app.pieces('<b>k</b>','y',None,['threads'])
        pages = app.pages(parts)
        self.assertEqual(sum(text.count('──────────') for text,_ in pages),1)
        self.assertTrue(all('tg-channel' in members for text,members in pages if 'x' in text))
        self.assertTrue(all('threads' not in members for text,members in pages if 'x' in text and 'y' not in text))

    def test_a_remount_is_acknowledged_once_in_a_status_message(self):
        item = {'slug':'album','edits':['короче'],'edits_applied':0,'messages':[]}
        self.store.put('items/item-1',item)
        with patch.object(app,'tell',return_value={'message_id':9}) as tell:
            app.acknowledge_remount(self.config,self.store,{'item':'item-1'},item)
            app.acknowledge_remount(self.config,self.store,{'item':'item-1'},item)
        self.assertEqual(tell.call_count,1)
        self.assertEqual(self.store.get('items/item-1')['status_message']['id'],9)

    def test_unreplied_text_to_a_previewed_draft_is_the_edit_at_once(self):
        self.preview()
        self.store.put('sessions/inbox',{'current':'album'})
        reply = self.message(6,text='сделай короче')
        self.assertIsNone(reply)
        self.assertEqual(self.store.get('specs/album')['state'],'drafting')
        self.assertEqual(self.store.get('items/item-1')['edits'],['сделай короче'])
        self.assertIsNone(self.store.get('sessions/inbox').get('awaiting'))

    def test_edit_queued_during_preview_wins_and_the_stale_keyboard_is_removed(self):
        sent = self.preview(self.queue_edit)
        saved = self.store.get('specs/album')
        self.assertEqual((saved['state'],saved['hash']),('drafting',''))
        item = self.store.get('items/item-1')
        self.assertEqual((item['status'],item['revision']),('queued',2))
        self.assertIsNotNone(self.store.get('outbox/edit-e1'))
        self.assertEqual(sent[-1][0],'editMessageText')
        self.assertEqual((sent[-1][1]['text'],sent[-1][1]['reply_markup']),('Черновик отменён.',{'inline_keyboard':[]}))
        self.assertNotIn('current',self.store.get('sessions/inbox') or {})

    def test_a_video_file_sent_to_a_previewed_draft_joins_it_and_requeues_the_montage(self):
        self.preview()
        self.store.put('sessions/inbox',{'current':'album'})
        reply = self.message(5,document={'file_id':'f','file_size':1000,'mime_type':'video/quicktime'},caption='так делай')
        item = self.store.get('items/item-1')
        self.assertEqual((item['status'],item['montage'],len(item['messages']),item['edits']),('queued',True,2,['так делай [добавлен клип]']))
        self.assertEqual(self.store.get('specs/album')['state'],'drafting')
        self.assertTrue(reply.startswith('Добавил в черновик'))

    def test_a_draft_waiting_for_an_upload_gets_a_fresh_link_at_the_new_revision(self):
        self.store.put('items/item-1',{'status':'queued','revision':1,'waiting_upload':True,'messages':[{'message_id':1,'video':{'file_id':'big','file_size':30*1024*1024}}],'slug':'album'})
        self.store.put('sessions/inbox',{'current':'album'})
        self.store.put('specs/album',{'item':'item-1','state':'previewed','spec':self.spec,'hash':'abcdefgh'})
        self.message(6,video={'file_id':'small','file_size':1000})
        item = self.store.get('items/item-1')
        self.assertEqual((item['revision'],item['waiting_upload']),(2,True))
        self.assertEqual(self.store.get('outbox/upload-6')['payload'],{'item':'item-1','step':'upload-prompt','revision':2})


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

    def test_post_cancels_unrelated_old_unfinished_posts(self):
        self.store.put('items/item-old',{'status':'queued','revision':1,'messages':[{'message_id':3,'text':'Old'}]})
        self.store.put('items/item-pub',{'status':'previewed','revision':1,'messages':[{'message_id':4,'text':'Publishing'}]})
        self.store.put('specs/pub',{'item':'item-pub','state':'approved','spec':{'slug':'pub','copy':{}},'hash':'abcdefgh'})
        self.store.put('sessions/inbox',{})
        self.store.intake(post_update(21),PLAIN)
        self.assertEqual(self.store.get('items/item-old')['status'],'rejected')
        self.assertEqual(self.store.get('specs/pub')['state'],'approved')

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
                self.assertIsNone(self.tap(store,'done','item-10',token,12))
                item = store.get('items/item-10')
                self.assertEqual((item['status'],item['montage']),('queued',clips))
                self.assertEqual(store.get('outbox/draft-12')['payload'],{'item':'item-10','step':step,'revision':item['revision']})
                self.assertNotIn('collecting',store.get('sessions/inbox'))
                self.assertIsNone(self.tap(store,'done','item-10',token,13))

    def test_progress_question_during_montage_gets_status_and_adds_no_edit(self):
        store = Store(FakeFirestore())
        self.post(store,10,text='/post')
        self.video(store,11,'Jakarta Chinatown')
        self.tap(store,'done','item-10',store.get('items/item-10')['batch_token'],12)
        store.put('renders/r1',{'state':'running','progress':{'label':'Рендерю черновик','since':0}})
        store.change('items/item-10',lambda value:value.update(render='r1'))
        reply = store.intake({'update_id':13,'message':{'message_id':13,'text':'Прогресс ?'}},self.config)
        self.assertIn('рендерю черновик',reply)
        self.assertEqual(store.get('items/item-10').get('edits',[]),[])

    def test_progress_question_on_a_photo_post_uses_no_montage_words(self):
        store = Store(FakeFirestore())
        self.post(store,10,text='/post')
        store.intake({'update_id':11,'message':{'message_id':11,'text':'Привет'}},self.config)
        self.tap(store,'done','item-10',store.get('items/item-10')['batch_token'],12)
        reply = store.intake({'update_id':13,'message':{'message_id':13,'text':'Что там?'}},self.config)
        self.assertIn('готовлю превью',reply)
        self.assertNotIn('Монтаж',reply)
        self.assertEqual(store.get('items/item-10').get('edits',[]),[])

    def test_question_text_queues_an_answer_and_never_an_edit(self):
        store = Store(FakeFirestore())
        self.post(store,10,text='/post')
        store.intake({'update_id':11,'message':{'message_id':11,'text':'Привет'}},self.config)
        self.tap(store,'done','item-10',store.get('items/item-10')['batch_token'],12)
        store.change('items/item-10',lambda value:value.update(status='previewed'))
        store.put('specs/item-10',{'state':'previewed','item':'item-10','spec':{'targets':['tg-channel']},'preview_message':100})
        store.put('published-messages/100',{'slug':'item-10'})
        store.intake({'update_id':13,'message':{'message_id':13,'text':'Почему так?','reply_to_message':{'message_id':100}}},self.config)
        self.assertEqual(store.get('items/item-10').get('edits',[]),[])

    def test_done_accepts_photos_with_clips_and_the_pin_follows_the_stage(self):
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
            self.assertEqual([m for m,f in calls],['sendMessage','unpinAllChatMessages','pinChatMessage'])
            self.assertIn(': собираю · 1 фото, 1 клип',calls[0][1]['text'])
            self.assertTrue(calls[2][1]['disable_notification'])
            token = store.get('items/item-10')['batch_token']
            self.tap(store,'done','item-10',token,13)
            app.sync_pin(self.config,store)
        item = store.get('items/item-10')
        self.assertEqual((item['status'],item['montage']),('queued',False))
        self.assertEqual(store.get('outbox/draft-13')['payload']['step'],'draft')
        self.assertEqual([m for m,f in calls][3:],['editMessageText'])
        self.assertEqual((calls[3][1]['message_id'],calls[3][1]['text'].endswith(': готовлю черновик')),(500,True))

    def test_only_the_newest_post_keeps_its_pin_and_a_new_post_replaces_older_pins(self):
        store = Store(FakeFirestore())
        self.post(store,10,text='/post')
        store.put('items/item-20',{'messages':[],'revision':0,'status':'queued','montage':True,'slug':'item-20','created':time.time() - 100})
        store.put('sessions/pin',{'item':'item-10','text':'Открыт пост','at':1,'message_id':400,'pinned':True})
        ids = iter(range(500,510))
        calls = []
        def telegram(method,fields=None,files=None):
            calls.append((method,fields.get('message_id')))
            return {'message_id':next(ids)}
        with patch.object(app.platforms,'telegram',side_effect=telegram):
            app.sync_pin(self.config,store)
            self.assertEqual(list(store.get('sessions/pin')['posts']),['item-10'])
            self.assertEqual(calls,[('editMessageText',400)])
            store.put('items/item-30',{'messages':[],'revision':0,'status':'queued','slug':'item-30','created':time.time() + 100})
            app.sync_pin(self.config,store)
            self.assertEqual(list(store.get('sessions/pin')['posts']),['item-30'])
            self.assertEqual(calls[1:],[('unpinChatMessage',400),('deleteMessage',400),('sendMessage',None),('unpinAllChatMessages',None),('pinChatMessage',503)])
            store.put('items/item-30',{**store.get('items/item-30'),'status':'previewed'})
            store.put('specs/item-30',{'item':'item-30','state':'done'})
            app.sync_pin(self.config,store)
        self.assertEqual(calls[6:9],[('unpinChatMessage',503),('deleteMessage',503),('unpinAllChatMessages',None)])
        self.assertEqual(store.get('sessions/pin')['posts'],{})

    def test_a_partial_post_and_a_legacy_pin_of_a_closed_post_have_no_pin(self):
        store = Store(FakeFirestore())
        store.put('items/item-30',{'messages':[],'revision':0,'status':'previewed','slug':'item-30','created':1})
        store.put('specs/item-30',{'item':'item-30','state':'partial'})
        store.put('sessions/pin',{'item':'item-40','text':'Открыт пост','at':1,'message_id':400,'pinned':True})
        calls = []
        with patch.object(app.platforms,'telegram',side_effect=lambda method,fields=None,files=None:calls.append(method)):
            app.sync_pin(self.config,store)
        self.assertEqual(calls,['unpinChatMessage','deleteMessage','unpinAllChatMessages'])
        self.assertEqual(store.get('sessions/pin'),{'posts':{}})

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
        self.assertEqual(calls,['sendMessage','unpinAllChatMessages','pinChatMessage','unpinAllChatMessages','pinChatMessage'])

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

    def test_orphaned_final_render_fails_over(self):
        self.store.put('renders/r1',{'state':'done','delivered':True})
        self.store.put('specs/album',{'state':'rendering','hash':'abcdefgh1','final_render':'r1','item':'item-1','spec':{'copy':{}}})
        with patch.object(app,'tell'), patch.object(app,'sync_pin'):
            app.recover_running(self.config,self.store)
        self.assertEqual(self.store.get('specs/album')['state'],'partial')

    def test_stuck_running_spec_survives_a_failed_notification(self):
        self.store.put('specs/album',{'state':'running','hash':'abcdefgh1','started_at':0,'item':'item-1','spec':{'copy':{}}})
        with patch.object(app,'tell',side_effect=RuntimeError('telegram down')):
            with self.assertRaises(RuntimeError):
                app.recover_running(self.config,self.store)
        with patch.object(app,'tell') as tell:
            app.recover_running(self.config,self.store)
        tell.assert_called_once()
        self.assertEqual(self.store.get('specs/album')['state'],'partial')

    def test_newest_spec_wins_over_an_old_drafting_spec(self):
        now = time.time()
        self.store.db.document('specs/old').set({'state':'drafting','item':'item-1','updated':now - 100,'spec':{'copy':{}}})
        self.store.put('specs/album',{'state':'previewed','hash':'abcdefgh1','item':'item-1','spec':{'targets':['tg-channel']},'preview_message':100})
        item = self.store.get('items/item-1')
        states = newest_states(snapshot.to_dict() for snapshot in self.store.db.collection('specs').stream())
        self.assertEqual(pin_stage(item,states.get('item-1')),'ждёт «Опубликовать»')

    def test_superseded_and_overdue_render_jobs_are_failed_silently(self):
        now = time.time()
        self.store.put('items/item-1',{'status':'drafting','revision':2,'messages':[],'slug':'album','created':now,'updated':now})
        self.store.put('renders/old',{'item':'item-1','revision':1,'state':'running','created':now})
        self.store.put('renders/late',{'item':'item-1','revision':2,'state':'queued','created':now - app.FINAL_DEADLINE - 10})
        self.store.put('renders/live',{'item':'item-1','revision':2,'state':'running','created':now})
        self.store.put('renders/final',{'item':'item-1','revision':1,'mode':'final','state':'running','created':now})
        with patch.object(app,'tell') as tell:
            app.recover_running(self.config,self.store)
        tell.assert_not_called()
        self.assertEqual([(self.store.get('renders/' + key)['state'],self.store.get('renders/' + key).get('delivered')) for key in ('old','late','live','final')],[('failed',True),('failed',True),('running',None),('running',None)])

    def test_stale_drafting_item_is_failed_and_announced_once(self):
        self.store.put('items/item-1',{'status':'drafting','revision':1,'messages':[{'message_id':1,'text':'Тема'}],'slug':'album','created':0,'updated':time.time() - 2000})
        self.store.db.document('specs/album').set({'state':'drafting','hash':'','item':'item-1','updated':time.time() - 2000,'spec':{'copy':{}}})
        with patch.object(app,'tell') as tell:
            app.recover_running(self.config,self.store)
            app.recover_running(self.config,self.store)
        tell.assert_called_once()
        self.assertIn('завис и остановлен',tell.call_args[0][1])
        self.assertEqual((self.store.get('items/item-1')['status'],self.store.get('specs/album')['state']),('failed','failed'))

    def test_stale_item_waiting_for_upload_is_not_failed(self):
        self.store.put('items/item-1',{'status':'queued','waiting_upload':True,'revision':1,'messages':[{'message_id':1,'text':'Тема'}],'slug':'album','created':0,'updated':time.time() - 5000})
        with patch.object(app,'tell') as tell:
            app.recover_running(self.config,self.store)
        tell.assert_not_called()
        self.assertEqual(self.store.get('items/item-1')['status'],'queued')

    def test_item_waiting_for_upload_over_a_day_is_failed_and_announced_once(self):
        self.store.put('items/item-1',{'status':'queued','waiting_upload':True,'montage':True,'revision':1,'messages':[{'message_id':1,'text':'Тема'}],'slug':'album','created':0,'updated':time.time() - 90000})
        with patch.object(app,'tell') as tell:
            app.recover_running(self.config,self.store)
            app.recover_running(self.config,self.store)
        tell.assert_called_once()
        self.assertIn('файл так и не загрузился, остановлен',tell.call_args[0][1])
        self.assertEqual(self.store.get('items/item-1')['status'],'failed')

    def test_pin_shows_upload_wait(self):
        self.store.put('items/item-1',{'status':'queued','waiting_upload':True,'montage':True,'revision':1,'messages':[{'message_id':1,'text':'Тема'}],'slug':'album','created':time.time(),'updated':time.time()})
        self.assertIn('жду загрузку файла',list(app.pin_texts(self.store).values())[0])

    def test_scheduled_post_is_pinned_as_scheduled(self):
        later = time.strftime('%Y-%m-%dT%H:%M:%S+00:00',time.gmtime(time.time() + 7200))
        self.store.put('items/item-1',{'status':'previewed','revision':1,'messages':[{'message_id':1,'text':'Тема'}],'slug':'album','created':time.time(),'updated':time.time()})
        self.store.put('specs/album',{'state':'approved','item':'item-1','updated':time.time(),'spec':{'at':later}})
        self.assertIn('запланирован',list(app.pin_texts(self.store).values())[0])


    def test_an_older_post_never_regains_the_pin_when_the_newest_has_no_stage(self):
        self.store.put('items/item-1',{'status':'queued','revision':1,'messages':[],'slug':'a','created':time.time() - 100,'updated':time.time()})
        self.store.put('items/item-2',{'status':'rejected','revision':1,'messages':[],'slug':'b','created':time.time(),'updated':time.time()})
        self.assertEqual(app.pin_texts(self.store),{})

    def test_server_errors_retry_only_idempotent_telegram_methods(self):
        failure = p.Error('API failure: 503',status_code=503)
        for method,calls in (('sendMessage',1),('editMessageText',3)):
            with patch.object(p,'request',side_effect=failure) as request, patch.object(p.time,'sleep'), patch.object(p,'key',return_value='t'):
                with self.assertRaises(p.Error):
                    p.telegram(method,{})
            self.assertEqual(request.call_count,calls)


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

    def test_confirm_failure_outside_platforms_still_closes_the_session(self):
        with patch.object(p,'link_opens',return_value=True), patch.object(p,'social_session',side_effect=KeyError('access_token')), patch.object(app,'enqueue'):
            app.verify(self.config,self.store,'example',3)
        session = self.store.get('sessions/item-1')
        self.assertEqual(session['state'],'published')
        rows = next(e for e in session['steps'] if e['id'] == 'verify')['targets']
        self.assertEqual((rows['tg-channel']['status'],rows['threads']['status'],rows['threads']['api']),('verified','unverified','error: KeyError'))


class SessionRevisionTests(unittest.TestCase):
    def test_newer_revision_supersedes_the_running_step_and_older_is_refused(self):
        store = Store(FakeFirestore())
        store.put('items/item-1',{'status':'drafting','revision':1,'messages':[],'slug':'example'})
        self.assertTrue(store.step('item-1','draft',{},1))
        self.assertTrue(store.step('item-1','draft',{},2))
        self.assertFalse(store.step('item-1','draft',{'state':'failed'},1))
        self.assertTrue(store.step('item-1','draft',{'state':'done'},2))
        drafts = [e for e in store.get('sessions/item-1')['steps'] if e['id'] == 'draft']
        self.assertEqual([(e['revision'],e['state']) for e in drafts],[(1,'cancelled'),(2,'done')])


if __name__ == '__main__':
    unittest.main()
