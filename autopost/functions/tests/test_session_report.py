import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))
from fake_firestore import FakeFirestore
import main as app
import progress
from store import Store, hinted, table_text, table_rows, CONTAINER_AGE


class SessionReportTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(FakeFirestore())
        self.config = {'roman_chat_id':1,'webhook_secret':'test-secret'}
        self.now = time.time()

    def partial(self):
        self.store.put('items/item-1', {'status':'approved','chosen':['telegram'],'messages':[]})
        self.store.put('specs/s1', {'item':'item-1','state':'partial','started_at':self.now - 100,
            'spec':{'targets':['tg-channel','threads']},
            'results':{'tg-channel':{'url':'https://t.me/c/1','at':self.now}},
            'errors':{'threads':'прервано перезапуском'}})
        return self.store.get('specs/s1')

    def test_table_lists_each_target_with_status_time_and_link(self):
        text = table_text(self.partial(), self.store.get('items/item-1'))
        self.assertIn('https://t.me/c/1', text)
        self.assertRegex(text, r'\d\. .+ — прервано — — — —')
        self.assertNotIn('всё опубликовано', text)

    def test_hint_only_when_no_target_is_chosen(self):
        self.assertNotEqual(hinted('x', ['a'], ()), hinted('x', ['a'], ['telegram']))
        self.assertEqual(hinted('x', ['a'], ['telegram']), 'x')

    def test_command_word_retry_requeues_a_partial_post(self):
        self.partial()
        self.store.put('sessions/inbox', {'current':'item-1'})
        reply = self.store.intake({'update_id':5,'message':{'message_id':5,'text':' повторить '}}, self.config)
        self.assertIsNotNone(reply)
        self.assertEqual(self.store.get('specs/s1')['state'], 'approved')

    def test_every_text_is_answered_and_kept_in_the_dialog(self):
        self.store.intake({'update_id':1,'message':{'message_id':1,'entities':[{'type':'bot_command','offset':0,'length':5}],'text':'/post'}}, self.config)
        reply = self.store.intake({'update_id':2,'message':{'message_id':2,'text':'Привет'}}, self.config)
        self.assertTrue(reply)
        self.assertIn('Привет', [d['text'] for d in self.store.get('sessions/item-1')['dialog']])

    def test_progress_lists_partial_posts_with_dialog_and_percent(self):
        self.partial()
        view = progress.snapshot(self.store, self.now)
        session = next(s for s in view['sessions'] if s['item'] == 'item-1')
        self.assertEqual(session['state'], 'partial')
        self.assertTrue(0 <= session['percent'] <= 100)
        self.assertEqual(session['table'][1]['target'], 'threads')
        self.assertIn('dialog', session)

    def test_maintenance_backfills_session_and_reasons_for_old_partial(self):
        self.store.put('items/item-1', {'status':'approved','messages':[]})
        self.store.put('specs/s1', {'item':'item-1','state':'partial','spec':{'targets':['tg-channel','threads']},
            'results':{'tg-channel':{'url':'u'}}})
        config = {**self.config, 'webhook_url':'test'}
        with patch.object(app.platforms, 'telegram', return_value={'url':'test'}), patch.object(app.platforms, 'request', return_value={}), patch.object(app, 'enqueue'), patch.object(app, 'tell'), patch('urllib.request.urlopen', side_effect=OSError()):
            app.maintenance(config, self.store, MagicMock())
        value = self.store.get('specs/s1')
        self.assertTrue(value['published_at'])
        self.assertTrue(value['errors']['threads'])
        self.assertEqual(self.store.get('sessions/item-1')['state'], 'partial')

    def test_stale_container_checkpoint_is_recreated(self):
        calls = []
        def make():
            calls.append(1)
            return 'c'
        self.store.checkpoint('s1', 'ig:container:1', make)
        self.store.checkpoint('s1', 'ig:container:1', make)
        self.assertEqual(len(calls), 1)
        path = [k for k in self.store.db.values if 'checkpoints' in k][0]
        self.assertIsNotNone(path)
        self.store.change(path, lambda v: v.update(at=self.now - CONTAINER_AGE - 10))
        self.store.checkpoint('s1', 'ig:container:1', make)
        self.assertEqual(len(calls), 2)

    def test_collecting_post_without_material_for_six_hours_is_cancelled(self):
        self.store.put('items/item-1', {'status':'collecting','messages':[],'updated':self.now - 7 * 3600,'created':self.now - 8 * 3600})
        self.store.put('sessions/inbox', {'collecting':'item-1'})
        with patch.object(app, 'tell') as tell, patch.object(app.platforms, 'telegram', return_value={}):
            app.stop_stale(self.config, self.store)
        self.assertEqual(self.store.get('items/item-1')['status'], 'rejected')
        self.assertNotIn('collecting', self.store.get('sessions/inbox'))
        self.assertTrue(tell.called)

    def test_classifier_cost_is_recorded_at_haiku_rates(self):
        self.store.record_usage(None, 'classify', {'model':'claude-haiku-5-5', 'input_tokens':1000000, 'output_tokens':1000000})
        month = [v for k, v in self.store.db.values.items() if k.startswith('usage/anthropic-')][0]
        self.assertAlmostEqual(month['usd'], 0.6)

    def test_typed_cancel_of_a_collecting_post_queues_no_second_message(self):
        self.store.intake({'update_id':1,'message':{'message_id':1,'entities':[{'type':'bot_command','offset':0,'length':5}],'text':'/post'}}, self.config)
        self.store.intake({'update_id':2,'message':{'message_id':2,'text':'Отмени'}}, self.config)
        self.assertFalse([k for k in self.store.db.values if k.startswith('outbox/say-')])


    def test_typed_cancel_of_a_queued_post_rejects_the_draft(self):
        self.store.put('items/item-9', {'status':'queued','messages':[],'slug':'item-9'})
        self.store.put('sessions/inbox', {'current':'item-9'})
        reply = self.store.intake({'update_id':5,'message':{'message_id':5,'text':'Отмени'}}, self.config)
        self.assertEqual(reply, 'Черновик отклонён.')

    def test_question_and_fix_while_collecting_never_become_material(self):
        self.store.intake({'update_id':1,'message':{'message_id':1,'entities':[{'type':'bot_command','offset':0,'length':5}],'text':'/post'}}, self.config)
        self.store.classifier = lambda text, context: {'kind':'question', 'value':None}
        reply = self.store.intake({'update_id':2,'message':{'message_id':2,'text':'Что там?'}}, self.config)
        self.assertIn('собирается', reply)
        self.store.classifier = lambda text, context: {'kind':'fix', 'value':'короче'}
        self.store.intake({'update_id':3,'message':{'message_id':3,'text':'сделай короче'}}, self.config)
        item = self.store.get('items/' + self.store.get('sessions/inbox')['collecting'])
        self.assertEqual(item['messages'], [])
        self.assertEqual(item['instructions'], ['короче'])

    def test_redelivery_makes_no_classifier_call_and_no_second_dialog_line(self):
        self.store.intake({'update_id':1,'message':{'message_id':1,'entities':[{'type':'bot_command','offset':0,'length':5}],'text':'/post'}}, self.config)
        calls = []
        self.store.classifier = lambda text, context: calls.append(text) or {'kind':'question', 'value':None}
        update = {'update_id':2,'message':{'message_id':2,'text':'Что там?'}}
        self.store.intake(update, self.config)
        self.store.intake(update, self.config)
        self.assertEqual(len(calls), 1)
        key = self.store.get('sessions/inbox')['collecting']
        self.assertEqual(len(self.store.get('sessions/' + key)['dialog']), 4)

    def test_container_checkpoint_without_a_time_is_recreated(self):
        self.store.put('specs/s1', {'state':'running'})
        made = []
        for _ in range(2):
            self.store.checkpoint('s1', 'ig:container', lambda: made.append(1) or 'c')
            self.store.put('specs/s1/checkpoints/' + __import__('hashlib').sha256(b'ig:container').hexdigest(), {'state':'done', 'name':'ig:container', 'value':'c'})
        self.assertEqual(len(made), 2)

    def test_typed_retry_refuses_an_old_partial_post_and_percent_follows_the_state(self):
        saved = self.partial()
        saved['updated'] = self.now - 49 * 3600
        self.store.db.document('specs/s1').set(saved)
        self.store.put('sessions/inbox', {'current':'item-1'})
        reply = self.store.intake({'update_id':7,'message':{'message_id':7,'text':'Повторить'}}, self.config)
        self.assertIn('поздно', reply)
        self.assertEqual(progress.percent_of({'steps':[{'state':'done'}]}, 'cancelled'), 0)
        self.assertEqual(progress.percent_of({}, 'published'), 100)
        self.assertEqual([r['ok'] for r in table_rows(saved, None)], [True, False])


if __name__ == '__main__':
    unittest.main()
