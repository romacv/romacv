import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

FUNCTIONS = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(FUNCTIONS))
import drafter
from fake_firestore import FakeFirestore
from store import Store


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.spec = json.loads((FUNCTIONS.parent/'examples/example-work.json').read_text())
        self.spec.update(targets=['tg-channel','threads'],at='2020-01-01T00:00:00+00:00')
        self.spec['threads_tag'] = 'iosdev'

    def valid(self, **extra):
        return {'slug':'a','kind':'work','at':'2026-01-01T00:00:00+00:00','targets':['threads'],'media':{},'copy':{'threads':'A concrete fact.\nNext comes the build.'},'approval':{},'threads_tag':'iosdev',**extra}

    def test_example_threads_and_personal_rules(self):
        self.assertEqual(drafter.validate(self.spec),[])
        self.spec['copy']['threads'] = 'Я'*251 + '?'
        self.assertTrue(any('exceeds' in e for e in drafter.validate(self.spec)))
        self.spec['copy']['threads'] = 'A fact.\n#one #two'
        self.assertTrue(any('hashtag' in e for e in drafter.validate(self.spec)))
        self.spec['copy']['threads'] = 'A fact.\nWould you pair breakfast with a sauna?'
        self.assertIn('threads: no closing question to readers unless Roman wrote it',drafter.validate(self.spec))
        self.assertEqual(drafter.validate(self.spec,owner='Would you pair breakfast with a sauna?'),[])
        self.assertIn('threads: no closing question to readers unless Roman wrote it',drafter.validate(self.spec,owner='Would you pair breakfast with a sauna? - bad',material='Breakfast post'))
        self.spec['targets'] = ['blog']
        self.assertTrue(any('supported targets' in e for e in drafter.validate(self.spec)))



    def test_target_outside_chosen_is_rejected(self):
        errors = drafter.validate(self.valid(),prepared=False,chosen=['tg-channel'])
        self.assertTrue(any('chosen' in e for e in errors))
        errors = drafter.validate(self.valid(),prepared=False,chosen=['threads'])
        self.assertFalse(any('chosen' in e for e in errors))




class FakeAnthropic:
    class APIStatusError(Exception):
        status_code = 500

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.messages = self

    def Anthropic(self, **kwargs):
        return self

    def create(self, **kwargs):
        self.calls.append(json.loads(json.dumps(kwargs['messages'])))
        block = Mock(type='text')
        block.text = self.replies.pop(0)
        return Mock(content=[block])





class ConversationTests(unittest.TestCase):
    def ask(self, store, replies, payload, follow, tag, count=0, current=None):
        store.put('items/a',{'revision':tag if current is None else current})
        fake = FakeAnthropic(replies)
        with patch.dict(sys.modules,{'anthropic':fake}):
            drafter.ask({'anthropic_key':'k'},payload,[],lambda value:[],thread=(store,'conversations/a',tag,count,('items/a','revision',tag)),follow=follow)
        return fake.calls

    def test_edit_resends_history_with_previous_draft(self):
        store = Store(FakeFirestore())
        self.ask(store,['{"v":1}'],{'slug':'a'},None,1)
        calls = self.ask(store,['{"v":2}'],{'slug':'a'},{'edits':['shorter']},2)
        roles = [m['role'] for m in calls[0]]
        self.assertEqual(roles,['user','assistant','user'])
        self.assertEqual(calls[0][1]['content'][0]['text'],'{"v":1}')
        self.assertIn('shorter',calls[0][2]['content'][0]['text'])
        self.assertEqual(calls[0][2]['content'][-1]['cache_control'],{'type':'ephemeral'})
        self.assertEqual(len(store.get('conversations/a')['turns']),4)
        self.ask(store,['{"v":3}'],{'slug':'a'},{'edits':['shorter']},2)
        self.assertEqual(len(store.get('conversations/a')['turns']),4)






if __name__ == '__main__':
    unittest.main()
