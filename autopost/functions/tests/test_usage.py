import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import drafter
from fake_firestore import FakeFirestore
from store import Store, tally

USAGE = NS(input_tokens=1000, output_tokens=2000, cache_read_input_tokens=10000, cache_creation_input_tokens=3000, cache_creation=NS(ephemeral_5m_input_tokens=2000, ephemeral_1h_input_tokens=1000), server_tool_use=NS(web_search_requests=2))


class UsageTests(unittest.TestCase):
    def test_usd_formula(self):
        row = tally(USAGE)
        self.assertAlmostEqual(row['usd'], (1000 * 2 + 2000 * 2.5 + 1000 * 4 + 10000 * 0.1 + 2000 * 10) / 1e6 + 0.02)
        self.assertEqual((row['cache_write_tokens'], row['web_searches'], row['calls']), (3000, 2, 1))
        self.assertAlmostEqual(tally(NS(input_tokens=0, output_tokens=0, cache_creation_input_tokens=1000))['usd'], 0.0025)

    def test_item_and_month_docs(self):
        store = Store(FakeFirestore())
        store.record_usage('a', 'draft', USAGE)
        store.record_usage('a', 'draft', USAGE)
        store.record_usage(None, 'skills', USAGE)
        store.reserve_speech(30, 'a/voice.ogg')
        item = store.get('usage/item-a')
        self.assertEqual(item['anthropic']['draft']['calls'], 2)
        self.assertEqual(item['speech_seconds'], 30)
        self.assertIn('updated', item)
        month = next(v for k, v in store.db.values.items() if k.startswith('usage/anthropic-'))
        self.assertEqual(month['calls'], 3)
        self.assertEqual(set(item['anthropic']['draft']), {'calls', 'input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'web_searches', 'usd'})

    def test_failing_recorder_keeps_ask(self):
        response = Mock(content=[Mock(type='text', text=json.dumps({'answer': 'Да, готово'}))], stop_reason='end_turn', usage=USAGE)
        client = Mock()
        client.messages.create.return_value = response
        with patch.object(drafter, 'connect', return_value=client):
            value = drafter.ask({}, {}, [], lambda v: [], rec=Mock(side_effect=RuntimeError('boom')))
        self.assertEqual(value, {'answer': 'Да, готово'})


if __name__ == '__main__':
    unittest.main()
