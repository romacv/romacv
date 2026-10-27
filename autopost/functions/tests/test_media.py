from pathlib import Path
import sys
import tempfile
import unittest

FUNCTIONS = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(FUNCTIONS))
import media
from PIL import Image


def overlay(**changes):
    return {'text':'Сегодня утро начинаю в Suka Espresso Ubud','place':'Suka Espresso Ubud','anchor':[0.5,0.5],'align':'center',**changes}


class StoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.source = self.directory/'plate.jpg'
        Image.new('RGB',(1200,900),(120,120,120)).save(self.source,'JPEG')

    def compose(self, **changes):
        destination = self.directory/'story.jpg'
        box = media.compose_story(self.source,destination,overlay(**changes))
        with Image.open(destination) as image:
            self.format = image.format
            return image.size, box

    def test_size_is_story_frame(self):
        size, _ = self.compose()
        self.assertEqual((size,self.format),((1080,1920),'JPEG'))


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


class ImageDocumentTests(unittest.TestCase):
    def setUp(self):
        self.document = {'file_id':'f','file_unique_id':'u','file_size':95000,'mime_type':'image/jpeg','file_name':'a.jpg'}

    def test_attachments_use_the_image_document_as_an_image(self):
        item = {'messages':[{'message_id':11,'document':self.document}]}
        values = media.attachments(item)
        self.assertEqual([(a['name'],a['kind'],a['file_id']) for a in values],[('11.jpg','photo','f')])


if __name__ == '__main__':
    unittest.main()
