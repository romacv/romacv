import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

FUNCTIONS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FUNCTIONS))
import skills
import voice
from fake_firestore import FakeFirestore
from store import Store


class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(FakeFirestore())

    def test_skill_prompt_holds_the_texts_only_when_collected(self):
        self.assertNotIn(voice.KEY, skills.render(skills.load(self.store)))
        voice.published(self.store, {'copy': {'tg-channel': 'Сломал сборку в пятницу, чинил до утра. #ios'}}, {'tg-channel': {}})
        book = skills.render(skills.load(self.store))
        self.assertIn('Мои тексты', book)
        self.assertIn('[telegram] Сломал сборку в пятницу, чинил до утра.', book)
        self.assertNotIn('#ios', book)
        self.assertIn('writing-texts', book)

    def test_refresh_fetches_only_newer_and_dedupes(self):
        rows = [('Новый', 300), ('Старый', 100), ('#tag', 400)]
        with patch.object(voice, 'graph', return_value=rows):
            voice.refresh({}, self.store)
            first = self.store.get(voice.PATH)
            self.assertEqual([r['text'] for r in first['texts'] if r['network'] == 'threads'], ['Новый', 'Старый'])
            self.assertEqual(first['cursors']['threads'], 400)
            voice.refresh({}, self.store)
        self.assertEqual(len(self.store.get(voice.PATH)['texts']), len(first['texts']))

    def test_failed_network_backs_off_alone_and_ensure_is_capped(self):
        def graph(provider, *args):
            if provider == 'threads':
                raise RuntimeError('down')
            return [('Пост', 5)]
        with patch.object(voice, 'graph', graph):
            voice.refresh({}, self.store)
        self.assertEqual(voice.due(self.store), set())
        state = self.store.get(voice.STATE)
        state['failed']['threads'] = time.time() - voice.BACKOFF - 1
        state['ok']['telegram'] = 0
        self.store.put(voice.STATE, state)
        self.assertEqual(voice.due(self.store), {'telegram'} | {'threads'})
        with patch.object(voice, 'CAP', 0.2), patch.object(voice, 'refresh', lambda *a: time.sleep(2)):
            started = time.time()
            voice.ensure({}, self.store)
        self.assertLess(time.time() - started, 1)


if __name__ == '__main__':
    unittest.main()
