from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest

FUNCTIONS = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(FUNCTIONS))
import drafter
import media
from PIL import Image


def overlay(**changes):
    return {'text':'Сегодня утро начинаю в Suka Espresso Ubud','place':'Suka Espresso Ubud','anchor':[0.5,0.5],'align':'center',**changes}


def errors(value, targets=('ig-story','tg-story'), images=True):
    spec = {'overlay':{'ig-story':value}}
    return drafter.overlay_errors(spec,list(targets),[{'path':'a.jpg','alt':'a'}] if images else [])


class OverlayValidationTests(unittest.TestCase):
    def test_accepts_valid_overlay(self):
        self.assertEqual(errors(overlay()),[])
        self.assertEqual(errors(overlay(place=None,anchor=[0,1])),[])

    def test_rejects_bad_fields(self):
        for bad in (overlay(text=''),overlay(text='x'*61),overlay(text='Утро \U0001f600'),overlay(place=5),
                    overlay(anchor=[0.5]),overlay(anchor=[1.2,0.5]),overlay(anchor=[True,0.5]),overlay(align='top'),
                    {'text':'a'}):
            self.assertTrue(errors(bad),bad)

    def test_rejects_wrong_target_or_no_photo(self):
        self.assertTrue(errors(overlay(),targets=('tg-story',)))
        self.assertTrue(errors(overlay(),images=False))

    def test_spec_field_allowed(self):
        at = (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
        found = drafter.validate({'slug':'a','kind':'personal','at':at,'targets':['tg-story'],'media':{},'copy':{},'overlay':{},'approval':{}},copy_only=True)
        self.assertNotIn('Unknown spec fields',found)


class ComposeStoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.source = self.directory/'plate.jpg'
        Image.new('RGB',(1200,900),(120,120,120)).save(self.source,'JPEG')

    def compose(self, **changes):
        destination = self.directory/'story.jpg'
        box = media.compose_story(self.source,destination,overlay(**changes))
        with Image.open(destination) as image:
            return image.size, box

    def test_size_is_story_frame(self):
        size, _ = self.compose()
        self.assertEqual(size,(1080,1920))

    def test_corner_anchors_stay_in_safe_zone(self):
        left, top, right, bottom = media.STORY_SAFE
        for anchor in ([0,0],[1,0],[0,1],[1,1]):
            for align in ('left','center','right'):
                _, box = self.compose(anchor=anchor,align=align)
                self.assertGreaterEqual(box[0],left)
                self.assertGreaterEqual(box[1],top)
                self.assertLessEqual(box[2],right)
                self.assertLessEqual(box[3],bottom)

    def test_str_paths_accepted(self):
        media.compose_story(str(self.source),str(self.directory/'s.jpg'),overlay())
        self.assertTrue((self.directory/'s.jpg').exists())

    def test_text_colour_follows_background(self):
        for colour, expected in (((255,255,255),(20,20,20)),((0,0,0),(255,255,255))):
            source = self.directory/'flat.jpg'
            Image.new('RGB',(1080,1920),colour).save(source,'JPEG')
            destination = self.directory/'flat-story.jpg'
            x0, y0, x1, y1 = media.compose_story(source,destination,overlay(place=None,text='Утро'))
            with Image.open(destination) as image:
                pixels = [p for p in image.convert('RGB').crop((x0,y0,x1,y1)).getdata() if abs(p[0]-expected[0]) < 30 and p[0] == p[1] == p[2]]
            self.assertTrue(any(abs(p[0]-expected[0]) < 5 for p in pixels))

    def test_is_light(self):
        self.assertTrue(media.is_light(Image.new('RGB',(10,10),(250,250,250))))
        self.assertFalse(media.is_light(Image.new('RGB',(10,10),(0,0,0))))

    def test_balanced_wrap(self):
        from PIL import ImageFont
        font = ImageFont.truetype(str(media.FONTS/'InterDisplay-Bold.otf'),64)
        lines = media.wrap_balanced('Сегодня утро начинаю в Suka Espresso Ubud',font)
        self.assertEqual(len(lines),2)
        self.assertLess(abs(font.getlength(lines[0])-font.getlength(lines[1])),250)

    def test_text_only_overlay(self):
        size, _ = self.compose(place=None)
        self.assertEqual(size,(1080,1920))


if __name__ == '__main__':
    unittest.main()
