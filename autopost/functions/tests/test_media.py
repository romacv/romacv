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


class PostPhotoTests(unittest.TestCase):
    def test_post_photo_is_square_around_the_detailed_subject(self):
        from PIL import ImageDraw
        directory = Path(tempfile.mkdtemp())
        source = directory/'tall.jpg'
        image = Image.new('RGB',(900,1600),(200,200,200))
        draw = ImageDraw.Draw(image)
        for step in range(0,200,10):
            draw.rectangle((100+step,1150+step,140+step,1190+step),fill=(20,20,20))
        image.save(source,'JPEG')
        destination = directory/'post.jpg'
        media.clean_image(source,destination,False,True)
        with Image.open(destination) as result:
            self.assertEqual(result.size,(900,900))
            self.assertLess(result.convert('L').getextrema()[0],100)

    def test_story_pill_corners_stay_transparent_and_emoji_overlay_renders(self):
        pill = media.place_pill('Ubud')
        self.assertEqual(pill.getpixel((0,0))[3],0)
        margin = pill.info['margin']
        self.assertLess(pill.getpixel((margin,margin))[3],40)
        self.assertEqual(max(pill.getpixel((0,y))[3] for y in range(pill.height)),0)
        directory = Path(tempfile.mkdtemp())
        source = directory/'plate.jpg'
        Image.new('RGB',(1200,900),(120,120,120)).save(source,'JPEG')
        destination = directory/'story.jpg'
        media.compose_story(source,destination,overlay(text='Back home to \U0001F4CDBali \U0001F334',place='Bali \U0001F1EE\U0001F1E9'))
        with Image.open(destination) as result:
            self.assertEqual(result.size,(1080,1920))


class EmojiRunTests(unittest.TestCase):
    def test_symbols_missing_from_emoji_font_stay_text_and_keycap_is_one_glyph(self):
        from PIL import ImageFont
        self.assertEqual(media.segments('Кафе \u2605 \u2318'),[('text','Кафе \u2605 \u2318')])
        self.assertEqual(media.segments('1\ufe0f\u20e3'),[('emoji','1\ufe0f\u20e3')])
        font = ImageFont.truetype(str(media.FONTS/'InterDisplay-Bold.otf'),60)
        layer = Image.new('RGBA',(400,120),(0,0,0,0))
        media.draw_runs(layer,10,80,'\u2605 1\ufe0f\u20e3',font,(255,255,255))
        self.assertIsNotNone(layer.getbbox())
        self.assertGreater(media.measure('1\ufe0f\u20e3',font),font.size)

    def test_dark_swap_places_and_reports_the_block_actually_pasted(self):
        directory = Path(tempfile.mkdtemp())
        source = directory/'plate.jpg'
        Image.new('RGB',(1200,900),(250,250,250)).save(source,'JPEG')
        light = media.story_block(overlay(place=None),False)[0]
        dark = media.story_block(overlay(place=None),True)[0]
        self.assertNotEqual(light.info['margin'],dark.info['margin'])
        box = media.compose_story(source,directory/'story.jpg',overlay(place=None))
        self.assertEqual((box[2]-box[0],box[3]-box[1]),(dark.width-2*dark.info['margin'],dark.height-2*dark.info['margin']))


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

    def test_attachments_use_the_video_document_as_a_video(self):
        item = {'messages':[{'message_id':12,'document':{**self.document,'mime_type':'video/quicktime','file_name':'a.MOV'}}]}
        values = media.attachments(item)
        self.assertEqual([(a['name'],a['kind'],a['file_id']) for a in values],[('12.mp4','video','f')])


if __name__ == '__main__':
    unittest.main()
