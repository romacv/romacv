import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

FUNCTIONS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FUNCTIONS))
from fake_firestore import FakeFirestore
import platforms as p
import drafter
import main as app
from store import Store, StateError


class StoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        from PIL import Image
        Image.new('RGB',(1080,1920),(90,90,90)).save(self.root/'image0.jpg','JPEG')
        p.configure({'channels':{'tg-channel':-100123},'telegram_bot_token':'x'},Store(FakeFirestore()),self.root)
        self.spec = {'slug':'trip','targets':['tg-channel','tg-story'],'media':{'images':[{'path':'image0.jpg','alt':'a'}]},'copy':{'tg-story':'Дом'}}
        self.identity = {'id':'biz','user':{'id':1,'username':'romacv777'}}

    def convert(self, path):
        video = self.root/'converted'/'photo-story.mp4'
        video.parent.mkdir(exist_ok=True)
        video.write_bytes(b'mp4')
        return video

    def post(self, state, reply=None):
        calls = []
        def fake(method, fields=None, files=None):
            calls.append((method,fields))
            if isinstance(reply,Exception):
                raise reply
            return {'username':'romacv777','id':-100123} if method == 'getChat' else {'id':7}
        with patch.object(p,'telegram_identity',return_value=self.identity), patch.object(p,'telegram',fake), patch.object(p,'checkpoint',lambda spec,st,target,step,action:action()):
            return p.post_telegram(self.spec,state,'tg-story'), calls

    def test_story_links_the_channel_post_and_waits_for_it(self):
        with self.assertRaises(p.Error):
            self.post({'steps':{}})
        with patch.object(p,'photo_story_video',self.convert):
            url, calls = self.post({'steps':{'tg-channel:publish':{'status':'done','value':{'message_id':305}}}})
        area = calls[-1][1]['areas'][0]
        self.assertEqual(area['type'],{'type':'link','url':'https://t.me/romacv777/305'})
        self.assertGreater(area['position']['y_percentage'],60)

    def test_story_failure_is_reported_in_russian_with_the_reason(self):
        failure = p.Error('API failure: 400',status_code=400)
        failure.description = "Bad Request: can't download or generate the file"
        self.spec['targets'] = ['tg-story']
        with patch.object(p,'photo_story_video',self.convert), patch.object(p,'video_info',return_value={}), self.assertRaises(p.Error) as caught:
            self.post({'steps':{}},failure)
        self.assertIn('Telegram не принял историю',str(caught.exception))
        self.assertIn("can't download",str(caught.exception))

    def test_photo_story_is_posted_as_a_unique_photo(self):
        self.spec['targets'] = ['tg-story']
        calls = []
        def fake(method, fields=None, files=None):
            calls.append((method,fields,files and files.get('asset') and files['asset'].read_bytes()))
            return {'id':7}
        with patch.object(p,'telegram_identity',return_value=self.identity), patch.object(p,'telegram',fake), patch.object(p,'checkpoint',lambda spec,st,target,step,action:action()):
            url = p.post_telegram(self.spec,{'steps':{}},'tg-story')
        stories = [(f['content'],data) for method,f,data in calls if method == 'postStory']
        self.assertEqual(stories[0][0],{'type':'photo','photo':'attach://asset'})
        self.assertNotEqual(stories[0][1],(self.root/'image0.jpg').read_bytes())
        self.assertTrue(url.endswith('/s/7'))

    def test_unique_photo_differs_per_call_and_stays_a_1080x1920_jpeg(self):
        from PIL import Image
        first, second = p.unique_photo(self.root/'image0.jpg'), p.unique_photo(self.root/'image0.jpg')
        self.addCleanup(shutil.rmtree,first.parent,True)
        self.addCleanup(shutil.rmtree,second.parent,True)
        self.assertNotEqual(first.read_bytes(),second.read_bytes())
        with Image.open(first) as image:
            self.assertEqual(image.size,(1080,1920))
        (self.root/'x.png').write_bytes(b'\x89PNG')
        with self.assertRaises(p.BuildError):
            p.unique_photo(self.root/'x.png')

    def test_failed_video_fallback_reraises_the_400_rejection(self):
        self.spec['targets'] = ['tg-story']
        def fake(method, fields=None, files=None):
            raise p.Error('API failure: 400',status_code=400)
        def broken(path):
            raise p.BuildError('ffmpeg failed')
        with patch.object(p,'telegram_identity',return_value=self.identity), patch.object(p,'telegram',fake), patch.object(p,'checkpoint',lambda spec,st,target,step,action:action()), patch.object(p,'photo_story_video',broken), self.assertRaises(p.Error) as caught:
            p.post_telegram(self.spec,{'steps':{}},'tg-story')
        self.assertEqual(caught.exception.status_code,400)

    def test_unique_photo_inserts_the_comment_after_leading_app_segments(self):
        data = b'\xff\xd8\xff\xe1\x00\x06abcd\xff\xdb\x00\x02'
        (self.root/'app.jpg').write_bytes(data)
        made = p.unique_photo(self.root/'app.jpg')
        self.addCleanup(shutil.rmtree,made.parent,True)
        out = made.read_bytes()
        self.assertEqual(out[:10],data[:10])
        self.assertEqual(out[10:12],b'\xff\xfe')
        self.assertTrue(out.endswith(b'\xff\xdb\x00\x02'))

    def test_photo_rejected_with_400_falls_back_to_a_silent_five_second_video(self):
        self.spec['targets'] = ['tg-story']
        calls = []
        def fake(method, fields=None, files=None):
            calls.append((method,fields))
            if method == 'postStory' and fields['content']['type'] == 'photo':
                raise p.Error('API failure: 400',status_code=400)
            return {'id':7}
        with patch.object(p,'telegram_identity',return_value=self.identity), patch.object(p,'telegram',fake), patch.object(p,'checkpoint',lambda spec,st,target,step,action:action()), patch.object(p,'photo_story_video',self.convert), patch.object(p,'video_info',return_value={}):
            p.post_telegram(self.spec,{'steps':{}},'tg-story')
        self.assertEqual([f['content']['type'] for m,f in calls if m == 'postStory'],['photo','video'])
        self.assertTrue([f['content'] for m,f in calls if m == 'postStory'][1]['is_animation'])

    def test_photo_story_video_has_no_audio_stream(self):
        with patch.object(p,'ffmpeg_binary',return_value='ffmpeg'), patch.object(p.subprocess,'run',return_value=Mock(returncode=1)) as run:
            with self.assertRaises(p.BuildError):
                p.photo_story_video(self.root/'image0.jpg')
        command = run.call_args[0][0]
        self.assertIn('-an',command)
        self.assertNotIn('anullsrc=r=48000:cl=stereo',command)
        self.assertEqual(command[command.index('-preset')+1],'ultrafast')

    def test_local_build_failure_keeps_its_text_and_never_enters_the_checkpoint(self):
        self.spec['targets'] = ['tg-story']
        steps = []
        def checkpoint(spec, state, target, step, action):
            steps.append(step)
            return action()
        def broken(path):
            raise p.BuildError('Фото истории Telegram должно быть JPEG.')
        with patch.object(p,'telegram_identity',return_value=self.identity), patch.object(p,'telegram') as sent, patch.object(p,'checkpoint',checkpoint), patch.object(p,'unique_photo',broken):
            with self.assertRaises(p.BuildError) as caught:
                p.post_telegram(self.spec,{'steps':{}},'tg-story')
        self.assertIn('должно быть JPEG',str(caught.exception))
        self.assertNotIn('код сети',str(caught.exception))
        self.assertEqual(steps,[])
        sent.assert_not_called()

    def test_photo_story_without_ffmpeg_fails_clearly(self):
        with patch.object(p,'ffmpeg_binary',return_value=None):
            with self.assertRaises(p.Error) as caught:
                p.photo_story_video(self.root/'image0.jpg')
        self.assertIn('ffmpeg',str(caught.exception))

    def test_story_rejection_logs_size_and_codec_without_secrets(self):
        failure = p.Error('API failure: 400',status_code=400)
        failure.description = "Bad Request: can't download or generate the file"
        clip = self.root/'clip.mp4'
        clip.write_bytes(b'mp4')
        def reject(method, fields=None, files=None):
            raise failure
        with patch.object(p,'telegram',reject), patch.object(p,'video_info',return_value={'width':720,'height':1280,'codec':'hevc','duration':4.0,'audio':True}), self.assertLogs(level='ERROR') as logged:
            with self.assertRaises(p.Error):
                p.post_story({'content':{'type':'video','video':'attach://asset'}},clip)
        entry = json.loads(logged.records[0].getMessage())
        self.assertEqual((entry['bytes'],entry['width'],entry['height'],entry['codec']),(3,720,1280,'hevc'))
        self.assertIn("can't download",entry['description'])

    def test_clip_story_carries_duration_and_animation_flag_when_silent(self):
        clip = self.root/'clip.mp4'
        clip.write_bytes(b'mp4')
        content = {'type':'video','video':'attach://asset','cover_frame_timestamp':0.0}
        with patch.object(p,'video_info',return_value={'duration':12.5,'audio':False}):
            first = p.prepare_story({'content':content},clip)[0]['content']
        with patch.object(p,'video_info',return_value={'duration':80.0,'audio':True}):
            second = p.prepare_story({'content':content},clip)[0]['content']
        self.assertEqual(first,{**content,'duration':12.5,'is_animation':True})
        self.assertEqual(second,{**content,'duration':60.0})

    def test_video_info_reads_duration_codec_size_and_audio(self):
        text = b"Duration: 00:01:02.50, start: 0.0\n  Stream #0:0: Video: hevc (Main), yuv420p, 720x1280, 30 fps\n  Stream #0:1: Audio: aac, 48000 Hz\n"
        with patch.object(p,'ffmpeg_binary',return_value='ffmpeg'), patch.object(p.subprocess,'run',return_value=Mock(stderr=text)):
            info = p.video_info(self.root/'x.mp4')
        self.assertEqual(info,{'codec':'hevc','width':720,'height':1280,'duration':62.5,'audio':True})


class MixedPostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        from PIL import Image
        Image.new('RGB',(1080,1920),(90,90,90)).save(self.root/'image0.jpg','JPEG')
        for name in ('clip0.mp4','clip1.mp4'):
            (self.root/name).write_bytes(b'mp4')
        p.configure({'channels':{'tg-channel':-100123},'telegram_bot_token':'x'},Store(FakeFirestore()),self.root)
        self.spec = {'slug':'trip','targets':['tg-channel'],'media':{'images':[{'path':'image0.jpg','alt':'a'}],'clips':[{'path':'clip0.mp4'},{'path':'clip1.mp4'}],'sequence':[{'kind':'clip','index':0},{'kind':'image','index':0},{'kind':'clip','index':1}]},'copy':{'tg-channel':'Дом','tg-story':'Дом','ig-post':'Home'}}
        self.identity = {'id':'biz','user':{'id':1,'username':'romacv777'}}

    def convert(self, path):
        video = self.root/'converted'/'photo-story.mp4'
        video.parent.mkdir(exist_ok=True)
        video.write_bytes(b'mp4')
        return video

    def telegram(self, target):
        calls = []
        def fake(method, fields=None, files=None):
            calls.append((method,fields))
            if method == 'sendMediaGroup':
                return [{'message_id':i} for i in range(1,len(fields['media'])+1)]
            return {'username':'romacv777','id':-100123} if method == 'getChat' else {'id':7,'message_id':1}
        with patch.object(p,'telegram_identity',return_value=self.identity), patch.object(p,'telegram',fake), patch.object(p,'checkpoint',lambda spec,st,t,step,action:action()), patch.object(p,'channel_post_url',return_value=None):
            p.post_telegram(self.spec,{'steps':{}},target)
        return calls

    def test_channel_album_and_stories_keep_photos_and_clips_in_order(self):
        album = self.telegram('tg-channel')[-1][1]['media']
        self.assertEqual([(m['type'],m['media']) for m in album],[('video','attach://clip0'),('photo','attach://image0'),('video','attach://clip1')])
        self.spec['targets'] = ['tg-story']
        with patch.object(p,'photo_story_video',self.convert):
            stories = [fields['content']['type'] for method,fields in self.telegram('tg-story') if method == 'postStory']
        self.assertEqual(stories,['video','photo','video'])

    def test_seven_photos_become_seven_telegram_stories_in_order(self):
        self.spec['targets'] = ['tg-story']
        self.spec['media'] = {'images':[{'path':'image0.jpg','alt':'a'}] * 7}
        with patch.object(p,'photo_story_video',self.convert):
            stories = [fields['content']['type'] for method,fields in self.telegram('tg-story') if method == 'postStory']
        self.assertEqual(stories,['photo'] * 7)

    def test_album_checkpoints_are_saved_per_album_so_a_retry_never_reposts(self):
        self.spec['media']['clips'] = [{'path':'clip0.mp4'},{'path':'clip1.mp4'}]
        self.spec['media']['images'] = [{'path':'image0.jpg','alt':'a'}] * 10
        self.spec['media']['sequence'] = [{'kind':'image','index':i} for i in range(10)] + [{'kind':'clip','index':0},{'kind':'clip','index':1}]
        saved, sends = {}, []
        def checkpoint(spec, state, target, step, action):
            if step not in saved:
                saved[step] = action()
            return saved[step]
        def fake(method, fields=None, files=None):
            sends.append(method)
            if method == 'sendMediaGroup' and len(sends) == 2 and not saved.get('retry'):
                saved['retry'] = True
                raise p.Error('429',status_code=429)
            return [{'message_id':i} for i in range(len(fields['media']))] if method == 'sendMediaGroup' else {'username':'romacv777','id':-100123}
        with patch.object(p,'telegram_identity',return_value=self.identity), patch.object(p,'telegram',fake), patch.object(p,'checkpoint',checkpoint):
            with self.assertRaises(p.Error):
                p.post_telegram(self.spec,{'steps':{}},'tg-channel')
            p.post_telegram(self.spec,{'steps':{}},'tg-channel')
        self.assertEqual(sends.count('sendMediaGroup'),3)
        self.assertEqual({'album0','album1','publish'} - set(saved),set())

    def test_instagram_story_keeps_every_photo_when_all_clips_are_dropped(self):
        spec = {'slug':'trip','media':{'images':[{'path':'image0.jpg','alt':'a'}] * 3,'sequence':[{'kind':'image','index':0},{'kind':'clip','index':0},{'kind':'image','index':1},{'kind':'image','index':2}]}}
        media, keys = app.target_media(spec,['image0.jpg','image1.jpg','image2.jpg'])
        self.assertEqual([(e['kind'],e['index']) for e in media['sequence']],[('image',0),('image',1),('image',2)])
        for name in ('image1.jpg','image2.jpg'):
            (self.root/name).write_bytes((self.root/'image0.jpg').read_bytes())
        requests = []
        def fake(method, url, fields=None, token=None, **rest):
            requests.append(fields or {})
            return {'id':'1','shortcode':'abc'}
        spec = {**spec,'media':media,'targets':['ig-story'],'copy':{'ig-story':'x'}}
        urls = {key:'https://x/' + key for key in keys}
        with patch.object(p,'request',fake), patch.object(p,'probe'), patch.object(p,'ready'), patch.object(p,'ready_all'), patch.object(p,'quota'), patch.object(p,'checkpoint',lambda spec,st,t,step,action:action()):
            p.post_social(spec,{'steps':{}},'ig-story',urls,('tok','1','https://graph'))
        self.assertEqual([f['image_url'] for f in requests if f.get('media_type') == 'STORIES'],['https://x/image0','https://x/image1','https://x/image2'])

    def instagram_story(self, text, fail_with_tags=False):
        requests = []
        def fake(method, url, fields=None, token=None, **rest):
            requests.append(fields or {})
            if fail_with_tags and fields and 'user_tags' in fields:
                raise p.Error('API failure: ' + str(fail_with_tags),status_code=400 if fail_with_tags is True else fail_with_tags)
            return {'id':'1','shortcode':'abc'}
        spec = {'slug':'trip','media':{'images':[{'path':'image0.jpg','alt':'a'}]},'targets':['ig-story'],'copy':{'ig-story':text}}
        with patch.object(p,'request',fake), patch.object(p,'probe'), patch.object(p,'ready'), patch.object(p,'quota'), patch.object(p,'checkpoint',lambda spec,st,t,step,action:action()):
            p.post_social(spec,{'steps':{}},'ig-story',{'image0':'https://x/image0'},('tok','1','https://graph'))
        return [f for f in requests if f.get('media_type') == 'STORIES']

    def test_instagram_story_mentions_the_brand_handle_and_retries_without_it(self):
        containers = self.instagram_story('Новая версия @nostress.app.')
        self.assertEqual(json.loads(containers[0]['user_tags']),[{'username':'nostress.app'}])
        containers = self.instagram_story('Новая версия @nostress.app',fail_with_tags=True)
        self.assertEqual(len(containers),2)
        self.assertIn('user_tags',containers[0])
        self.assertNotIn('user_tags',containers[1])
        self.assertNotIn('user_tags',self.instagram_story('Без упоминаний')[0])

    def test_untagged_retry_happens_only_after_a_400(self):
        with self.assertRaises(p.Error):
            self.instagram_story('Новая версия @nostress.app',fail_with_tags=500)

    def test_instagram_story_tags_only_the_first_frame_and_not_after_an_error(self):
        spec = {'slug':'trip','media':{'images':[{'path':'image0.jpg','alt':'a'}] * 2,'sequence':[{'kind':'image','index':0},{'kind':'image','index':1}]},'targets':['ig-story'],'copy':{'ig-story':'@nostress.app'}}
        (self.root/'image1.jpg').write_bytes((self.root/'image0.jpg').read_bytes())
        urls = {'image0':'https://x/image0','image1':'https://x/image1'}
        def run(state):
            requests = []
            def fake(method, url, fields=None, token=None, **rest):
                requests.append(fields or {})
                return {'id':'1','shortcode':'abc'}
            with patch.object(p,'request',fake), patch.object(p,'probe'), patch.object(p,'ready_all'), patch.object(p,'quota'), patch.object(p,'checkpoint',lambda spec,st,t,step,action:action()):
                p.post_social(spec,state,'ig-story',urls,('tok','1','https://graph'))
            return [f for f in requests if f.get('media_type') == 'STORIES']
        self.assertEqual(['user_tags' in f for f in run({'steps':{}})],[True,False])
        self.assertEqual(['user_tags' in f for f in run({'steps':{'ig-story:untagged':{'status':'done','value':True}}})],[False,False])

    def test_instagram_carousel_has_image_and_video_children_in_order(self):
        requests = []
        def fake(method, url, fields=None, token=None, **rest):
            requests.append(fields or {})
            return {'id':'1','shortcode':'abc'}
        self.spec['targets'] = ['ig-post']
        urls = {'image0':'https://x/i0','clip0':'https://x/c0','clip1':'https://x/c1'}
        with patch.object(p,'request',fake), patch.object(p,'probe'), patch.object(p,'ready'), patch.object(p,'ready_all'), patch.object(p,'quota'), patch.object(p,'checkpoint',lambda spec,st,t,step,action:action()), patch.object(p,'text_for',return_value='Home'):
            p.post_social(self.spec,{'steps':{}},'ig-post',urls,('tok','1','https://graph'))
        children = [f for f in requests if f.get('is_carousel_item')]
        self.assertEqual([f.get('video_url') or f.get('image_url') for f in children],['https://x/c0','https://x/i0','https://x/c1'])
        self.assertEqual([f.get('media_type') for f in children if 'video_url' in f],['VIDEO','VIDEO'])


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(FakeFirestore())
        self.config = {'roman_user_id':1, 'roman_chat_id':1}
        p.configure(self.config, self.store, self.root)
        self.spec = json.loads((FUNCTIONS.parent / 'examples/example-work.json').read_text())
        self.spec.update(targets=['tg-channel','threads'], at='2020-01-01T00:00:00+00:00')
        self.assets = {target:{} for target in self.spec['targets']}
        self.digest = app.asset_hash(self.spec,self.assets)
        self.store.put('items/item-1', {'status':'previewed','revision':1,'messages':[]})
        self.store.put('specs/example', {'state':'previewed','hash':self.digest,'item':'item-1','preview_message':77,'spec':self.spec,'assets':self.assets,'results':{},'errors':{}})

    def chain(self):
        with patch.object(app,'enqueue') as enqueue:
            app.run(self.config,self.store,None,'example',self.root)
            while enqueue.called:
                enqueue.reset_mock()
                app.run(self.config,self.store,None,'example',self.root,True)

    def callback(self, verb='pub', digest=None, identifier=1):
        return self.store.intake({'update_id':identifier,'callback_query':{'id':str(identifier),'data':f'{verb}:example:{(digest or self.digest)[:8]}'}},self.config)

    def test_unapproved_and_changed_content_refused(self):
        self.assertFalse(self.store.claim_run('example'))
        self.callback()
        self.store.change('specs/example',lambda value:value.update(hash='changed'))
        self.assertFalse(self.store.claim_run('example'))

    def test_stale_and_duplicate_preview_cannot_approve(self):
        self.assertIsNone(self.callback(digest='oldhash8'))
        self.assertEqual(self.store.get('specs/example')['state'],'previewed')
        self.assertTrue(self.callback(identifier=2))
        self.assertIsNone(self.callback(identifier=2))
        self.assertIsNone(self.callback(identifier=3))
        self.assertEqual(len(list(self.store.db.collection('outbox').stream())),1)

    def test_completed_targets_never_repost(self):
        self.callback()
        with patch.object(app,'remove_keyboard'), patch.object(app,'tell',return_value={'message_id':999}), patch.object(app,'enqueue') as enqueue, patch.object(app.media,'materialize'), patch.object(p,'social_session',return_value=('token','123',p.THREADS)), patch.object(p,'link_opens',return_value=True), patch.object(p,'post_telegram',return_value='https://t.me/example/1') as tg, patch.object(p,'post_social',return_value='https://www.threads.com/@romacv777/post/Abc') as th:
            self.chain()
            self.chain()
        self.assertEqual((tg.call_count,th.call_count),(1,1))

    def test_old_spec_with_site_target_publishes_the_rest_without_github(self):
        self.spec.update(targets=['blog','tg-channel','threads'],blog={'language':'en','title':'Old','description':'Old','body':'Old'})
        self.assets = {target:{} for target in self.spec['targets']}
        self.digest = app.asset_hash(self.spec,self.assets)
        self.store.put('specs/example',{'state':'previewed','hash':self.digest,'item':'item-1','preview_message':77,'spec':self.spec,'assets':self.assets,'results':{},'errors':{}})
        self.callback()
        with patch.object(app,'remove_keyboard'), patch.object(app,'tell',return_value={'message_id':999}), patch.object(app,'enqueue') as enqueue, patch.object(app.media,'materialize'), patch.object(p,'social_session',return_value=('token','123',p.THREADS)), patch.object(p,'link_opens',return_value=True), patch.object(p,'request',side_effect=AssertionError('network call')), patch.object(p,'post_telegram',return_value='https://t.me/example/1') as tg, patch.object(p,'post_social',return_value='https://www.threads.com/@romacv777/post/Abc') as th:
            self.chain()
        self.assertEqual((tg.call_count,th.call_count),(1,1))
        saved = self.store.get('specs/example')
        self.assertEqual((saved['state'],sorted(saved['results']),saved['errors']),('done',['tg-channel','threads'],{}))

    def test_old_partial_post_with_site_result_reaches_done_on_retry(self):
        self.spec.update(targets=['blog','tg-channel','threads'])
        self.assets = {target:{} for target in self.spec['targets']}
        self.digest = app.asset_hash(self.spec,self.assets)
        site = {'url':'https://www.resrom.com/blog/example','verified':True}
        self.store.put('specs/example',{'state':'previewed','hash':self.digest,'item':'item-1','preview_message':77,'spec':self.spec,'assets':self.assets,'results':{},'errors':{}})
        self.callback()
        self.store.change('specs/example',lambda value:value.update(state='partial',results={'blog':site,'tg-channel':{'url':'https://t.me/example/1','verified':True}},errors={'threads':'setup'},attempts={}))
        self.assertTrue(self.callback('retry',identifier=10))
        with patch.object(app,'remove_keyboard'), patch.object(app,'tell',return_value={'message_id':999}), patch.object(app,'enqueue') as enqueue, patch.object(app.media,'materialize'), patch.object(p,'social_session',return_value=('token','123',p.THREADS)), patch.object(p,'link_opens',return_value=True), patch.object(p,'request',side_effect=AssertionError('network call')), patch.object(p,'post_telegram') as tg, patch.object(p,'post_social',return_value='https://www.threads.com/@romacv777/post/Abc') as th:
            app.run(self.config,self.store,None,'example',self.root)
        tg.assert_not_called()
        th.assert_called_once()
        self.assertEqual([call[0][0]['step'] for call in enqueue.call_args_list],['verify'])
        self.assertEqual(self.store.get('specs/example')['state'],'done')

    def test_retry_skips_published_targets_and_preserves_published_precedence(self):
        self.callback()
        for results in ({},{'tg-channel':{'url':'https://t.me/example/1','verified':True}}):
            with self.subTest(results=results):
                published = {'tg-channel':{'url':'','verified':True,'deleted':True,'copy':'Исправленный текст'}}
                self.store.change('specs/example',lambda value:value.update(state='partial',results=results,published=published,errors={'threads':'setup'},attempts={}))
                self.assertTrue(self.callback('retry',identifier=10 + len(results)))
                with patch.object(app,'remove_keyboard'), patch.object(app,'tell',return_value={'message_id':999}), patch.object(app,'enqueue') as enqueue, patch.object(app.media,'materialize'), patch.object(p,'social_session',return_value=('token','123',p.THREADS)), patch.object(p,'link_opens',return_value=True), patch.object(p,'post_telegram') as tg, patch.object(p,'post_social',return_value='https://www.threads.com/@romacv777/post/Abc') as th:
                    app.run(self.config,self.store,None,'example',self.root)
                tg.assert_not_called()
                th.assert_called_once()
                self.assertEqual([call[0][0]['step'] for call in enqueue.call_args_list],['verify'])
                self.assertEqual(self.store.get('specs/example')['published']['tg-channel'],published['tg-channel'])
                self.assertEqual(self.store.get('items/item-1')['published']['tg-channel'],published['tg-channel'])

    def test_pending_creation_never_repeated(self):
        action = Mock(side_effect=p.Error('timeout'))
        for _ in range(2):
            with self.assertRaises((p.Error,StateError)):
                self.store.checkpoint('example','threads:create',action)
        action.assert_called_once()

    def test_media_bytes_change_approval_hash(self):
        image = self.root/'asset.png'
        image.write_bytes(b'original')
        self.spec['media'] = {'images':[{'path':'asset.png','alt':'Example'}]}
        before = p.fingerprint(self.spec)
        image.write_bytes(b'changed')
        self.assertNotEqual(before,p.fingerprint(self.spec))



class PublishedTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(FakeFirestore())
        self.config = {'roman_user_id':1,'roman_chat_id':1,'telegram_bot_id':2,'channels':{'tg-channel':-100123}}
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        p.configure(self.config,self.store,self.root)
        keyboard = patch.object(app,'remove_keyboard')
        self.keyboard = keyboard.start()
        self.addCleanup(keyboard.stop)
        self.spec = {'slug':'article','kind':'work','at':'2020-01-01T00:00:00+00:00','targets':['tg-channel','threads','ig-post'],'media':{},'copy':{'tg-channel':'Здесь ошибка в тексте.','threads':'A concrete fact.\nWhat next?','ig-post':'A concrete fact. #tools #coding #software'},'approval':{}}
        self.results = {'tg-channel':{'url':'https://t.me/example/10','chat_id':-100123,'message_ids':[10,11],'caption':True,'copy':self.spec['copy']['tg-channel']},'threads':{'url':'https://www.threads.com/@romacv777/post/Abc','id':'123','copy':self.spec['copy']['threads']},'ig-post':{'url':'https://www.instagram.com/p/Abc/','id':'456','copy':self.spec['copy']['ig-post']}}
        self.source = {'item':'item-1','state':'done','spec':self.spec,'results':self.results,'preview_message':20,'published_at':1,'hash':'abcdefgh1234'}
        self.store.put('specs/article',self.source)
        self.store.put('published-messages/20',{'slug':'article'})
        self.store.put('items/item-1',{'status':'previewed','revision':1})

    def intake(self, text, reply=None, identifier=100):
        message = {'text':text,'message_id':identifier}
        if reply:
            message['reply_to_message'] = {'message_id':reply}
        return self.store.intake({'update_id':identifier,'message':message},self.config)

    def delete_tap(self, identifier=100, digest=None):
        digest = digest or self.store.get('specs/article')['hash'][:8]
        return self.store.intake({'update_id':identifier,'callback_query':{'id':str(identifier),'data':'pdel:article:' + digest}},self.config)

    def prepare(self, operation='fix'):
        self.delete_tap() if operation == 'delete' else self.intake('ошибка в посте',20)
        corrected = {'copy':{**self.spec['copy'],'tg-channel':'Здесь исправленный текст.'}}
        with patch.object(app,'tell',return_value={'message_id':30}), patch.object(drafter,'ask',return_value=corrected):
            app.fix(self.config,self.store,'fix-100')
        return self.store.get('specs/fix-100')

    def approve(self, saved, verb='fix', identifier=101):
        return self.store.intake({'update_id':identifier,'callback_query':{'id':str(identifier),'data':verb + ':fix-100:' + saved['hash'][:8]}},self.config)

    def test_new_fix_preview_supersedes_the_older_one(self):
        self.prepare()
        self.intake('ещё одна правка',20,102)
        corrected = {'copy':{**self.spec['copy'],'tg-channel':'Другой текст.'}}
        with patch.object(app,'tell',return_value={'message_id':31}), patch.object(drafter,'ask',return_value=corrected):
            app.fix(self.config,self.store,'fix-102')
        self.assertEqual((self.store.get('specs/fix-100')['state'],self.store.get('specs/fix-102')['state']),('rejected','previewed'))
        self.assertEqual(self.store.get('outbox/superseded-fix-100')['payload'],{'slug':'fix-100','step':'remove-keyboard','message':30})

    def test_fix_edits_only_changed_targets_and_never_reposts(self):
        saved = self.prepare()
        self.assertTrue(self.approve(saved))
        with patch.object(p,'change_published',return_value='исправлено') as change, patch.object(p,'post_social') as repost, patch.object(p,'post_telegram') as post, patch.object(app,'tell',return_value={'message_id':31}):
            app.apply_fix(self.config,self.store,None,'fix-100',self.root)
        self.assertEqual([call.args[0] for call in change.call_args_list],['tg-channel'])
        repost.assert_not_called()
        post.assert_not_called()
        original = self.store.get('specs/article')
        self.assertEqual(original['results']['tg-channel']['copy'],'Здесь исправленный текст.')
        self.assertEqual(original['results']['threads'],self.results['threads'])
        self.assertEqual(self.store.get('specs/fix-100')['state'],'done')

    def test_delete_removes_each_target_once_and_partial_keeps_keyboard(self):
        saved = self.prepare('delete')
        self.assertEqual(self.delete_tap(99),'Удаление уже готовится.')
        self.approve(saved,'del')
        def change(target,*args):
            if target == 'threads':
                raise p.Error('permission',403)
            return 'удалено'
        with patch.object(p,'change_published',side_effect=change) as action, patch.object(app,'tell',return_value={'message_id':31}) as tell:
            app.apply_fix(self.config,self.store,None,'fix-100',self.root)
            self.assertEqual(sorted(call.args[0] for call in action.call_args_list),['ig-post','tg-channel','threads'])
            markup = json.dumps(tell.call_args.kwargs['reply_markup'])
            self.assertIn('pfix:article',markup)
            self.assertIn('pdel:article',markup)
            self.assertIn('retry:fix-100',markup)
            self.approve(saved,'retry',102)
            app.apply_fix(self.config,self.store,None,'fix-100',self.root)
            self.assertEqual([call.args[0] for call in action.call_args_list][3:],['threads'])
        self.assertEqual(self.store.get('specs/article')['results']['tg-channel']['deleted'],True)
        self.assertFalse(self.store.get('specs/article').get('deleted'))


if __name__ == '__main__':
    unittest.main()
