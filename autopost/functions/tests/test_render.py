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


class BarTests(unittest.TestCase):
    def test_bar_shows_the_rounded_share(self):
        self.assertEqual(render.bar(0.4),'▰▰▰▰▱▱▱▱▱▱ 40%')
        self.assertEqual((render.bar(-1),render.bar(2)),('▱▱▱▱▱▱▱▱▱▱ 0%','▰▰▰▰▰▰▰▰▰▰ 100%'))

    def test_loader_text_adds_the_step_and_the_bar_only_when_known(self):
        loader = render.Loader({'mode':'draft','loader':{'text':'Монтирую'}},Mock())
        loader.stage('Рендерю черновик')
        self.assertTrue(loader.text().startswith('Монтирую\nРендерю черновик: 0:0'))
        self.assertNotIn('%',loader.text())
        loader.progress(0.5)
        self.assertTrue(loader.text().endswith('шаг 3 из 3\n▰▰▰▰▰▱▱▱▱▱ 50%'))


class LoaderTests(unittest.TestCase):
    def test_stage_edits_the_status_message_and_failure_replaces_it(self):
        with patch.dict(render.os.environ,{'TELEGRAM_BOT_TOKEN':'t'}):
            loader = render.Loader({'loader':{'chat_id':1,'message_id':5,'text':'Монтирую 1 клип'}},Mock())
        calls = []
        loader.call = lambda method,fields:calls.append((method,fields))
        loader.stage('Рендерю черновик')
        loader.stop(True)
        edits = [f['text'] for m,f in calls if m == 'editMessageText']
        self.assertTrue(edits[0].startswith('Монтирую 1 клип\nРендерю черновик: 0:0'))
        self.assertEqual(edits[-1],'Монтирую 1 клип\nМонтаж не удался.')


class TitleTests(unittest.TestCase):
    def test_title_splits_at_the_last_comma(self):
        self.assertEqual(render.title_parts('Gelora Bung Karno, Jakarta'),('Gelora Bung Karno','JAKARTA'))
        self.assertEqual(render.title_parts('A, B, Bali'),('A, B','BALI'))
        self.assertEqual(render.title_parts('Jakarta'),('Jakarta',''))


class RenderSettingsTests(unittest.TestCase):
    def test_draft_is_small_and_fast_final_keeps_quality(self):
        arguments = render.encode_arguments('tg-story',True)
        self.assertEqual(render.frame_size(True),(540,960))
        for pair in (['-c:v','libx264'],['-preset','ultrafast'],['-crf','32']):
            self.assertIn(pair,[arguments[i:i+2] for i in range(len(arguments))])
        self.assertNotIn('libx265',arguments)
        self.assertNotIn('loudnorm',render.audio_filter(True))
        self.assertIn('scale=540:960',render.clip_filters('crop',True))
        self.assertEqual(render.frame_size(False),(1080,1920))
        self.assertIn('libx265',render.encode_arguments('tg-story',False))
        self.assertIn('loudnorm',render.audio_filter(False))
        self.assertIn('scale=1080:1920',render.clip_filters('crop',False))


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

    def test_publish_tap_on_montage_starts_final_render(self):
        start,tell = self.start_final()
        saved = self.store.get('specs/example')
        job = self.store.get('renders/' + saved['final_render'])
        self.assertEqual(saved['state'],'rendering')
        self.assertEqual((job['mode'],job['edl'],job['state'],job['sources']),('final',self.edl,'queued',{'a':{}}))
        start.assert_called_once_with('pr',saved['final_render'])

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

    def test_failed_final_render_publishes_nothing_and_offers_retry(self):
        self.start_final()
        render_id = self.store.get('specs/example')['final_render']
        self.store.change('renders/' + render_id,lambda value:value.update(state='failed'))
        with patch.object(app,'tell') as tell:
            app.rendered(self.config,self.store,Mock(),{'item':'item-1','revision':1,'render':render_id},self.root)
        self.assertEqual(self.store.get('specs/example')['state'],'partial')
        self.assertIsNone(self.store.get('outbox/final-' + render_id))

    def test_recovery_handles_draft_and_final_renders(self):
        for mode in ('draft','final'):
            self.store.put('renders/r-' + mode,{'mode':mode,'state':'done','item':'item-1','revision':1})
        config = {**self.config,'webhook_url':'test','github_pat':'test'}
        with patch.object(app.platforms,'telegram',return_value={'url':'test'}), patch.object(app.platforms,'request',return_value={}), patch.object(app,'enqueue') as enqueue, patch.object(app,'tell'), patch('urllib.request.urlopen',side_effect=OSError()):
            app.maintenance(config,self.store,Mock())
        steps = sorted((call.args[0]['render'],call.args[0]['step']) for call in enqueue.call_args_list if call.args[0].get('render') in ('r-draft','r-final'))
        self.assertEqual(steps,[('r-draft','rendered'),('r-final','rendered')])
        self.assertFalse(self.store.get('renders/r-final').get('delivered',False))


if __name__ == '__main__':
    unittest.main()
