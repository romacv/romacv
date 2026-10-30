from pathlib import Path
import sys
import unittest

FUNCTIONS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FUNCTIONS))
from fake_firestore import FakeFirestore
from store import Store


class FlowFixTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(FakeFirestore())
        self.config = {'roman_chat_id':1}

    def say(self, identifier, text, **extra):
        return self.store.intake({'update_id':identifier,'message':{'message_id':identifier,'text':text,**extra}},self.config)

    def published(self, slug, item, **fields):
        self.store.put('items/' + item,{'status':'done','revision':1,'messages':[],'slug':slug,'title':fields.pop('title','Пост')})
        self.store.put('specs/' + slug,{'state':'done','item':item,'published_at':fields.pop('at',5),'spec':{'targets':['tg-channel','threads']},'results':{'tg-channel':{'url':'https://t.me/x/1','at':1}},'errors':{},**fields})


    def test_typed_accept_question_never_accepts_an_older_post_while_the_current_one_publishes(self):
        self.published('old','item-old',state='done')
        self.store.put('items/item-1',{'status':'approved','revision':1,'messages':[],'slug':'album','title':'Новый'})
        self.store.put('specs/album',{'state':'running','item':'item-1','spec':{'targets':['tg-channel']},'results':{},'errors':{}})
        self.store.put('sessions/inbox',{'current':'item-1'})
        self.store.classifier = lambda text,context:{'kind':'question','value':None}
        self.say(3,'принять')
        self.assertNotIn('accepted',self.store.get('specs/old'))


























if __name__ == '__main__':
    unittest.main()
