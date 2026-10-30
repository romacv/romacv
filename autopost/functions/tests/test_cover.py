import importlib
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

FUNCTIONS = Path(__file__).resolve().parents[1]
with patch.dict(sys.modules,{'google.cloud.tasks_v2':Mock()}):
    sys.path.insert(0,str(FUNCTIONS.parent/'render'))
    render = importlib.import_module('render')


class CoverHeadlineTests(unittest.TestCase):
    def test_headline_stays_inside_the_three_by_four_crop_and_keeps_the_size(self):
        from PIL import Image
        image = Image.new('RGB',(1080,1920),(0,0,0))
        result = render.cover_headline(image,'Завтрак в Arctic, Убуд')
        self.assertEqual(result.size,image.size)
        box = result.convert('L').point(lambda value: 255 if value > 100 else 0).getbbox()
        self.assertIsNotNone(box)
        self.assertGreaterEqual(box[1],240)
        self.assertLessEqual(box[3],1680)



if __name__ == '__main__':
    unittest.main()
