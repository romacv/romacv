from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fake_firestore import FakeFirestore
from store import Store
import main as app


class ScreenshotRoundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(FakeFirestore())
        self.config = {'roman_chat_id':1}
        self.spec = {'slug':'album','kind':'work','at':'2020-01-01T00:00:00+00:00','targets':['tg-channel'],'copy':{'tg-channel':'Text'},'approval':{}}
        self.store.put('items/item-1',{'status':'drafting','revision':1,'messages':[{'message_id':1,'text':'Тема'}],'slug':'album'})
        self.store.put('specs/album',{'state':'drafting','hash':'','item':'item-1','spec':self.spec})

    def commit_preview(self):
        item = self.store.get('items/item-1')
        self.store.put('specs/album',{'state':'drafting','hash':'','item':'item-1','spec':self.spec})
        def telegram(method,fields,files=None):
            return {'message_id':100}
        with patch.object(app.platforms,'telegram',side_effect=telegram), patch.object(app.platforms,'telegram_identity'), patch.object(app.media,'materialize'):
            app.preview(self.config,self.store,None,'item-1',self.spec,{'tg-channel':{}},self.root,revision=item['revision'])
        self.assertEqual(self.store.get('items/item-1')['status'],'previewed')

    def add_screenshot(self, message_id):
        note,shot = self.store.edit_note({'message_id':message_id,'photo':[{'file_id':'p' + str(message_id),'file_size':9}]})
        self.store.atomic(lambda tx:self.store.add_edit(tx,self.store.get('items/item-1',tx),'item-1',note,shot,'e' + str(message_id)))

    def media_edit(self):
        item = self.store.get('items/item-1')
        seen = []
        def intake(bucket,item_id,source,config,directory):
            seen.extend(m['message_id'] for m in source['messages'])
            return [{'name':str(m['message_id']) + '.jpg','path':self.root / (str(m['message_id']) + '.jpg')} for m in source['messages']]
        with patch.object(app.media,'intake',side_effect=intake), patch.object(app.media,'clean_image'):
            edits,shots = app.edit_media(self.config,self.store,None,{'item':'item-1','revision':item['revision']},item,self.root)
        return seen,edits,shots

    def test_each_round_sends_only_its_own_screenshot(self):
        self.add_screenshot(10)
        seen,edits,shots = self.media_edit()
        self.assertEqual((seen,len(shots),edits),([10],1,['Правка на приложенном скриншоте.']))
        self.commit_preview()
        self.add_screenshot(11)
        seen,edits,shots = self.media_edit()
        self.assertEqual((seen,len(shots)),([11],1))
        self.assertEqual(edits,['Правка со скриншотом, уже применена.','Правка на приложенном скриншоте.'])
        self.assertEqual([x['message_id'] for x in self.store.get('items/item-1')['edit_photos']],[10,11])

    def test_text_edit_after_a_screenshot_round_sends_no_images(self):
        self.add_screenshot(10)
        self.commit_preview()
        self.store.atomic(lambda tx:self.store.add_edit(tx,self.store.get('items/item-1',tx),'item-1','короче',None,'e12'))
        seen,edits,shots = self.media_edit()
        self.assertEqual((seen,shots,edits),([],[],['Правка со скриншотом, уже применена.','короче']))

    def test_captioned_screenshot_note_is_rendered_as_applied(self):
        note,shot = self.store.edit_note({'message_id':10,'caption':'Увеличь шрифт','photo':[{'file_id':'p','file_size':9}]})
        self.store.atomic(lambda tx:self.store.add_edit(tx,self.store.get('items/item-1',tx),'item-1',note,shot,'e10'))
        self.commit_preview()
        _,edits,_ = self.media_edit()
        self.assertEqual(edits,['Увеличь шрифт [скриншот уже применён]'])


if __name__ == '__main__':
    unittest.main()
