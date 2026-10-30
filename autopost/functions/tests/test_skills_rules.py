import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import skills
from fake_firestore import FakeFirestore
from store import Store


class OwedRulesTests(unittest.TestCase):
    def test_live_publishing_skill_gets_owed_rules_once(self):
        store = Store(FakeFirestore())
        skills.load(store)
        old = skills.fresh(skills.SEED['publishing'])
        old['body'] = 'Mine.\n- Hints win.'
        store.put('skills/publishing', old)
        store.put(skills.SEEDED, {'at': 1, 'version': 4})
        found = skills.load(store)['publishing']
        for rule in ('never @handle', 'previewed once', 'one centred block', 'always carry tags'):
            self.assertEqual(found['body'].count(rule), 1)
        self.assertEqual(found['history'][-1]['body'], 'Mine.\n- Hints win.')
        self.assertEqual(skills.load(store)['publishing']['revision'], found['revision'])

    def test_story_text_rule_is_rewritten_once(self):
        store = Store(FakeFirestore())
        skills.load(store)
        store.put(skills.SEEDED, {'at': 1, 'version': 5})
        doc = skills.fresh(skills.SEED['publishing'])
        doc['body'] = 'Mine.\n- Story text, place pill and stickers sit in free space above the subject, never on a face, person or object.'
        store.put('skills/publishing', doc)
        found = skills.load(store)['publishing']
        self.assertEqual(found['body'].count('one centred block'), 1)
        self.assertNotIn('stickers sit in free space', found['body'])
        self.assertEqual(skills.load(store)['publishing']['revision'], found['revision'])

    def test_old_migrations_do_not_rerun(self):
        store = Store(FakeFirestore())
        skills.load(store)
        before = store.get('skills/roman-social-networks')['body']
        store.put(skills.SEEDED, {'at': 1, 'version': 4})
        self.assertEqual(skills.load(store)['roman-social-networks']['body'], before)


if __name__ == '__main__':
    unittest.main()
