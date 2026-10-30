from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

FUNCTIONS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FUNCTIONS))
from fake_firestore import FakeFirestore
from store import Store, table_rows, trimmed, open_hint, POST_HINT
import main as app
import progress


def step(id, state, since, ended=None, **extra):
    return {'id':id, 'label':id, 'state':state, 'since':since, 'ended':ended, **extra}


def session(steps, state='open', **extra):
    return {'item':'item-1', 'state':state, 'opened':steps[0]['since'], 'ended':None, 'outcome':None, 'usd':0, 'updated':max(s['ended'] or s['since'] for s in steps), 'steps':steps, **extra}


class TimelineTests(unittest.TestCase):
    def test_consecutive_repeats_merge_into_one_row(self):
        value = session([step('edit', 'done', 1, 11, usd=0.1), step('edit', 'cancelled', 11, 15, usd=0.2), step('edit', 'done', 15, 31), step('render', 'done', 31, 41), step('prepare', 'planned', 41)])
        past = progress.timeline(value, 100)['past']
        self.assertEqual([(p['id'], p['attempts'], p['cancelled'], p['seconds'], p['usd'], p['state']) for p in past], [('edit', 3, 1, 30, 0.3, 'done'), ('render', 1, 0, 10, 0, 'done')])

    def test_backfilled_session_has_no_durations(self):
        value = session([step('collect', 'done', 5, 50), step('draft', 'done', 5, 50), step('publish', 'done', 5, 50)], 'published')
        self.assertEqual([p['seconds'] for p in progress.timeline(value, 100)['past']], [None, None, None])

    def test_reason_maps_known_errors_and_keeps_the_rest(self):
        self.assertEqual(progress.reason_of('API failure: 190', 'threads'), 'Нужен новый токен Threads')
        self.assertEqual(progress.reason_of('API failure: 190'), 'Нужен новый токен')
        self.assertEqual(progress.reason_of("Bad Request: can't download or generate the file"), 'Telegram не принял файл')
        self.assertEqual(progress.reason_of('boom'), 'boom')
        self.assertEqual(progress.reason_of(None), '')


class SessionViewTests(unittest.TestCase):
    def view(self, value, now, item=None, spec=None):
        return progress.session_view('item-1', value, item, spec, now, 'Asia/Makassar')

    def test_open_session_of_a_failed_draft_is_stopped(self):
        value = session([step('collect', 'done', 0, 10), step('draft', 'done', 10, 20)])
        view = self.view(value, 1000, {'status':'failed','messages':[]}, {'state':'failed'})
        self.assertEqual((view['state'], view['outcome']['kind'], view['stalled']), ('stopped', 'stopped', False))

    def test_stalled_follows_the_tick_thresholds(self):
        value = session([step('collect', 'done', 0, 10), step('draft', 'running', 10)])
        item = {'status':'drafting','messages':[]}
        self.assertFalse(self.view(value, 10 + 1700, item)['stalled'])
        view = self.view(value, 10 + 1900, item)
        self.assertEqual((view['stalled'], view['idle']), (True, 1900))
        publish = session([step('publish', 'running', 1)])
        self.assertTrue(self.view(publish, 700, item)['stalled'])
        wait = session([step('wait', 'running', 1)])
        self.assertFalse(self.view(wait, 90000, item)['stalled'])

    def test_stopped_reason_comes_from_the_data(self):
        value = session([step('draft', 'running', 10)])
        cases = [({'status':'failed', 'stop_why':'upload'}, {'state':'failed'}, 'файл не загрузился'), ({'status':'failed', 'stop_why':'stalled'}, {'state':'failed'}, 'черновик завис и остановлен'), ({'status':'drafting'}, {'state':'failed', 'errors':{'x':'Материал истёк. Пришли файлы заново.'}}, 'материал устарел'), ({'status':'drafting'}, {'state':'failed', 'errors':{'x':'boom'}}, 'boom'), ({'status':'drafting'}, {'state':'failed'}, 'ошибка черновика')]
        for item, spec, why in cases:
            view = self.view(value, 100, {**item, 'messages':[]}, spec)
            self.assertEqual((view['state'], view['outcome']['why']), ('stopped', why))

    def test_failed_fix_operation_does_not_stop_the_post(self):
        value = session([step('draft', 'running', 10)])
        view = self.view(value, 100, {'status':'drafting', 'messages':[]}, {'state':'failed', 'operation':'fix'})
        self.assertEqual(view['state'], 'open')

    def test_busy_render_extends_the_draft_limit(self):
        value = session([step('draft', 'running', 10)])
        item = {'status':'drafting', 'messages':[]}
        busy = progress.session_view('item-1', value, item, None, 10 + 3000, 'Asia/Makassar', [{'label':'x', 'done':False, 'seconds':1}])
        self.assertFalse(busy['stalled'])
        self.assertTrue(self.view(value, 10 + 3000, item)['stalled'])

    def test_current_step_keeps_its_duration_when_past_steps_are_flat(self):
        value = session([step('collect', 'done', 5, 50), step('draft', 'done', 5, 50), step('wait', 'running', 50)])
        view = progress.timeline(value, 80)
        self.assertEqual((view['past'][0]['seconds'], view['current']['seconds']), (None, 30))

    def test_closed_session_is_never_stalled(self):
        value = session([step('draft', 'done', 0, 10)], 'published')
        self.assertFalse(self.view(value, 99999, {'status':'approved','messages':[]})['stalled'])


