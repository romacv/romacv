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

    def test_secret_comparison_is_constant_time(self):
        with patch.object(app.hmac,'compare_digest',wraps=app.hmac.compare_digest) as compare:
            self.request(self.message(),secret='wrong')
        compare.assert_called_once_with(b'wrong',b'test-secret')

    def test_other_users_chats_and_groups_ignored(self):
        for update in (self.message(user=2),self.message(chat=2),self.message(kind='group')):
            self.assertEqual(self.request(update).status_code,200)
        self.assertEqual(self.store.db.values,{})
        self.task.assert_not_called()

    def test_duplicate_update_only_enqueues_once(self):
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

    def test_business_connection_is_pinned_to_owner_and_chat(self):
        update = {'update_id':60,'business_connection':{'id':'business','user':{'id':1},'user_chat_id':2}}
        self.request(update)
        self.assertIsNone(self.store.get('tokens/business'))
        update['business_connection']['user_chat_id'] = 1
        self.request(update)
        self.assertEqual(self.store.get('tokens/business')['business_connection_id'],'business')

    def test_methods_and_malformed_body(self):
        self.assertEqual(self.request({},method='GET').status_code,405)
        self.assertEqual(self.request({},method='OPTIONS').status_code,204)
        self.assertEqual(self.request(['invalid']).status_code,400)

    def test_upload_receipt_requires_origin_expiry_and_hmac(self):
        self.store.put('items/item-1',{'revision':1,'waiting_upload':True})
        expires = int(app.time.time())+900
        receipt = {'action':'uploaded','item':'item-1','revision':1,'expires':expires,'token':app.upload_mac(self.config,'item-1',1,expires)}
        self.assertEqual(self.request(receipt,origin='https://example.com').status_code,403)
        with patch.object(app.time,'time',return_value=expires+1):
            self.assertEqual(self.request(receipt,origin='https://www.resrom.com').status_code,403)
        receipt['token'] = 'forged'
        self.assertEqual(self.request(receipt,origin='https://www.resrom.com').status_code,403)
        receipt['token'] = app.upload_mac(self.config,'item-1',1,expires)
        self.assertEqual(self.request(receipt,origin='https://www.resrom.com').status_code,200)
        self.task.assert_called_once()


if __name__ == '__main__':
    unittest.main()
