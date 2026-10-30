import copy
import json
import re
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

FUNCTIONS = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(FUNCTIONS))
import agents
import drafter
import geo
import platforms as p
from fake_firestore import FakeFirestore
from store import Store

PLACES = {'places':[{'name':'Suka Espresso','city':'Ubud','country':'Indonesia','maps_url':'https://www.google.com/maps/search/?api=1&query=Suka%20Espresso%2C%20Ubud','evidence':'photo 1: sign','confidence':'high'}],'unknown':[]}
MEDIA = {'images':[{'name':'clean-1.jpg','alt':'Чашка кофе на деревянном столе','keep':True,'crop':'center','story':None}],'order':['clean-1.jpg']}


def copy_reply(text):
    return {'copy':{'tg-channel':text},'kind':'personal','at':datetime.now(timezone.utc).isoformat()}


class FakeStream:
    def __init__(self, raw):
        self.raw = raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        yield SimpleNamespace(type='message_start')
        yield SimpleNamespace(type='content_block_start',index=0,content_block=SimpleNamespace(type='text'))
        yield SimpleNamespace(type='content_block_delta',index=0,delta=SimpleNamespace(type='text_delta',text=self.raw))
        yield SimpleNamespace(type='content_block_stop',index=0)

    def get_final_message(self):
        usage = SimpleNamespace(input_tokens=10,output_tokens=5,cache_read_input_tokens=0,cache_creation_input_tokens=0,cache_creation=None,server_tool_use=None)
        return SimpleNamespace(content=[SimpleNamespace(type='text',text=self.raw)],stop_reason='end_turn',model='claude-sonnet-5-5',usage=usage)


class FakeAnthropic:
    class APIStatusError(Exception):
        status_code = 500

    def __init__(self, replies):
        self.replies = {name:list(values) for name, values in replies.items()}
        self.calls = []
        self.lock = threading.Lock()
        self.messages = self

    def Anthropic(self, **kwargs):
        return self

    def with_options(self, **kwargs):
        return self

    def stream(self, **kwargs):
        name = re.search(r'Role: (\w+) agent', kwargs['messages'][0]['content'][-1]['text']).group(1)
        with self.lock:
            self.calls.append((name, copy.deepcopy(kwargs)))
            reply = self.replies[name].pop(0)
        if isinstance(reply, Exception):
            raise reply
        return FakeStream(json.dumps(reply, ensure_ascii=False))


class AgentTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(FakeFirestore())
        self.config = {'anthropic_key':'k','roman_user_id':1,'roman_chat_id':1}
        p.configure(self.config, self.store, self.root)
        Image.new('RGB',(1200,900),(120,120,120)).save(self.root / 'clean-1.jpg','JPEG')
        self.images = [self.root / 'clean-1.jpg']
        self.store.put('items/i',{'revision':1,'status':'queued','slug':'trip','messages':[]})
        self.source = {'slug':'trip','messages':[{'text':'Отличное утро'}],'edits':[],'transcripts':[],'available_images':['clean-1.jpg'],'public_projects':[]}

    def run_draft(self, fake, validate=None):
        validate = validate or (lambda spec:drafter.validate(spec,prepared=False,owner='Отличное утро'))
        with patch.dict(sys.modules,{'anthropic':fake}):
            return agents.run_draft(self.config,self.store,'i',1,self.source,self.images,[],validate,None,None)

    def test_one_agent_failure_fails_the_draft_once(self):
        good = copy_reply('Тихое утро в Убуде и кофе с видом на рисовые террасы.')
        fake = FakeAnthropic({'places':[PLACES],'media':[RuntimeError('boom')],'copy':[good]})
        with self.assertRaises(drafter.Error) as caught:
            self.run_draft(fake)
        self.assertRegex(str(caught.exception),'[а-я]')
        self.assertEqual(self.store.get('conversations/i-places')['output'],PLACES)
        fake = FakeAnthropic({'places':[],'media':[MEDIA],'copy':[good]})
        spec = self.run_draft(fake)
        self.assertEqual([name for name, _ in fake.calls],['media','copy'])
        self.assertEqual(spec['copy'],good['copy'])
        self.assertTrue(self.store.get('conversations/i')['turns'])

    def test_flush_cancels_only_for_a_newer_revision(self):
        def flushed(steps):
            self.store.put('sessions/i',{'item':'i','state':'open','steps':steps})
            live = agents.Live(self.store,'i','draft',2)
            live.cancel = threading.Event()
            live.update('media',status='running')
            live.flush()
            return live
        live = flushed([{'id':'draft','state':'running','revision':1}])
        self.assertFalse(live.cancel.is_set())
        self.assertTrue(live.dirty)
        live = flushed([{'id':'draft','state':'running','revision':2},{'id':'draft','state':'running','revision':3}])
        self.assertTrue(live.cancel.is_set())
        live = flushed([{'id':'draft','state':'running','revision':2}])
        self.assertFalse(live.cancel.is_set())
        self.assertIn('media',self.store.get('sessions/i')['steps'][0]['agents'])

    def test_merge_keeps_roman_sending_order_unless_he_asked_for_another(self):
        source = {**self.source,'available_images':['clean-851.jpg','clean-852.jpg']}
        entries = [{'name':n,'alt':'a','keep':True} for n in source['available_images']]
        good = {'copy':{'tg-channel':'Тост'}}
        def order(media):
            spec, _ = agents.merge(None,source,PLACES,media,good,[],None,'x')
            return [i['path'] for i in spec['media']['images']]
        self.assertEqual(order({'images':entries,'order':['clean-852.jpg','clean-851.jpg']}),['clean-851.jpg','clean-852.jpg'])
        self.assertEqual(order({'images':entries,'order':['clean-852.jpg','clean-851.jpg'],'explicit':True}),['clean-852.jpg','clean-851.jpg'])

    def test_merge_produces_a_spec_the_validator_accepts(self):
        good = copy_reply('Тихое утро в Убуде и кофе с видом на рисовые террасы.')
        found = {'name':'Suka Espresso','address':'Ubud','lat':-8.5,'lng':115.26,'maps_url':'https://maps.google.com/?cid=1'}
        with patch.object(geo,'place',return_value=found):
            spec, errors = agents.merge(None,self.source,PLACES,MEDIA,good,[],None,'Отличное утро')
        self.assertEqual(errors,{})
        self.assertEqual(spec['place'],found)
        self.assertEqual(spec['media']['images'],[{'path':'clean-1.jpg','alt':MEDIA['images'][0]['alt']}])
        bad = copy_reply('Тихое утро в Убуде. Link in bio.')
        fake = FakeAnthropic({'places':[PLACES],'media':[MEDIA],'copy':[bad,good]})
        result = self.run_draft(fake)
        self.assertEqual(drafter.validate(result,prepared=False,owner='Отличное утро'),[])
        self.assertEqual(sorted(name for name, _ in fake.calls),['copy','copy','media','places'])
        self.assertEqual(self.store.get('conversations/i-copy')['output'],good)
        first = {name:request for name, request in fake.calls[:3]}
        prefixes = [json.dumps([r['tools'],r['system'],r['messages'][0]['content'][:-1]],sort_keys=True) for r in first.values()]
        self.assertEqual(len(set(prefixes)),1)
        self.assertEqual(sum(1 for name, _ in fake.calls if name == 'media'),1)
        self.assertIn('Correct these validation errors',json.dumps(fake.calls[-1][1]['messages'][-1]))


if __name__ == '__main__':
    unittest.main()