class StuckAndTitleTests(unittest.TestCase):
    def view(self, value, now, item=None, spec=None):
        return progress.session_view('item-1', value, item, spec, now, 'Asia/Makassar')

    def test_open_session_without_running_step_stalls_from_last_ended_step(self):
        value = session([step('collect', 'done', 0, 10), step('draft', 'done', 10, 100), step('publish', 'planned', 100)])
        fresh = self.view(value, 100 + 600, {'status':'previewed', 'messages':[]})
        self.assertEqual((fresh['stalled'], fresh['idle']), (False, 600))
        stuck = self.view(value, 100 + 2000, {'status':'previewed', 'messages':[]})
        self.assertEqual((stuck['stalled'], stuck['idle'], stuck['state']), (True, 2000, 'open'))

    def test_waiting_upload_does_not_stall_without_running_step(self):
        value = session([step('collect', 'done', 0, 10)])
        self.assertFalse(self.view(value, 10 + 5000, {'status':'queued', 'waiting_upload':True, 'messages':[]})['stalled'])

    def test_title_falls_back_to_first_owner_line_then_time(self):
        value = session([step('collect', 'running', 0)], dialog=[{'who':'owner', 'text':'/post'}, {'who':'bot', 'text':'Присылай'}, {'who':'owner', 'text':'Кофейня на набережной'}])
        self.assertEqual(self.view(value, 10)['title'], 'Кофейня на набережной')
        empty = session([step('collect', 'running', 0)], opened=0)
        empty['opened'] = 3600 * 3
        self.assertEqual(self.view(empty, 10)['title'], 'Пост от 11:00')

    def test_trimmed_cuts_at_the_last_space_before_sixty(self):
        self.assertEqual(trimmed('слово ' * 5), 'слово слово слово слово слово')
        long = ' '.join(['слово'] * 20)
        cut = trimmed(long)
        self.assertTrue(cut.endswith('слово…') and len(cut) <= 61)
        self.assertEqual(trimmed('х' * 80), 'х' * 60 + '…')

    def test_message_to_an_open_post_gets_status_and_buttons(self):
        reply = open_hint({'status':'failed', 'revision':4, 'messages':[{'text':'Кофейня'}]}, 'item-1')
        self.assertEqual([b['callback_data'] for b in reply['reply_markup']['inline_keyboard'][0]], ['cdraft:item-1:4', 'again:item-1:4'])
        self.assertIn('уже открыт', reply['text'])
        self.assertEqual(open_hint(None, None), POST_HINT)


class TickStopTests(unittest.TestCase):
    def stopped_view(self, waiting):
        store = Store(FakeFirestore())
        now = time.time()
        old = now - 30 * 3600
        store.put('items/item-1', {'status':'drafting', 'messages':[], 'updated':old, 'created':old, **({'waiting_upload':True} if waiting else {})})
        store.put('sessions/item-1', session([step('collect', 'done', old, old + 1), step('draft', 'running', old + 1)]))
        with patch.object(app, 'tell'):
            app.stop_stale({'roman_chat_id':1}, store)
        item = store.get('items/item-1')
        self.assertEqual(item['status'], 'failed')
        return progress.session_view('item-1', store.get('sessions/item-1'), item, None, now, 'Asia/Makassar')

    def test_reason_survives_the_notice_loop(self):
        stalled = self.stopped_view(False)
        self.assertEqual((stalled['state'], stalled['outcome']['why']), ('stopped', 'черновик завис и остановлен'))
        upload = self.stopped_view(True)
        self.assertEqual((upload['state'], upload['outcome']['why']), ('stopped', 'файл не загрузился'))

    def test_expired_material_text_maps_to_its_reason(self):
        self.assertEqual(progress.reason_of('Материал истёк. Пришли файлы заново.'), 'материал устарел')


class DialogTests(unittest.TestCase):
    def test_long_entry_is_kept_and_oversized_dialog_drops_oldest_first(self):
        store = Store(FakeFirestore())
        store.put('sessions/item-1', session([step('collect', 'running', 1)]))
        store.talk('item-1', 'ж' * 3000, 'b' * 5000)
        dialog = store.get('sessions/item-1')['dialog']
        self.assertEqual([len(r['text']) for r in dialog], [3000, 4096])
        for n in range(140):
            store.talk('item-1', 'o%03d' % n + 'x' * 4000, None)
        dialog = store.get('sessions/item-1')['dialog']
        self.assertLessEqual(sum(len(r['text'].encode()) for r in dialog), 512 * 1024)
        self.assertEqual(dialog[-1]['text'][:4], 'o139')
        self.assertNotEqual(dialog[0]['text'][:1], 'ж')


class TableTests(unittest.TestCase):
    def test_finished_post_table_follows_results_not_the_spec(self):
        saved = {'state':'done', 'spec':{'targets':['ig-reel']}, 'results':{'tg-channel':{'url':'https://t.me/c/1'}, 'threads':{'url':'https://www.threads.com/x'}}}
        rows = table_rows(saved, {'chosen':[]})
        self.assertEqual([(r['target'], r['status'], r['ok']) for r in rows], [('tg-channel', 'опубликовано', True), ('threads', 'опубликовано', True)])


if __name__ == '__main__':
    unittest.main()
