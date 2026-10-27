from pathlib import Path
import sys
import tempfile
import unittest

FUNCTIONS = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(FUNCTIONS))
import media
from PIL import Image


class CleanImageTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.source = self.directory/'source.jpg'
        Image.new('RGB',(900,1200),(200,60,60)).save(self.source,'JPEG')

    def clean(self, **options):
        destination = self.directory/'out.jpg'
        media.clean_image(self.source,destination,**options)
        with Image.open(destination) as image:
            return image.convert('RGB')

    def test_shared_keeps_ratio_without_bars(self):
        image = self.clean()
        self.assertEqual(image.size,(900,1200))
        self.assertEqual(image.getpixel((0,600)),image.getpixel((450,600)))
        self.assertNotEqual(image.getpixel((0,600)),(0,0,0))

    def test_ig_post_crops_to_four_by_five(self):
        image = self.clean(crop=True)
        self.assertEqual(image.size,(900,1125))
        self.assertNotEqual(image.getpixel((0,562)),(0,0,0))

    def test_story_fills_bars_with_blurred_photo(self):
        image = self.clean(story=True)
        self.assertEqual(image.size,(1080,1920))
        side = image.getpixel((540,5))
        self.assertNotEqual(side,(0,0,0))
        self.assertTrue(all(a < b for a,b in zip(side,image.getpixel((540,960)))))


if __name__ == '__main__':
    unittest.main()
