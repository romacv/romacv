from pathlib import Path
import sys
import time
import unittest
from unittest.mock import Mock, patch

FUNCTIONS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FUNCTIONS))
from fake_firestore import FakeFirestore
import main as app
from store import Store


class Base(unittest.TestCase):
    def setUp(self):
        self.store = Store(FakeFirestore())
        self.config = {'roman_chat_id':1}

    def drafting(self):
        self.store.put('items/item-9',{'status':'drafting','revision':8,'slug':'item-9','messages':[{'message_id':1,'text':'Тема'}]})
        self.store.put('sessions/inbox',{'current':'item-9'})


class StalledDraftTests(Base):
    def test_stopped_draft_keeps_material_and_offers_a_retry(self):
        self.store.put('items/item-1',{'status':'drafting','revision':3,'slug':'item-1','updated':time.time() - 10 ** 6,'messages':[{'message_id':n,'photo':[{'file_id':str(n)}]} for n in range(7)]})
        self.store.put('specs/item-1',{'state':'drafting','item':'item-1','updated':time.time() - 10 ** 6})
        sent = []
        with patch.object(app,'tell',side_effect=lambda config,text,**extra:sent.append((text,extra))):
            app.stop_stale(self.config,self.store)
        self.assertEqual(len(self.store.get('items/item-1')['messages']),7)
        text,extra = sent[0]
        self.assertIn('Материал сохранён',text)
        self.assertEqual(extra['reply_markup']['inline_keyboard'][0][0]['callback_data'],'again:item-1:3')



    def test_one_draft_starts_one_prepare_render(self):
        item = {'montage':True}
        payload = {'item':'item-1','revision':2}
        inputs = [{'kind':'video','name':'a.mp4','manifest':{}}]
        bucket = Mock()
        bucket.name = 'b'
        config = {'roman_chat_id':1,'worker_url':'u','runtime_service_account':'s'}
        with patch.object(app.firebase_admin,'get_app',return_value=Mock(project_id='p')), patch.object(app,'start_job') as start:
            self.store.put('items/item-1',{'status':'drafting','revision':2})
            app.start_preparation(config,self.store,bucket,payload,item,inputs)
            app.start_preparation(config,self.store,bucket,payload,item,inputs)
        self.assertEqual(start.call_count,1)




























if __name__ == '__main__':
    unittest.main()
