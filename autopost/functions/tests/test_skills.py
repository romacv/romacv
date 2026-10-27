import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import drafter
import learning
import skills
from fake_firestore import FakeFirestore
from store import Store


class SkillsTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(FakeFirestore())

    def test_live_skills_migrate_once(self):
        skills.load(self.store)
        self.store.put(skills.SEEDED, {'at': 1})
        old = skills.fresh(skills.SEED['montage'])
        old['body'] = 'Mine. A. Short unspoken headlines are titles, not subtitles. one at the start, a new one when the location changes. Language follows the post.'
        self.store.put('skills/montage', old)
        self.store.put('skills/writing-texts', {**old, 'body': 'edited by Roman'})
        found = skills.load(self.store)
        self.assertTrue(found['montage']['body'].startswith('Mine. A. one cue'))
        self.assertNotIn('Short unspoken', found['montage']['body'])
        self.assertIn('covers the whole target', found['montage']['body'])
        self.assertEqual(found['writing-texts']['body'], 'edited by Roman')
        self.assertIn('Short unspoken', found['montage']['history'][-1]['body'])
        self.assertEqual(self.store.get(skills.SEEDED)['version'], skills.MIGRATION)
        self.store.put('skills/montage', old)
        self.assertIn('Short unspoken', skills.load(self.store)['montage']['body'])

    def test_seed_once_and_prompt_loading(self):
        first = skills.load(self.store)
        self.assertEqual(set(first), set(skills.SEED))
        self.store.put('skills/montage', {**first['montage'], 'body': 'edited by Roman'})
        self.assertEqual(skills.load(self.store)['montage']['body'], 'edited by Roman')
        self.store.delete('skills/about-roman')
        self.assertNotIn('about-roman', skills.load(self.store))
        self.store.put('items/a', {'revision': 1})
        response = Mock(content=[Mock(type='text', text='{}')])
        client = Mock()
        client.messages.create.return_value = response
        import anthropic
        with patch.object(anthropic, 'Anthropic', return_value=client):
            drafter.ask({'anthropic_key': 'k'}, {'a': 1}, [], lambda value: [], thread=(self.store, 'conversations/a', 1, 0, ('items/a', 'revision', 1)))
        contract, book = client.messages.create.call_args.kwargs['system']
        self.assertIn('EDL JSON schema', contract['text'])
        self.assertIn('edited by Roman', book['text'])
        self.assertNotIn('about-roman', book['text'])
        self.assertEqual((contract['cache_control'], book['cache_control']), ({'type': 'ephemeral'}, {'type': 'ephemeral'}))

    def test_budget_keeps_newest_examples(self):
        base = skills.fresh(skills.SEED['montage'])
        base['examples'] = [{'verdict': 'bad', 'feedback': 'x' * 500, 'before': 'b' * 500, 'after': 'a' * 500, 'at': i} for i in range(40)]
        text = skills.render({'montage': base})
        self.assertLessEqual(len(text.encode()), skills.BUDGET)
        self.assertIn('x' * 500, text)

    def test_learn_output_validation(self):
        current = {'montage': skills.fresh(skills.SEED['montage'])}
        corpus = '\n'.join(skills.words([{'role': 'user', 'text': json.dumps({'edits': ['Субтитры слишком мелкие, сделай крупнее']}, ensure_ascii=False)}]))
        good = {'skill': 'montage', 'verdict': 'bad', 'feedback': 'субтитры слишком мелкие', 'before': 'a', 'after': 'b'}
        out = skills.check({'examples': [good,
            {**good, 'feedback': 'invented words'},
            {**good, 'skill': 'unknown'},
            {**good, 'after': 'written by Claude'},
            {**good, 'before': 'mail me r@example.com'},
            {**good, 'before': 'not in any draft'},
            {**good, 'verdict': 'meh'}],
            'revisions': [{'skill': 'montage', 'body': 'x' * 7000}, {'skill': 'nope', 'body': 'y' * 200}]}, current, corpus, 'a draft', 'b final')
        self.assertEqual(out['examples'], {'montage': [{'verdict': 'bad', 'feedback': 'субтитры слишком мелкие', 'before': 'a', 'after': 'b'}]})
        self.assertEqual(out['bodies'], {})
        skills.load(self.store)
        longer = current['montage']['body'] + '\n- one more rule learned from Roman'
        skills.apply(self.store, {'examples': out['examples'], 'bodies': {'montage': longer}}, {'montage': 0})
        saved = self.store.get('skills/montage')
        self.assertEqual((len(saved['examples']), saved['revision'], saved['body'] == longer), (1, 2, False))
        skills.apply(self.store, {'examples': {}, 'bodies': {'montage': 'z' * 200}}, {'montage': 2})
        self.assertNotEqual(self.store.get('skills/montage')['body'], 'z' * 200)
        skills.apply(self.store, {'examples': {}, 'bodies': {'montage': longer}}, {'montage': 3})
        saved = self.store.get('skills/montage')
        self.assertEqual((saved['body'], len(saved['history']), saved['history'][0]['source']), (longer, 1, 'learn'))

    def test_skill_keyboard_and_split_text(self):
        found = skills.load(self.store)
        rows = skills.keyboard(found)['inline_keyboard']
        self.assertEqual([r[0]['web_app']['url'] for r in rows], ['https://www.resrom.com/p?view=skills&skill=' + k for k in sorted(found)])
        self.assertEqual([r[0]['text'] for r in rows], sorted(found))
        pages = skills.split(skills.full('x', {'title': 'T', 'body': 'a' * 3000 + '\n' + 'b' * 3000, 'examples': [{'verdict': 'bad', 'feedback': 'f'}]}))
        self.assertTrue(len(pages) > 1 and all(len(p.encode('utf-16-le')) // 2 <= 4096 for p in pages) and pages[-1].endswith('bad: «f»'))

    def test_learning_is_scheduled_once_and_only_with_signal(self):
        turns = [{'role': 'user', 'text': '{}'}, {'role': 'assistant', 'text': '{}'}]
        self.store.put('specs/clean', {'spec': {}})
        self.store.put('conversations/clean', {'turns': turns})
        learning.schedule(self.store, 'clean', 'clean', 'published')
        self.assertEqual(list(self.store.db.collection('outbox').stream()), [])
        self.store.put('specs/edited', {'spec': {}})
        self.store.put('conversations/edited', {'turns': turns + [{'role': 'user', 'text': '{"edits":["x"]}'}, {'role': 'assistant', 'text': '{}'}]})
        learning.schedule(self.store, 'edited', 'edited', 'published')
        learning.schedule(self.store, 'edited', 'edited', 'published')
        self.assertEqual(len(list(self.store.db.collection('outbox').stream())), 1)

    def test_learning_failure_is_silent_and_cleans_the_record(self):
        self.store.put('conversations/i', {'turns': [{'role': 'user', 'text': '{}'}, {'role': 'assistant', 'text': '{}'}]})
        key = learning.capture(self.store, 'i', 's', 'published')
        with patch.object(drafter, 'converse', side_effect=RuntimeError('down')):
            learning.step({}, self.store, {'record': key})
        self.assertIsNone(self.store.get(key))
