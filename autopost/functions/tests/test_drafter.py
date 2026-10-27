import json
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

FUNCTIONS = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(FUNCTIONS))
import drafter
from fake_firestore import FakeFirestore
from store import Store
import platforms as p


class ValidatorTests(unittest.TestCase):
    def setUp(self):
        self.spec = json.loads((FUNCTIONS.parent/'examples/example-work.json').read_text())
        self.spec.update(targets=['tg-channel','threads'],at='2020-01-01T00:00:00+00:00')
        self.spec.pop('blog')
        self.spec['threads_tag'] = 'iosdev'

    def valid(self, **extra):
        return {'slug':'a','kind':'work','at':'2026-01-01T00:00:00+00:00','targets':['threads'],'media':{},'copy':{'threads':'A concrete fact.\nWhat next?'},'approval':{},'threads_tag':'iosdev',**extra}

    def test_example_threads_and_personal_rules(self):
        self.assertEqual(drafter.validate(self.spec),[])
        self.spec['copy']['threads'] = 'Я'*251 + '?'
        self.assertTrue(any('exceeds' in e for e in drafter.validate(self.spec)))
        self.spec['copy']['threads'] = 'A fact.\n#one #two'
        self.assertTrue(any('question' in e for e in drafter.validate(self.spec)))
        spec = json.loads((FUNCTIONS.parent/'examples/example-work.json').read_text())
        spec['kind'] = 'personal'
        self.assertTrue(any('work' in e for e in drafter.validate(spec)))

    def test_personal_channel_and_threads_copy_must_match(self):
        spec = {'slug':'a','kind':'personal','at':datetime.now(timezone.utc).isoformat(),'targets':['tg-channel','threads'],'media':{},'copy':{'tg-channel':'Привет','threads':'Пока'},'approval':{},'threads_tag':'бали'}
        self.assertIn('personal: tg-channel and threads copy must be identical',drafter.validate(spec,copy_only=True))
        spec['copy']['threads'] = 'Привет'
        self.assertNotIn('personal: tg-channel and threads copy must be identical',drafter.validate(spec,copy_only=True))

    def test_target_outside_chosen_is_rejected(self):
        errors = drafter.validate(self.valid(),prepared=False,chosen=['tg-channel'])
        self.assertTrue(any('chosen' in e for e in errors))
        errors = drafter.validate(self.valid(),prepared=False,chosen=['threads'])
        self.assertFalse(any('chosen' in e for e in errors))

    def test_notes_tag_and_owner_wording(self):
        self.assertEqual(drafter.validate(self.valid(notes='Блог убран: он только для работы.'),prepared=False),[])
        for note in ('Blog removed: the schema has no crop field.','Квадрат делает renderer.'):
            self.assertTrue(any('notes' in e for e in drafter.validate(self.valid(notes=note),prepared=False)),note)
        self.assertIn('threads needs threads_tag',drafter.validate({k:v for k,v in self.valid().items() if k != 'threads_tag'},prepared=False))
        self.assertNotIn('threads needs threads_tag',drafter.validate({k:v for k,v in self.valid().items() if k != 'threads_tag'},prepared=False,copy_only=True))
        for tag in ('#a','a.b','a&b','a b'):
            self.assertTrue(any('threads_tag' in e for e in drafter.validate(self.valid(threads_tag=tag),prepared=False)),tag)
        spec = {'slug':'a','kind':'personal','at':datetime.now(timezone.utc).isoformat(),'targets':['tg-story'],'media':{},'copy':{'tg-story':'Back home to 📍Bali'},'approval':{}}
        self.assertTrue(drafter.validate(spec,copy_only=True))
        self.assertEqual(drafter.validate(spec,copy_only=True,owner='надо "Back home to 📍Bali"'),[])
        self.assertEqual(drafter.overlay_errors({'copy':spec['copy'],'overlay':{'tg-story':{'text':'Back home to 📍Bali','place':'📍Bali','anchor':[0.5,0.5],'align':'center'}}},['tg-story'],[{'path':'a.jpg','alt':'a'}]),[])

    def test_overlay_rules(self):
        images = [{'path':'a.jpg','alt':'a'}]
        def overlay(**changes):
            return {'text':'Сегодня утро начинаю в Suka Espresso Ubud','place':'Suka Espresso Ubud','anchor':[0.5,0.5],'align':'center',**changes}
        def errors(value, targets=('ig-story','tg-story'), photos=images):
            return drafter.overlay_errors({'overlay':{'ig-story':value}},list(targets),photos)
        self.assertEqual(errors(overlay()),[])
        for bad in (overlay(text=''),overlay(text='x'*61),overlay(anchor=[1.2,0.5]),overlay(align='top')):
            self.assertTrue(errors(bad),bad)
        self.assertTrue(errors(overlay(),targets=('tg-story',)))
        self.assertTrue(errors(overlay(),photos=[]))
        spec = {'copy':{'tg-story':'Утро в Убуде'},'overlay':{'tg-story':overlay(text='Другая строка',place=None)}}
        self.assertEqual(drafter.overlay_errors(spec,['tg-story'],images),['tg-story: overlay text must equal the target copy'])


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


