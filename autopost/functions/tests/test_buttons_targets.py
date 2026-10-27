import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fake_firestore import FakeFirestore
from store import Store, collect_keyboard
import drafter
import main as app
import media
import platforms


CONFIG = {'roman_user_id':1,'roman_chat_id':1,'channels':{'tg-channel':-100},'threads':{'id':'1'},'instagram':{'id':'2'}}
PLAIN = {'roman_user_id':1,'roman_chat_id':1}
POST = {'type':'bot_command','offset':0,'length':5}


def text_update(identifier, text, **extra):
    message = {'message_id':identifier,'from':{'id':1},'chat':{'id':1,'type':'private'},'text':text,**extra}
    return {'update_id':identifier,'message':message}


def post_update(identifier):
    return text_update(identifier,'/post',entities=[POST])


def tap(identifier, data, message_id=50, text='Добавил в пост.'):
    return {'update_id':identifier,'callback_query':{'id':str(identifier),'data':data,'message':{'message_id':message_id,'text':text}}}


class ButtonTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(FakeFirestore())

    def collecting(self, config=PLAIN):
        self.store.intake(post_update(10),config)
        return 'item-10', self.store.get('items/item-10')

    def test_tap_retires_the_keyboard(self):
        item_id, item = self.collecting()
        taps = []
        result = self.store.intake(tap(11,'cncl:' + item_id + ':' + item['batch_token']),PLAIN,taps)
        self.assertEqual(result,'Пост отменён.')
        self.assertEqual([(t['message_id'],t['line'],t['text']) for t in taps],[(50,'Отменено.','Добавил в пост.')])

    def test_done_tap_retires_with_status_line_stored_for_the_final_edit(self):
        item_id, item = self.collecting()
        self.store.intake(text_update(11,'Материал'),PLAIN)
        taps = []
        self.store.intake(tap(12,'done:' + item_id + ':' + item['batch_token']),PLAIN,taps)
        self.assertTrue(taps[0]['line'].startswith('Готовлю превью'))
        self.assertEqual(self.store.get('items/' + item_id)['status_message']['id'],50)

    def test_retire_taps_edits_the_tapped_message(self):
        with patch.object(app.platforms,'configure'), patch.object(app.platforms,'telegram') as telegram:
            app.retire_taps(PLAIN,self.store,[{'message_id':50,'text':'Добавил в пост.','line':'Отменено.','markup':None}])
        method, fields = telegram.call_args[0][:2]
        self.assertEqual(method,'editMessageText')
        self.assertEqual((fields['message_id'],fields['text']),(50,'Добавил в пост.\nОтменено.'))

    def test_new_ack_retires_the_previous_keyboard(self):
        self.store.put('items/item-10',{'status':'collecting','messages':[],'revision':0,'batch_token':'tok'})
        calls = []
        def telegram(method, fields, files=None):
            calls.append((method,fields))
            return {'message_id':len(calls) + 100}
        with patch.object(app.platforms,'telegram',side_effect=telegram), patch.object(app,'remove_keyboard') as remove:
            result = {'item':'item-10','text':'Добавил в пост.','reply_markup':collect_keyboard('item-10','tok')}
            app.send_collect(PLAIN,self.store,result)
            remove.assert_not_called()
            app.send_collect(PLAIN,self.store,result)
        remove.assert_called_once_with(PLAIN,{'preview_message':101})
        self.assertEqual(self.store.get('items/item-10')['keyboard_message'],102)


class ImageDocumentTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(FakeFirestore())
        self.document = {'file_id':'f','file_unique_id':'u','file_size':95000,'mime_type':'image/jpeg','file_name':'a.jpg'}

    def test_ack_counts_the_image_document_as_a_photo(self):
        self.store.intake(post_update(10),PLAIN)
        result = self.store.intake(text_update(11,'',document=self.document,caption='Подпись'),PLAIN)
        self.assertIn('1 фото',result['text'])

    def test_other_documents_are_not_photos(self):
        self.store.intake(post_update(10),PLAIN)
        result = self.store.intake(text_update(11,'',document={**self.document,'mime_type':'application/pdf'},caption='Подпись'),PLAIN)
        self.assertNotIn('фото',result['text'])

    def test_attachments_use_the_image_document_as_an_image(self):
        item = {'messages':[{'message_id':11,'document':self.document}]}
        values = media.attachments(item)
        self.assertEqual([(a['name'],a['kind'],a['file_id']) for a in values],[('11.jpg','photo','f')])


class DiscardTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(FakeFirestore())
        self.store.put('sessions/inbox',{'current':'alpha','awaiting':{'kind':'edit','slug':'alpha'}})
        self.store.put('items/item-a',{'status':'previewed','revision':1,'messages':[{'message_id':1,'text':'Alpha'}]})
        self.store.put('specs/alpha',{'item':'item-a','state':'previewed','spec':{'slug':'alpha','copy':{}},'preview_message':30,'hash':'abcdefgh'})
        self.store.put('items/item-b',{'status':'drafting','revision':1,'messages':[{'message_id':2,'text':'Beta'}]})
        self.store.put('specs/beta',{'item':'item-b','state':'drafting','spec':{'slug':'beta','copy':{}},'hash':'12345678'})

    def test_post_announces_and_rejects_open_drafts(self):
        result = self.store.intake(post_update(20),PLAIN)
        self.assertTrue(result['text'].startswith('Отменяю старые черновики: '))
        self.assertIn('Alpha',result['text'])
        self.assertIn('Beta',result['text'])
        for slug, item in (('alpha','item-a'),('beta','item-b')):
            self.assertEqual(self.store.get('specs/' + slug)['state'],'rejected')
            self.assertEqual(self.store.get('items/' + item)['status'],'rejected')
        session = self.store.get('sessions/inbox')
        self.assertEqual(session['collecting'],'item-20')
        self.assertNotIn('awaiting',session)
        self.assertNotIn('current',session)
        job = self.store.get('outbox/discard-20-alpha')['payload']
        self.assertEqual((job['step'],job['message'],job['line']),('remove-keyboard',30,'Черновик отменён.'))

    def test_post_without_open_drafts_is_unchanged(self):
        store = Store(FakeFirestore())
        result = store.intake(post_update(20),PLAIN)
        self.assertNotIn('Отменяю',result['text'])

    def test_late_preview_of_a_discarded_draft_is_dropped(self):
        self.store.intake(post_update(20),PLAIN)
        with patch.object(app.platforms,'telegram') as telegram:
            app.preview(PLAIN,self.store,None,'item-b',{'slug':'beta','targets':['tg-channel'],'copy':{'tg-channel':'x'}},{},Path('.'))
        telegram.assert_not_called()
        self.assertEqual(self.store.get('specs/beta')['state'],'rejected')

    def test_superseded_when_rejected(self):
        self.store.intake(post_update(20),PLAIN)
        self.assertTrue(app.superseded(self.store,{'item':'item-b','revision':2}))


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(FakeFirestore())
        self.store.intake(post_update(10),CONFIG)
        self.item = self.store.get('items/item-10')
        self.base = 'item-10:' + self.item['batch_token']

    def test_keyboard_offers_only_configured_targets_two_per_row(self):
        markup = collect_keyboard('item-10','tok',['story','channel','site'])
        rows = markup['inline_keyboard']
        self.assertEqual([[b['text'] for b in row] for row in rows],[['Сторис','Telegram-канал'],['Сайт'],['Готово','Отменить']])

    def test_first_ack_lists_configured_boxes_and_hint(self):
        store = Store(FakeFirestore())
        result = store.intake(post_update(10),CONFIG)
        texts = [b['text'] for row in result['reply_markup']['inline_keyboard'] for b in row]
        self.assertEqual(texts,['Сторис','Instagram-пост','Instagram Reels','Threads','Telegram-канал','Готово','Отменить'])
        self.assertTrue(result['text'].endswith('Отметь, куда пойдёт пост.'))
        self.assertTrue(all(len(b['callback_data'].encode()) <= 64 for row in result['reply_markup']['inline_keyboard'] for b in row))

    def test_no_boxes_when_nothing_is_configured(self):
        store = Store(FakeFirestore())
        result = store.intake(post_update(10),PLAIN)
        self.assertEqual([[b['text'] for b in row] for row in result['reply_markup']['inline_keyboard']],[['Готово','Отменить']])
        self.assertNotIn('Отметь',result['text'])

    def test_toggle_on_and_off_edits_markup_and_stores_chosen(self):
        taps = []
        self.assertEqual(self.store.intake(tap(11,'tg3:' + self.base),CONFIG,taps),'Отмечено: Threads')
        self.assertEqual(self.store.get('items/item-10')['chosen'],['threads'])
        self.assertIsNone(taps[0]['line'])
        marked = [b['text'] for row in taps[0]['markup']['inline_keyboard'] for b in row]
        self.assertIn('✓ Threads',marked)
        self.assertEqual(self.store.intake(tap(12,'tg3:' + self.base),CONFIG,taps),'Снято: Threads')
        self.assertEqual(self.store.get('items/item-10')['chosen'],[])
        self.assertNotIn('✓ Threads',[b['text'] for row in taps[0]['markup']['inline_keyboard'] for b in row])

    def test_retire_taps_edits_only_the_markup_for_a_toggle(self):
        markup = collect_keyboard('item-10','tok',['threads'],['threads'])
        with patch.object(app.platforms,'configure'), patch.object(app.platforms,'telegram') as telegram:
            app.retire_taps(CONFIG,self.store,[{'message_id':50,'text':'x','line':None,'markup':markup}])
        self.assertEqual(telegram.call_args[0][0],'editMessageReplyMarkup')

    def test_done_with_nothing_checked_gives_the_toast_and_keeps_the_keyboard(self):
        self.store.intake(text_update(11,'Материал'),CONFIG)
        taps = []
        result = self.store.intake(tap(12,'done:' + self.base),CONFIG,taps)
        self.assertEqual(result,'Отметь, куда пойдёт пост.')
        self.assertEqual(taps,[])
        self.assertEqual(self.store.get('items/item-10')['status'],'collecting')
        self.assertIsNone(self.store.get('outbox/draft-12'))

    def test_done_with_a_box_checked_queues_the_draft(self):
        self.store.intake(text_update(11,'Материал'),CONFIG)
        self.store.intake(tap(12,'tg3:' + self.base),CONFIG)
        taps = []
        self.store.intake(tap(13,'done:' + self.base),CONFIG,taps)
        self.assertEqual(self.store.get('items/item-10')['status'],'queued')
        self.assertEqual(len(taps),1)

    def test_nothing_chosen_without_boxes_is_unchanged(self):
        store = Store(FakeFirestore())
        store.intake(post_update(10),PLAIN)
        store.intake(text_update(11,'Материал'),PLAIN)
        token = store.get('items/item-10')['batch_token']
        store.intake(tap(12,'done:item-10:' + token),PLAIN)
        self.assertEqual(store.get('items/item-10')['status'],'queued')

    def test_unconfigured_toggle_is_stale(self):
        store = Store(FakeFirestore())
        store.intake(post_update(10),PLAIN)
        token = store.get('items/item-10')['batch_token']
        self.assertIsNone(store.intake(tap(11,'tg3:item-10:' + token),PLAIN))

    def valid(self, **extra):
        return {'slug':'a','kind':'work','at':'2026-01-01T00:00:00+00:00','targets':['threads'],'media':{},'copy':{'threads':'A concrete fact.\nWhat next?'},'approval':{},**extra}

    def test_drafter_rejects_a_target_outside_chosen(self):
        errors = drafter.validate(self.valid(),prepared=False,chosen=['tg-channel'])
        self.assertTrue(any('chosen' in e for e in errors))
        errors = drafter.validate(self.valid(),prepared=False,chosen=['threads'])
        self.assertFalse(any('chosen' in e for e in errors))
        errors = drafter.validate(self.valid(),prepared=False)
        self.assertFalse(any('chosen' in e for e in errors))

    def test_notes_field_is_validated(self):
        self.assertFalse(any('notes' in e for e in drafter.validate(self.valid(notes='Нет видео'),prepared=False)))
        self.assertTrue(any('notes' in e for e in drafter.validate(self.valid(notes='x' * 501),prepared=False)))

    def test_unsupported_target_message(self):
        reasons = app.unsupported(['ig-reel','ig-post','threads'],True,False)
        self.assertEqual(list(reasons),['ig-reel'])
        self.assertEqual(app.skipped_lines(reasons),['Не пойдёт в Instagram Reels: нужно видео.'])

    def test_restrict_stores_skipped_and_raises_when_nothing_fits(self):
        item = {'chosen':['ig-reel','threads']}
        self.store.put('items/item-10',{**self.item,'chosen':['ig-reel','threads']})
        allowed = app.restrict(CONFIG,self.store,{'item':'item-10'},item,True,False)
        self.assertEqual(allowed,['threads'])
        self.assertEqual(self.store.get('items/item-10')['skipped'],['Не пойдёт в Instagram Reels: нужно видео.'])
        only = {'chosen':['ig-reel']}
        with self.assertRaises(app.Error) as caught:
            app.restrict(CONFIG,self.store,{'item':'item-10'},only,True,False)
        self.assertIn('Не пойдёт в Instagram Reels',str(caught.exception))

    def test_restrict_without_choice_returns_none(self):
        self.assertIsNone(app.restrict(CONFIG,self.store,{'item':'item-10'},{},True,False))


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
        self.assertIn('clean-1.jpg',fake.calls[1][-1]['content'])

    def test_failure_logs_the_offending_paths(self):
        def validator(value):
            return ['Invalid media path ["media/x.jpg"]; use only these available_images paths exactly: ["clean-1.jpg"]']
        with self.assertLogs(level='ERROR') as logged:
            fake, failure = self.ask([json.dumps({'media':{}})] * 2,validator)
        self.assertIsInstance(failure,drafter.Error)
        self.assertIn('media/x.jpg',logged.output[0])


class HeicTests(unittest.TestCase):
    def test_heic_becomes_jpeg(self):
        import tempfile
        import pillow_heif
        from PIL import Image
        with tempfile.TemporaryDirectory() as folder:
            source, destination = Path(folder)/'in.heic', Path(folder)/'out.jpg'
            pillow_heif.from_pillow(Image.new('RGBA',(64,48),(200,10,10,255))).save(source)
            media.clean_image(source,destination)
            with Image.open(destination) as result:
                self.assertEqual((result.format,result.mode),('JPEG','RGB'))


if __name__ == '__main__':
    unittest.main()
