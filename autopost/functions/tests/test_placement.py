import importlib
import json
from pathlib import Path
import sys
import tempfile
import unicodedata
import unittest
from unittest.mock import Mock, patch

FUNCTIONS = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(FUNCTIONS))
import media
import montage
import platforms as p
from PIL import Image, ImageDraw

with patch.dict(sys.modules,{'google.cloud.tasks_v2':Mock()}):
    sys.path.insert(0,str(FUNCTIONS.parent/'render'))
    render = importlib.import_module('render')

OVERLAY = {'text':'Жизнь на Бали','place':None,'anchor':[0.5,0.5],'align':'center'}


def scene(subject):
    image = Image.new('RGB',(1080,1920),(120,125,130))
    ImageDraw.Draw(image).rectangle(subject,fill=(250,215,20))
    return image


def intersects(box, other):
    return min(box[2],other[2]) > max(box[0],other[0]) and min(box[3],other[3]) > max(box[1],other[1])


class StoryPlacementTests(unittest.TestCase):
    def compose(self, subject, overlay=OVERLAY):
        directory = Path(tempfile.mkdtemp())
        scene(subject).save(directory/'a.jpg','JPEG',quality=95)
        return media.compose_story(directory/'a.jpg',directory/'b.jpg',overlay)

    def test_text_is_one_centred_block_above_the_subject_with_side_margins(self):
        subject = (300,700,780,1500)
        box = self.compose(subject)
        self.assertFalse(intersects(box,subject))
        self.assertLessEqual(box[3],subject[1])
        self.assertGreaterEqual(box[0],80)
        self.assertLessEqual(box[2],1000)
        self.assertLessEqual(abs(box[0]+box[2]-1080),2)

    def test_without_room_for_the_pill_it_is_dropped_and_the_size_stays(self):
        subject = (300,480,780,1500)
        full = {**OVERLAY,'text':'Жизнь на Бали - каждый день праздник','place':'Бали'}
        box = self.compose(subject,full)
        self.assertFalse(intersects(box,subject))
        self.assertLessEqual(box[3],subject[1])
        plain = media.story_block(full,False,full['text'],False)[2]
        self.assertEqual(box[3]-box[1],plain[3]-plain[1])

    def test_degrade_order_is_handle_then_pill_then_a_shorter_line(self):
        overlay = {**OVERLAY,'text':'Жизнь на Бали - каждый день праздник @rusters','place':'Бали'}
        self.assertEqual(media.story_variants(overlay),[
            ('Жизнь на Бали - каждый день праздник @rusters','Бали'),
            ('Жизнь на Бали - каждый день праздник','Бали'),
            ('Жизнь на Бали - каждый день праздник',None),
            ('Жизнь на Бали',None)])

    def test_font_size_is_the_same_for_every_line(self):
        short = media.story_block(OVERLAY,False,'Бали',False)[2]
        longer = media.story_block(OVERLAY,False,'Жизнь на Бали',False)[2]
        self.assertEqual(short[3]-short[1],longer[3]-longer[1])

    def test_a_long_one_line_text_keeps_the_whole_footprint_80px_from_both_edges(self):
        overlay = {**OVERLAY,'text':'Жизнь на Бали каждый день','place':None}
        block, box, foot = media.story_block(overlay,False,overlay['text'],False)
        self.assertLessEqual(foot[2]-foot[0],1080-2*80)
        placed = self.compose((400,1200,700,1800),overlay)
        self.assertGreaterEqual(placed[0],80)
        self.assertLessEqual(placed[2],1000)

    def test_no_free_space_still_places_the_text_over_the_least_important_region(self):
        directory = Path(tempfile.mkdtemp())
        scene((0,0,1080,1920)).save(directory/'a.jpg','JPEG',quality=95)
        placed = media.compose_story(directory/'a.jpg',directory/'b.jpg',OVERLAY)
        self.assertGreaterEqual(placed[0],80)
        self.assertLessEqual(placed[2],1000)

    def test_every_sampled_frame_is_respected(self):
        canvases = [scene((100,250,500,700)),scene((600,250,1000,700))]
        frames = [(media.edge_map(c),media.subject_keepout(c)) for c in canvases]
        width, height = 300,120
        variant, left, top = media.free_slot(frames,[(width,height)],250,1500)
        for subject in ((100,250,500,700),(600,250,1000,700)):
            self.assertFalse(intersects((left,top,left+width,top+height),subject))


