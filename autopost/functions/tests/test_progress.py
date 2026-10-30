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

    def call(self, init, view=None):
        request = Mock(method='GET')
        request.args = {'view':view} if view else {}
        request.headers = {'Authorization':'tma ' + init}
        return app.respond(request,self.config,self.store)

    def test_init_data_gate(self):
        self.assertEqual(self.call(signed()).status_code,200)
        self.assertEqual(self.call(signed().replace('auth_date=','auth_date=1')).status_code,403)
        self.assertEqual(self.call(signed(user=2)).status_code,403)
        self.assertEqual(self.call(signed(age=90000)).status_code,401)
        self.assertEqual(self.call(signed(token='other')).status_code,403)














if __name__ == '__main__':
    unittest.main()
