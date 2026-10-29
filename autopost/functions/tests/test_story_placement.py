from pathlib import Path
import random
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import media
from PIL import Image


def photo(busy):
    random.seed(3)
    image = Image.new('RGB',(1080,1920),(90,90,90))
    rows = busy[1]-busy[0]
    image.paste(Image.frombytes('RGB',(1080,rows),random.randbytes(1080*rows*3)),(0,busy[0]))
    return image


class PlacementTests(unittest.TestCase):
    def place(self, busy):
        directory = Path(tempfile.mkdtemp())
        photo(busy).save(directory/'a.jpg','JPEG')
        return media.compose_story(directory/'a.jpg',directory/'b.jpg',{'text':'Утро в Убуде','place':'Ubud','anchor':[0.5,0.5],'align':'center'})

    def test_block_avoids_the_busy_subject_area(self):
        self.assertLess(self.place((900,1500))[3],900)
        self.assertGreater(self.place((250,850))[1],850)


if __name__ == '__main__':
    unittest.main()
