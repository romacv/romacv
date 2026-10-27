import hashlib
import hmac
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import Mock
from urllib.parse import urlencode

FUNCTIONS = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(FUNCTIONS))
from fake_firestore import FakeFirestore, Collection
from store import Store
import progress as app

TOKEN = '123:test-token'


def signed(user=1, age=0, token=TOKEN):
    fields = {'auth_date':str(int(time.time()) - age),'user':json.dumps({'id':user})}
    check = '\n'.join(key + '=' + fields[key] for key in sorted(fields))
    secret = hmac.new(b'WebAppData',token.encode(),hashlib.sha256).digest()
    return urlencode({**fields,'hash':hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()})


class Query(Collection):
    def where(self, field, operator, value):
        query = Query(self.db, self.path)
        check = {'in':lambda x: x in value,'>=':lambda x: x is not None and x >= value}[operator]
        query.predicate = lambda row: check(row.get(field))
        return query


class Database(FakeFirestore):
    def collection(self, path):
        return Query(self, path)

    def get_all(self, references):
        return [reference.get() for reference in references]


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(Database())
        self.config = {'roman_user_id':'1','telegram_bot_token':TOKEN}

    def call(self, init):
        request = Mock(method='GET')
        request.headers = {'Authorization':'tma ' + init}
        return app.respond(request,self.config,self.store)

    def test_init_data_gate(self):
        self.assertEqual(self.call(signed()).status_code,200)
        self.assertEqual(self.call(signed().replace('auth_date=','auth_date=1')).status_code,403)
        self.assertEqual(self.call(signed(user=2)).status_code,403)
        self.assertEqual(self.call(signed(age=90000)).status_code,401)
        self.assertEqual(self.call(signed(token='other')).status_code,403)

    def test_snapshot_failure(self):
        self.store.db.collection = Mock(side_effect=RuntimeError)
        self.assertEqual(self.call(signed()).status_code,503)

    def test_snapshot_shape(self):
        now = time.time()
        self.store.put('items/a',{'messages':[{'text':'Первый'}],'status':'queued','created':now})
        self.store.put('items/b',{'messages':[{'text':'Второй'}],'status':'previewed','created':now})
        self.store.put('items/c',{'messages':[{'text':'Третий'}],'status':'approved','created':now})
        self.store.put('renders/r',{'item':'a','mode':'draft','state':'running','bucket':'secret','progress':{'label':'Рендерю','since':now - 30},'steps':[{'label':'Скачиваю','since':now - 30},{'label':'Рендерю','since':now - 20}]})
        self.store.put('specs/c',{'item':'c','state':'done','published_at':now - 5})
        body = json.loads(self.call(signed()).get_data(as_text=True))
        self.assertEqual(body['active'][0]['title'],'Первый')
        self.assertEqual([(s['label'],s['seconds'],s['done']) for s in body['active'][0]['steps']],[('Скачиваю',10,True),('Рендерю',20,False)])
        self.assertEqual(body['waiting'],[{'title':'Второй','status':'previewed'}])
        self.assertEqual(body['published'][0]['title'],'Третий')
        self.assertNotIn('secret',json.dumps(body))


if __name__ == '__main__':
    unittest.main()