class MediaPathReaskTests(unittest.TestCase):
    def ask(self, replies, validator):
        fake = FakeAnthropic(replies)
        with patch.dict(sys.modules,{'anthropic':fake}):
            try:
                return fake, drafter.ask({'anthropic_key':'k'},{'slug':'a'},[],validator)
            except drafter.Error as failure:
                return fake, failure

    def test_reask_names_the_real_paths_and_then_succeeds(self):
        def validator(value):
            return [] if value['media']['images'] == [{'path':'clean-1.jpg'}] else ['Invalid media path ["media/x.jpg"]; use only these available_images paths exactly: ["clean-1.jpg"]']
        fake, value = self.ask([json.dumps({'media':{'images':[{'path':'media/x.jpg'}]}}),json.dumps({'media':{'images':[{'path':'clean-1.jpg'}]}})],validator)
        self.assertEqual(value['media']['images'],[{'path':'clean-1.jpg'}])
        self.assertEqual(len(fake.calls),2)
        self.assertIn('clean-1.jpg',json.dumps(fake.calls[1][-1]['content']))


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

    def test_stale_save_is_skipped_and_fresh_edits_follow_stored_count(self):
        store = Store(FakeFirestore())
        self.ask(store,['{"v":1}'],{'slug':'a'},None,3,1)
        self.assertEqual(store.seen('conversations/a',4),1)
        self.ask(store,['{"v":2}'],{'slug':'a'},{'edits':['x']},2,2,3)
        self.assertEqual(store.get('conversations/a')['tag'],3)
        self.assertEqual(store.seen('conversations/a',3),0)

    def test_discard_clears_conversation(self):
        store = Store(FakeFirestore())
        self.ask(store,['{}'],{'slug':'a'},None,1)
        store.atomic(lambda tx:store.discard(tx,[({'id':'a','item':None,'specs':[]},'')],{},'x'))
        self.assertIsNone(store.get('conversations/a'))


class DraftCorrectionTests(unittest.TestCase):
    def test_packaged_prompt_and_exactly_one_correction(self):
        response = Mock(content=[Mock(type='text',text='{}')])
        client = Mock()
        client.messages.create.return_value = response
        import anthropic
        with patch.object(anthropic,'Anthropic',return_value=client), patch.object(drafter,'language_errors'):
            validator = Mock(side_effect=[['missing field'],[]])
            self.assertEqual(drafter.ask({'anthropic_key':'unused'}, {'text':'input'}, [], validator),{})
        self.assertEqual(client.messages.create.call_count,2)
        self.assertIn('EDL JSON schema',client.messages.create.call_args.kwargs['system'][0]['text'])
        client.reset_mock()
        with patch.object(anthropic,'Anthropic',return_value=client):
            with self.assertRaises(p.Error):
                drafter.ask({'anthropic_key':'unused'},{},[],lambda value:['invalid'])
        self.assertEqual(client.messages.create.call_count,2)


if __name__ == '__main__':
    unittest.main()
