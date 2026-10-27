import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

FUNCTIONS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FUNCTIONS))
from fake_firestore import FakeFirestore
import main as app
import platforms as p
from store import Store

with patch.dict(sys.modules,{'google.cloud.tasks_v2':Mock()}):
    sys.path.insert(0,str(FUNCTIONS.parent/'render'))
    render = importlib.import_module('render')


class RenderSettingsTests(unittest.TestCase):
    def test_draft_settings_are_small_and_fast(self):
        arguments = render.encode_arguments('tg-story',True)
        self.assertEqual(render.frame_size(True),(540,960))
        for pair in (['-c:v','libx264'],['-preset','ultrafast'],['-crf','32'],['-b:a','96k']):
            self.assertIn(pair,[arguments[i:i+2] for i in range(len(arguments))])
        self.assertNotIn('libx265',arguments)
        self.assertNotIn('loudnorm',render.audio_filter(True))
        self.assertIn('scale=540:960',render.clip_filters('crop',True))
        self.assertIn('boxblur',render.clip_filters('blur',True))

    def test_final_settings_keep_full_quality(self):
        self.assertEqual(render.frame_size(False),(1080,1920))
        self.assertIn('libx265',render.encode_arguments('tg-story',False))
        self.assertIn('-crf',render.encode_arguments('ig-reel',False))
        self.assertIn('loudnorm',render.audio_filter(False))
        self.assertIn('scale=1080:1920',render.clip_filters('crop',False))

    def test_draft_shares_one_encode_per_distinct_timeline(self):
        same = {'clips':[{'source':'a','in':0,'out':5}],'subtitles':[],'titles':[]}
        other = {'clips':[{'source':'a','in':1,'out':5}],'subtitles':[]}
        targets = {'ig-reel':same,'tg-channel':json.loads(json.dumps(same)),'ig-story':other}
        self.assertEqual(render.timeline_groups(targets,True),{'ig-reel':['ig-reel','tg-channel'],'ig-story':['ig-story']})
        self.assertEqual(list(render.timeline_groups(targets,False)),['ig-reel','tg-channel','ig-story'])


class TitlePlacementTests(unittest.TestCase):
    def test_final_title_is_centered_at_590(self):
        self.assertEqual(render.title_origin((600,92),(1080,1920)),(240,590))

    def test_draft_title_is_proportional(self):
        self.assertEqual(render.title_origin((300,46),(540,960)),(120,295))


class FinalRenderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(FakeFirestore())
        self.config = {'roman_user_id':1,'roman_chat_id':1}
        p.configure(self.config,self.store,self.root)
        self.edl = {'kind':'work','targets':{'threads':{'clips':[],'subtitles':[]}},'copy':{'threads':'Text?'},'cover':{'target':'threads','time':0},'fit':'crop'}
        self.spec = {'slug':'example','kind':'work','at':'2020-01-01T00:00:00+00:00','targets':['threads'],'media':{'video':'video.mp4','cover':'cover.jpg'},'copy':{'threads':'Text?'},'approval':{}}
        self.draft_assets = {'threads':{'video.mp4':{'object':'draft/video.mp4','generation':'1'},'cover.jpg':{'object':'draft/cover.jpg','generation':'1'}}}
        self.basis = {'edl':self.edl,'render':app.RENDER_SETTINGS}
        self.digest = app.asset_hash(self.spec,self.basis)
        self.store.put('items/item-1',{'status':'previewed','revision':1,'render':'draft-1','slug':'example','messages':[]})
        self.store.put('renders/draft-1',{'id':'draft-1','mode':'draft','item':'item-1','revision':1,'edl':self.edl,'sources':{'a':{}},'bucket':'b','project':'pr','worker_url':'w','service_account':'s','state':'done'})
        self.store.put('specs/example',{'state':'previewed','hash':self.digest,'item':'item-1','spec':self.spec,'assets':self.draft_assets,'basis':self.basis,'results':{},'errors':{}})

    def approve(self):
        self.store.intake({'update_id':1,'callback_query':{'id':'1','data':'pub:example:' + self.digest[:8]}},self.config)

    def start_final(self):
        self.approve()
        with patch.object(app,'remove_keyboard'), patch.object(app,'tell') as tell, patch.object(app,'start_job') as start, patch.object(p,'post_social') as publish:
            app.run(self.config,self.store,None,'example',self.root)
        publish.assert_not_called()
        return start,tell

    def test_hash_ignores_draft_bytes(self):
        self.assertEqual(app.asset_hash(self.spec,self.basis),self.digest)
        self.assertNotEqual(app.asset_hash(self.spec,{**self.basis,'render':'final-v2'}),self.digest)

    def test_publish_tap_on_montage_starts_final_render(self):
        start,tell = self.start_final()
        saved = self.store.get('specs/example')
        job = self.store.get('renders/' + saved['final_render'])
        self.assertEqual(saved['state'],'rendering')
        self.assertEqual((job['mode'],job['edl'],job['state'],job['sources']),('final',self.edl,'queued',{'a':{}}))
        start.assert_called_once_with('pr',saved['final_render'])
        self.assertIn('в полном качестве',tell.call_args.args[1])

    def test_final_completion_continues_run_with_final_assets(self):
        self.start_final()
        render_id = self.store.get('specs/example')['final_render']
        final = {'videos':{'threads':{'object':'final/video.mp4','generation':'2'}},'cover':{'object':'final/cover.jpg','generation':'2'}}
        self.store.change('renders/' + render_id,lambda value:value.update(state='done',outputs=final))
        app.rendered(self.config,self.store,Mock(),{'item':'item-1','revision':1,'render':render_id},self.root)
        saved = self.store.get('specs/example')
        self.assertEqual((saved['state'],saved['final'],saved['assets']['threads']['video.mp4']),('approved',True,final['videos']['threads']))
        self.assertTrue(self.store.get('renders/' + render_id)['delivered'])
        self.assertEqual(self.store.get('outbox/final-' + render_id)['payload'],{'step':'run','slug':'example'})
        with patch.object(app,'remove_keyboard'), patch.object(app,'tell',return_value={'message_id':9}), patch.object(app,'start_job') as start, patch.object(app.media,'materialize') as materialize, patch.object(app.media,'signed',return_value='https://example.com/x'), patch.object(p,'probe'), patch.object(p,'social_session',return_value=('token','123',p.THREADS)), patch.object(p,'link_opens',return_value=True), patch.object(p,'post_social',return_value='https://www.threads.com/@x/post/1') as publish:
            bucket = Mock()
            app.run(self.config,self.store,bucket,'example',self.root)
        start.assert_not_called()
        publish.assert_called_once()
        self.assertEqual(materialize.call_args.args[1],saved['assets']['threads'])
        self.assertEqual(self.store.get('specs/example')['state'],'done')

    def test_stale_final_render_is_ignored(self):
        self.start_final()
        render_id = self.store.get('specs/example')['final_render']
        self.store.put('renders/old',{'mode':'final','state':'done','outputs':{'videos':{},'cover':{}}})
        before = self.store.get('specs/example')
        app.rendered(self.config,self.store,Mock(),{'item':'item-1','revision':1,'render':'old'},self.root)
        self.assertTrue(self.store.get('renders/old')['delivered'])
        self.assertEqual(self.store.get('specs/example'),before)
        self.store.change('renders/' + render_id,lambda value:value.update(state='done',outputs={'videos':{},'cover':{}}))
        app.rendered(self.config,self.store,Mock(),{'item':'item-1','revision':2,'render':render_id},self.root)
        self.assertEqual(self.store.get('specs/example')['state'],'rendering')

    def test_failed_final_render_publishes_nothing_and_offers_retry(self):
        self.start_final()
        render_id = self.store.get('specs/example')['final_render']
        self.store.change('renders/' + render_id,lambda value:value.update(state='failed'))
        with patch.object(app,'tell') as tell:
            app.rendered(self.config,self.store,Mock(),{'item':'item-1','revision':1,'render':render_id},self.root)
        self.assertEqual(self.store.get('specs/example')['state'],'partial')
        self.assertIn('Повторить',json.dumps(tell.call_args.kwargs['reply_markup'],ensure_ascii=False))
        self.assertIsNone(self.store.get('outbox/final-' + render_id))

    def test_non_montage_run_publishes_without_render(self):
        assets = {'threads':{}}
        digest = app.asset_hash(self.spec,assets)
        self.store.put('specs/example',{'state':'approved','hash':digest,'approved_hash':digest,'item':'item-1','spec':self.spec,'assets':assets,'results':{},'errors':{}})
        with patch.object(app,'remove_keyboard'), patch.object(app,'tell',return_value={'message_id':9}), patch.object(app,'start_job') as start, patch.object(app.media,'materialize'), patch.object(p,'social_session',return_value=('token','123',p.THREADS)), patch.object(p,'link_opens',return_value=True), patch.object(p,'post_social',return_value='https://www.threads.com/@x/post/1') as publish:
            app.run(self.config,self.store,Mock(),'example',self.root)
        start.assert_not_called()
        publish.assert_called_once()

    def test_recovery_handles_draft_and_final_renders(self):
        for mode in ('draft','final'):
            self.store.put('renders/r-' + mode,{'mode':mode,'state':'done','item':'item-1','revision':1})
        config = {**self.config,'webhook_url':'test','github_pat':'test'}
        with patch.object(app.platforms,'telegram',return_value={'url':'test'}), patch.object(app.platforms,'request',return_value={}), patch.object(app,'enqueue') as enqueue, patch.object(app,'tell'), patch('urllib.request.urlopen',side_effect=OSError()):
            app.maintenance(config,self.store,Mock())
        steps = sorted((call.args[0]['render'],call.args[0]['step']) for call in enqueue.call_args_list if call.args[0].get('render') in ('r-draft','r-final'))
        self.assertEqual(steps,[('r-draft','rendered'),('r-final','rendered')])
        self.assertTrue(self.store.get('renders/r-final')['delivered'])
