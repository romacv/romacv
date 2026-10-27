import hashlib
import hmac
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import Mock, patch
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
        check = {'in':lambda x: x in value,'==':lambda x: x == value,'>=':lambda x: x is not None and x >= value}[operator]
        query.predicate = lambda row: check(row.get(field))
        return query

    def limit(self, count):
        return self


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
        self.store.put('items/a',{'messages':[{'text':'Первый '+'очень длинный '*5}],'status':'queued','created':now})
        self.store.put('items/b',{'messages':[{'text':'Второй'}],'status':'previewed','created':now,'updated':now})
        self.store.put('items/c',{'messages':[{'text':'Третий'}],'status':'previewed','created':now,'updated':now})
        self.store.put('items/d',{'messages':[{'text':'Четвёртый'}],'status':'collecting','created':now,'chosen':['channel']})
        self.store.put('renders/r',{'item':'a','mode':'draft','state':'running','bucket':'secret','progress':{'label':'Рендерю','since':now - 30},'steps':[{'label':'Скачиваю','since':now - 30,'ended':now - 20,'seconds':10,'vcpu_s':20,'gib_s':20},{'label':'Рендерю','since':now - 20}]})
        self.store.put('specs/b',{'item':'b','state':'previewed','targets':['tg-channel']})
        self.store.put('specs/c',{'item':'c','state':'done','published_at':now - 5,'results':{'tg-channel':{'url':'https://t.me/x/1'},'ig-post':{'url':'https://signed.example/secret?sig=1'}}})
        self.store.put('usage/' + time.strftime('%Y-%m',time.gmtime(now)),{'seconds':1800,'reservations':['x']})
        self.store.put('sessions/budget',{'costAmount':0.4,'budgetAmount':1,'costIntervalStart':'2026-10-01T07:00:00Z','currencyCode':'USD','at':now - 3600})
        used = {app.RUN_CPU:250000,app.RUN_MEMORY:1000,app.FIRESTORE_READS:1200}
        with patch.object(app,'monitoring_client',return_value=Mock()), patch.object(app,'series_total',side_effect=lambda client,project,metric,*rest: used[metric]):
            body = json.loads(self.call(signed()).get_data(as_text=True))
        self.assertTrue(body['active'][0]['title'].startswith('Первый очень длинный') and not body['active'][0]['title'].endswith('…'))
        self.assertEqual([(s['label'],s['seconds'],s['done'],round(s['vcpu_s'])) for s in body['active'][0]['steps']],[('Скачиваю',10,True,20),('Рендерю',20,False,40)])
        self.assertAlmostEqual(body['active'][0]['steps'][1]['paid'],40 * 0.000018 + 40 * 0.000002,4)
        self.assertEqual([(w['title'],w['status'],w['targets']) for w in body['waiting']],[('Четвёртый','collecting',['Telegram-канал']),('Второй','previewed',['Telegram-канал'])])
        self.assertEqual(body['published'][0]['title'],'Третий')
        self.assertEqual(body['published'][0]['links'],[{'name':'Telegram-канал','url':'https://t.me/x/1'}])
        rows = {row['key']:row for row in body['quota']}
        self.assertEqual((rows['cpu']['percent'],rows['cpu']['paid']),(104.2,round(10000 * 0.000018,4)))
        self.assertEqual((rows['reads']['used'],rows['speech']['used'],rows['speech']['paid']),(1200,30,0))
        self.assertEqual((body['budget']['cost'],body['budget']['budget']),(0.4,1))
        self.assertNotIn('secret',json.dumps(body))

    def test_monitoring_failure_degrades(self):
        self.store.put('sessions/budget',{'costAmount':0.1,'budgetAmount':1,'at':1})
        with patch.object(app,'monitoring_client',return_value=Mock()), patch.object(app,'series_total',side_effect=RuntimeError):
            response = self.call(signed())
        body = json.loads(response.get_data(as_text=True))
        self.assertEqual(response.status_code,200)
        self.assertEqual([row['key'] for row in body['quota']],['speech'])
        self.assertEqual(body['budget']['cost'],0.1)


if __name__ == '__main__':
    unittest.main()