class TitlePlacementTests(unittest.TestCase):
    def test_title_without_frames_stays_at_the_top_of_the_range(self):
        self.assertEqual(render.emptiest_title_y([],30,(300,50),1/3,1250),round((300-render.TITLE_MARGIN)/3))

    def test_montage_title_avoids_the_subject_in_any_sampled_frame(self):
        directory = Path(tempfile.mkdtemp())
        frames = []
        for number, subject in enumerate(((90,60,270,165),(90,210,270,315))):
            path = directory/f'{number}.jpg'
            scene_image = Image.new('RGB',(360,640),(120,125,130))
            ImageDraw.Draw(scene_image).rectangle(subject,fill=(250,215,20))
            scene_image.save(path,'JPEG',quality=95)
            frames.append(path)
        top = render.emptiest_title_y(frames,30,(300,50),1/3,1250)
        for subject in ((90,60,270,165),(90,210,270,315)):
            self.assertFalse(intersects((30,top,330,top+50),subject))


class MontageStoryTests(unittest.TestCase):
    def setUp(self):
        example = json.loads((FUNCTIONS.parent/'examples/example-montage.json').read_text())
        self.edl, self.sources = example['edl'], example['sources']
        self.edl.setdefault('threads_tag','bali')
        self.edl['targets']['ig-story'] = json.loads(json.dumps(self.edl['targets']['tg-story']))
        self.edl['copy']['ig-story'] = self.edl['copy']['tg-story']

    def test_telegram_and_instagram_stories_share_one_timeline(self):
        self.assertEqual(self.edl['targets']['ig-story'],self.edl['targets']['tg-story'])
        self.assertEqual(montage.validate(self.edl,self.sources),[])
        self.edl['targets']['ig-story']['clips'].reverse()
        self.assertIn('ig-story and tg-story must share one story timeline',montage.validate(self.edl,self.sources))

    def test_story_timelines_group_into_one_preview(self):
        groups = render.timeline_groups({name:self.edl['targets'][name] for name in ('ig-story','tg-story')},True)
        self.assertEqual(len(groups),1)


class VideoInfoTests(unittest.TestCase):
    def info(self, text):
        with patch.object(p,'ffmpeg_binary',return_value='ffmpeg'), patch.object(p.subprocess,'run',return_value=Mock(stderr=text)):
            return p.video_info(Path('x.mp4'))

    def test_rotated_phone_clip_reports_its_displayed_size(self):
        text = b"Duration: 00:00:05.00, start: 0.0\n  Stream #0:0: Video: hevc (Main), yuv420p, 1920x1080, 30 fps\n    Side data:\n      displaymatrix: rotation of -90.00 degrees\n"
        self.assertEqual((self.info(text)['width'],self.info(text)['height']),(1080,1920))
        legacy = b"Duration: 00:00:05.00\n    rotate          : 270\n  Stream #0:0: Video: h264, yuv420p, 1920x1080, 30 fps\n"
        self.assertEqual((self.info(legacy)['width'],self.info(legacy)['height']),(1080,1920))
        flat = b"Duration: 00:00:05.00\n  Stream #0:0: Video: h264, yuv420p, 1920x1080, 30 fps\n    Side data:\n      displaymatrix: rotation of 180.00 degrees\n"
        self.assertEqual(self.info(flat)['width'],1920)

    def test_album_video_probe_runs_once_per_file(self):
        path = Path(tempfile.mkdtemp())/'v.mp4'
        path.write_bytes(b'0')
        p.cached_video_size.cache_clear()
        with patch.object(p,'video_size',return_value={'width':720,'height':1280}) as probe:
            for _ in range(3):
                fields = p.video_fields(path)
        self.assertEqual(probe.call_count,1)
        self.assertEqual(fields,{'width':720,'height':1280,'supports_streaming':True})


class EncodeTests(unittest.TestCase):
    def test_every_final_format_keys_a_frame_each_second(self):
        for target in ('ig-reel','tg-channel','threads','ig-story'):
            self.assertIn('-g',render.encode_arguments(target,False))
            self.assertEqual(render.encode_arguments(target,False)[render.encode_arguments(target,False).index('-g')+1],'30')
        self.assertIn('keyint=30:min-keyint=30:scenecut=0',render.encode_arguments('tg-story',False))


class SpeechHintTests(unittest.TestCase):
    def test_hints_keep_phrases_cap_length_and_normalise(self):
        decomposed = unicodedata.normalize('NFD','Café Ubud')
        hints = media.speech_hints('Мы пошли в Casa Luna, ' + decomposed,'Rusters '+'x'*300)
        self.assertIn('Casa Luna',hints)
        self.assertIn(unicodedata.normalize('NFC','Café Ubud'),hints)
        self.assertTrue(all(len(h) <= 100 for h in hints))
        self.assertTrue(all(h == unicodedata.normalize('NFC',h) for h in hints))


if __name__ == '__main__':
    unittest.main()
