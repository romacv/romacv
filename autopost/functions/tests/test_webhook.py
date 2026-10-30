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

    def toggle(self, identifier='9'):
        return {'update_id':int(identifier),'callback_query':{'id':identifier,'from':{'id':1},'message':{'message_id':77,'chat':{'id':1,'type':'private'},'text':'Новый пост'},'data':'tg4:item-1:tok'}}

    def test_channel_toggle_checks_and_a_redelivered_tap_repaints_the_keyboard(self):
        self.config.update(channels={'tg-channel':1})
        self.store.put('items/item-1',{'status':'collecting','revision':1,'messages':[],'batch_token':'tok','slug':'item-1'})
        sent = []
        with patch.object(app.platforms,'configure'), patch.object(app.platforms,'telegram',side_effect=lambda method,fields,files=None:sent.append((method,fields))):
            answer = json.loads(self.request(self.toggle('9')).get_data())
            self.assertNotIn('text',answer)
            self.assertEqual(self.store.get('items/item-1')['chosen'],['channel'])
            sent.clear()
            self.request(self.toggle('9'))
        buttons = [b['text'] for row in sent[-1][1]['reply_markup']['inline_keyboard'] for b in row]
        self.assertEqual(sent[-1][0],'editMessageReplyMarkup')
        self.assertIn('☑︎ Telegram-канал',buttons)

    def test_slow_telegram_edit_does_not_hold_the_webhook(self):
        self.config.update(channels={'tg-channel':1})
        self.store.put('items/item-1',{'status':'collecting','revision':1,'messages':[],'batch_token':'tok','slug':'item-1'})
        release = app.threading.Event()
        self.addCleanup(release.set)
        with patch.object(app,'WEBHOOK_EDIT_SECONDS',0.2), patch.object(app.platforms,'configure'), patch.object(app.platforms,'telegram',side_effect=lambda *a,**k:release.wait(30)):
            started = app.time.time()
            response = self.request(self.toggle('11'))
        self.assertLess(app.time.time()-started,5)
        self.assertNotIn('text',json.loads(response.get_data()))

    def test_slow_collect_send_falls_back_inline_and_a_late_landing_is_deleted(self):
        self.store.put('items/item-1',{'status':'collecting','revision':1,'messages':[],'batch_token':'tok','slug':'item-1'})
        release = app.threading.Event()
        self.addCleanup(release.set)
        calls = []
        def telegram(method,fields,files=None):
            calls.append(method)
            if method == 'sendMessage':
                release.wait(30)
                return {'message_id':501}
        result = {'item':'item-1','text':'Принято','reply_markup':{'inline_keyboard':[]}}
        update = {'update_id':70,'message':{'message_id':5,'text':'ещё','from':{'id':1},'chat':{'id':1,'type':'private'}}}
        with patch.object(app,'WEBHOOK_EDIT_SECONDS',0.2), patch.object(app.platforms,'configure'), patch.object(app.platforms,'telegram',side_effect=telegram), patch.object(self.store,'intake',return_value=result):
            answer = json.loads(self.request(update).get_data())
            self.assertEqual(answer['method'],'sendMessage')
            release.set()
            for _ in range(50):
                if 'deleteMessage' in calls:
                    break
                app.time.sleep(0.1)
        self.assertIn('deleteMessage',calls)

    def test_failed_keyboard_edit_is_retried_once(self):
        self.store.put('items/item-1',{'status':'collecting','revision':1,'messages':[],'batch_token':'tok','slug':'item-1','chosen':['channel']})
        attempts = []
        def telegram(method,fields,files=None):
            attempts.append(fields)
            if len(attempts) == 1:
                raise RuntimeError('timeout')
        with patch.object(app.platforms,'configure'), patch.object(app.platforms,'telegram',side_effect=telegram):
            app.retire_taps(self.config,self.store,[{'message_id':77,'line':None,'item':'item-1','token':'tok','offers':[],'markup':None}])
        self.assertEqual(len(attempts),2)

    def test_refusal_is_a_multiline_alert_within_200_characters(self):
        self.config['instagram'] = {'user':1}
        self.store.put('items/item-1',{'status':'collecting','revision':1,'messages':[{'message_id':1,'photo':[{'file_id':'p'}]}],'batch_token':'tok','slug':'item-1','chosen':['ig-reel']})
        update = {'update_id':80,'callback_query':{'id':'80','from':{'id':1},'message':{'message_id':77,'chat':{'id':1,'type':'private'}},'data':'done:item-1:tok'}}
        with patch.object(app.platforms,'configure'), patch.object(app.platforms,'telegram') as telegram:
            answer = json.loads(self.request(update).get_data())
        self.assertTrue(answer['show_alert'])
        self.assertLessEqual(len(answer['text']),200)

    def test_successful_done_tap_is_a_silent_toast(self):
        self.store.put('items/item-1',{'status':'collecting','revision':1,'messages':[{'message_id':1,'photo':[{'file_id':'p'}]}],'batch_token':'tok','slug':'item-1'})
        update = {'update_id':83,'callback_query':{'id':'83','from':{'id':1},'message':{'message_id':77,'chat':{'id':1,'type':'private'}},'data':'done:item-1:tok'}}
        with patch.object(app.platforms,'configure'), patch.object(app.platforms,'telegram'):
            answer = json.loads(self.request(update).get_data())
        self.assertFalse(answer.get('show_alert'))

    def test_text_message_gets_a_reaction_and_typing(self):
        with patch.object(app.platforms,'telegram') as telegram:
            self.request(self.message())
        self.assertEqual([call.args for call in telegram.call_args_list[:2]],[('setMessageReaction',{'chat_id':1,'message_id':7,'reaction':[{'type':'emoji','emoji':'👀'}]}),('sendChatAction',{'chat_id':1,'action':'typing'})])

    def test_one_reaction_per_album(self):
        with patch.object(app.platforms,'telegram',return_value={'message_id':5}) as telegram:
            for number in (1,2,3):
                self.request({'update_id':200 + number,'message':{'message_id':number,'media_group_id':'g','photo':[{'file_id':'p' + str(number)}],'from':{'id':1},'chat':{'id':1,'type':'private'}}})
        self.assertEqual([call.args[0] for call in telegram.call_args_list].count('setMessageReaction'),1)

    def test_stale_button_alerts(self):
        update = {'update_id':81,'callback_query':{'id':'81','from':{'id':1},'message':{'message_id':77,'chat':{'id':1,'type':'private'}},'data':'done:gone:tok'}}
        answer = json.loads(self.request(update).get_data())
        self.assertTrue(answer['show_alert'])
        self.assertIn('устарела',answer['text'])

    def test_album_follow_up_refreshes_the_collect_message(self):
        self.store.put('items/item-1',{'status':'collecting','revision':1,'messages':[{'message_id':1,'media_group_id':'g','photo':[{'file_id':'p'}]}],'batch_token':'tok','slug':'item-1','keyboard_message':900,'keyboard_group':'g'})
        self.store.put('albums/g',{'item':'item-1','at':1})
        self.store.put('sessions/inbox',{'collecting':'item-1'})
        sent = []
        update = {'update_id':82,'message':{'message_id':2,'media_group_id':'g','video':{'file_id':'v'},'from':{'id':1},'chat':{'id':1,'type':'private'}}}
        with patch.object(app.platforms,'configure'), patch.object(app.platforms,'telegram',side_effect=lambda method,fields,files=None:sent.append((method,fields))):
            self.request(update)
        sent = [call for call in sent if call[0] not in ('setMessageReaction','sendChatAction')]
        self.assertEqual(sent[0][0],'editMessageText')
        self.assertEqual(sent[0][1]['message_id'],900)
        self.assertNotIn('1 фото',sent[0][1]['text'])

    def test_album_parts_after_done_join_the_post_and_another_album_starts_a_new_one(self):
        self.store.put('items/item-1',{'status':'queued','revision':2,'messages':[{'message_id':1,'media_group_id':'g','photo':[{'file_id':'p'}]}],'batch_token':'tok','slug':'item-1'})
        self.store.put('albums/g',{'item':'item-1','at':1})
        self.store.put('sessions/inbox',{'current':'item-1'})
        chat = {'from':{'id':1},'chat':{'id':1,'type':'private'}}
        with patch.object(app.platforms,'configure'), patch.object(app.platforms,'telegram',return_value={'message_id':5}):
            self.request({'update_id':60,'message':dict({'message_id':2,'media_group_id':'g','photo':[{'file_id':'p2'}]},**chat)})
            self.request({'update_id':61,'message':dict({'message_id':3,'media_group_id':'g','video':{'file_id':'v'}},**chat)})
            joined = self.store.get('items/item-1')
            self.assertIsNone(self.store.get('items/item-60'))
            self.assertIsNone(self.store.get('items/item-61'))
            self.assertNotIn('edit_photos',joined)
            self.assertEqual([m['message_id'] for m in joined['messages']],[1,2,3])
            self.request({'update_id':62,'message':dict({'message_id':4,'media_group_id':'other','video':{'file_id':'v2'}},**chat)})
        self.assertIsNotNone(self.store.get('items/item-62'))

    def test_late_album_part_joins_its_own_item_when_current_moved(self):
        self.store.put('items/item-1',{'status':'drafting','revision':2,'messages':[{'message_id':1,'media_group_id':'g','photo':[{'file_id':'p'}]}],'batch_token':'tok','slug':'item-1'})
        self.store.put('items/item-2',{'status':'queued','revision':2,'messages':[{'message_id':9,'photo':[{'file_id':'q'}]}],'batch_token':'tok','slug':'item-2'})
        self.store.put('albums/g',{'item':'item-1','at':1})
        self.store.put('sessions/inbox',{'current':'item-2'})
        chat = {'from':{'id':1},'chat':{'id':1,'type':'private'}}
        with patch.object(app.platforms,'configure'), patch.object(app.platforms,'telegram',return_value={'message_id':5}):
            self.request({'update_id':70,'message':dict({'message_id':2,'media_group_id':'g','video':{'file_id':'v'}},**chat)})
        self.assertEqual([m['message_id'] for m in self.store.get('items/item-1')['messages']],[1,2])
        self.assertEqual((self.store.get('items/item-1')['status'],self.store.get('items/item-1')['revision'],'edits' in self.store.get('items/item-1')),('queued',3,False))
        self.assertEqual(len(self.store.get('items/item-2')['messages']),1)
        self.assertIsNone(self.store.get('items/item-70'))

    def test_late_album_part_joins_a_previewed_spec_without_taking_current(self):
        self.store.put('items/item-1',{'status':'previewed','revision':2,'messages':[{'message_id':1,'media_group_id':'g','photo':[{'file_id':'p'}]}],'batch_token':'tok','slug':'item-1'})
        self.store.put('specs/item-1',{'state':'previewed','item':'item-1','slug':'item-1','hash':'h','spec':{'targets':[]}})
        self.store.put('albums/g',{'item':'item-1','at':1})
        self.store.put('sessions/inbox',{'current':'item-2'})
        chat = {'from':{'id':1},'chat':{'id':1,'type':'private'}}
        with patch.object(app.platforms,'configure'), patch.object(app.platforms,'telegram',return_value={'message_id':5}):
            self.request({'update_id':73,'message':dict({'message_id':2,'media_group_id':'g','photo':[{'file_id':'p2'}]},**chat)})
        self.assertEqual([m['message_id'] for m in self.store.get('items/item-1')['messages']],[1,2])
        self.assertEqual(self.store.get('specs/item-1')['state'],'drafting')
        self.assertEqual(self.store.get('sessions/inbox')['current'],'item-2')
        self.assertIsNotNone(self.store.get('updates/73'))

    def test_late_album_part_after_approval_is_refused_once_without_a_new_post(self):
        self.store.put('items/item-1',{'status':'drafting','revision':2,'messages':[{'message_id':1,'media_group_id':'g','photo':[{'file_id':'p'}]}],'batch_token':'tok','slug':'item-1'})
        self.store.put('specs/item-1',{'state':'approved','item':'item-1','slug':'item-1','hash':'h','approved_hash':'h','spec':{'targets':[]}})
        self.store.put('albums/g',{'item':'item-1','at':1})
        self.store.put('sessions/inbox',{'current':'item-1'})
        chat = {'from':{'id':1},'chat':{'id':1,'type':'private'}}
        with patch.object(app.platforms,'configure'), patch.object(app.platforms,'telegram',return_value={'message_id':5}):
            first = self.request({'update_id':71,'message':dict({'message_id':2,'media_group_id':'g','photo':[{'file_id':'p2'}]},**chat)})
            second = self.request({'update_id':72,'message':dict({'message_id':3,'media_group_id':'g','photo':[{'file_id':'p3'}]},**chat)})
        self.assertIn('после «Опубликовать»',first.get_data(as_text=True))
        self.assertNotIn('после «Опубликовать»',second.get_data(as_text=True))
        self.assertEqual(len(self.store.get('items/item-1')['messages']),1)
        self.assertIsNone(self.store.get('items/item-71'))
        self.assertIsNone(self.store.get('items/item-72'))

    def test_bounded_sends_work_without_patched_configure(self):
        self.store.put('items/item-1',{'status':'collecting','revision':1,'messages':[],'batch_token':'tok','slug':'item-1'})
        seen = []
        def telegram(method,fields,files=None):
            seen.append(app.platforms.CONTEXT.get()[0])
        with patch.object(app.platforms,'telegram',side_effect=telegram):
            self.request(self.toggle('90'))
        self.assertTrue(seen)

    def test_album_parts_arriving_before_the_first_send_completes_make_one_collect_message(self):
        self.store.put('items/item-1',{'status':'collecting','revision':0,'messages':[],'batch_token':'tok','slug':'item-1'})
        self.store.put('sessions/inbox',{'collecting':'item-1'})
        sends = []
        def telegram(method,fields,files=None):
            sends.append(method)
            return {'message_id':5}
        with patch.object(app.platforms,'telegram',side_effect=telegram), patch.object(app,'send_collect') as collect:
            for number in (1,2,3):
                self.request({'update_id':100 + number,'message':{'message_id':number,'media_group_id':'g','photo':[{'file_id':'p' + str(number)}],'from':{'id':1},'chat':{'id':1,'type':'private'}}})
        self.assertEqual(collect.call_count,1)
        self.assertNotIn('sendMessage',sends)

    def test_album_after_post_edits_the_one_collect_message_in_place(self):
        self.store.put('items/item-1',{'status':'collecting','revision':1,'messages':[{'message_id':1,'media_group_id':'g','photo':[{'file_id':'p'}]}],'batch_token':'tok','slug':'item-1','keyboard_message':900,'added':{'at':app.time.time()}})
        self.store.put('sessions/inbox',{'collecting':'item-1'})
        update = {'update_id':92,'message':{'message_id':2,'media_group_id':'g','video':{'file_id':'v'},'from':{'id':1},'chat':{'id':1,'type':'private'}}}
        with patch.object(app.platforms,'telegram',return_value={'message_id':5}) as telegram:
            self.request(update)
        calls = [call.args for call in telegram.call_args_list if call.args[0] not in ('setMessageReaction','sendChatAction')]
        self.assertEqual((calls[0][0],calls[0][1]['message_id']),('editMessageText',900))

    def test_single_photos_in_a_burst_make_one_bottom_keyboard_message_with_the_count(self):
        self.store.put('items/item-1',{'status':'collecting','revision':0,'messages':[],'batch_token':'tok','slug':'item-1','keyboard_message':900})
        self.store.put('sessions/inbox',{'collecting':'item-1'})
        calls = []
        def telegram(method,fields,files=None):
            calls.append((method,fields))
            return {'message_id':5}
        with patch.object(app.platforms,'configure'), patch.object(app.platforms,'telegram',side_effect=telegram):
            for number in (1,2,3):
                self.request({'update_id':300 + number,'message':{'message_id':number,'photo':[{'file_id':'p' + str(number)}],'from':{'id':1},'chat':{'id':1,'type':'private'}}})
        calls = [call for call in calls if call[0] not in ('setMessageReaction','sendChatAction')]
        self.assertEqual([call[0] for call in calls].count('sendMessage'),1)
        self.assertEqual(self.store.get('items/item-1')['keyboard_message'],5)
        self.assertTrue([call for call in calls if call[1].get('message_id') == 900 and call[0] != 'editMessageText'])
        edits = [call[1] for call in calls if call[0] == 'editMessageText' and call[1]['message_id'] == 5]
        self.assertIn('3 фото',edits[-1]['text'])
        self.assertIn('Готово',edits[-1]['text'])

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
