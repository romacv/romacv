import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

FUNCTIONS = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(FUNCTIONS))
from fake_firestore import FakeFirestore
from store import Store
import main as app


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.config = {'roman_user_id':1,'roman_chat_id':1,'webhook_secret':'test-secret'}
        self.store = Store(FakeFirestore())
        self.runtime = patch.object(app,'runtime',return_value=(self.config,self.store,None))
        self.runtime.start()
        self.addCleanup(self.runtime.stop)
        self.enqueue = patch.object(app,'enqueue')
        self.task = self.enqueue.start()
        self.addCleanup(self.enqueue.stop)
        self.pin = patch.object(app,'sync_pin')
        self.pin.start()
        self.addCleanup(self.pin.stop)
        self.telegram = patch.object(app.platforms,'telegram')
        self.telegram.start()
        self.addCleanup(self.telegram.stop)

    def request(self,value,secret='test-secret',method='POST',origin=None):
        request = Mock(method=method)
        request.headers = {'X-Telegram-Bot-Api-Secret-Token':secret}
        if origin:
            request.headers['Origin'] = origin
        request.get_json.return_value = value
        return app.webhook(request)

    def message(self,user=1,chat=1,kind='private'):
        return {'update_id':42,'message':{'message_id':7,'from':{'id':user},'chat':{'id':chat,'type':kind},'text':'Материал'}}

    def test_missing_wrong_and_unicode_secret(self):
        for secret in ('','wrong','неверно'):
            with self.subTest(secret=secret):
                response = self.request(self.message(),secret)
                self.assertEqual(response.status_code,403)
        self.assertEqual(self.store.db.values,{})
        self.task.assert_not_called()

    def test_other_users_chats_and_groups_ignored(self):
        for update in (self.message(user=2),self.message(chat=2),self.message(kind='group')):
            self.assertEqual(self.request(update).status_code,200)
        self.assertEqual(self.store.db.values,{})
        self.task.assert_not_called()

    def test_duplicate_update_only_enqueues_once(self):
        self.store.put('sessions/inbox',{'current':'example'})
        self.store.put('specs/example',{'state':'done','item':'item-1','spec':{'copy':{}},'results':{},'errors':{}})
        self.store.put('items/item-1',{'status':'previewed','revision':1,'messages':[]})
        patcher = patch.object(app.drafter,'classify',return_value={'kind':'caption','value':None})
        patcher.start()
        self.addCleanup(patcher.stop)
        first = self.request(self.message())
        self.assertEqual(json.loads(first.get_data())['method'],'sendMessage')
        self.request(self.message())
        self.task.assert_called_once()
        self.assertIsNotNone(self.store.get('updates/42'))

    def test_callback_checks_user_private_chat_and_hash(self):
        self.store.put('specs/example',{'state':'previewed','hash':'abcdefgh1234'})
        update = {'update_id':50,'callback_query':{'id':'callback','from':{'id':2},'message':{'chat':{'id':1,'type':'private'}},'data':'pub:example:abcdefgh'}}
        self.request(update)
        self.assertEqual(self.store.get('specs/example')['state'],'previewed')
        update['callback_query']['from']['id'] = 1
        response = self.request(update)
        self.assertEqual(json.loads(response.get_data())['method'],'answerCallbackQuery')
        self.assertEqual(self.store.get('specs/example')['state'],'approved')
        self.request(update)
        self.task.assert_called_once()





















if __name__ == '__main__':
    unittest.main()
