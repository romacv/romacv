import copy
import json
from pathlib import Path
import sys
import unittest

FUNCTIONS = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(FUNCTIONS))
import montage


class MontageTests(unittest.TestCase):
    def setUp(self):
        example = json.loads((FUNCTIONS.parent/'examples/example-montage.json').read_text())
        self.edl, self.sources = example['edl'], example['sources']
        self.edl.setdefault('threads_tag','bali')

    def test_threads_target_needs_threads_tag(self):
        del self.edl['threads_tag']
        self.assertIn('threads needs threads_tag',montage.validate(self.edl,self.sources))
        self.edl['targets'].pop('threads')
        self.edl['copy'].pop('threads')
        self.assertNotIn('threads needs threads_tag',montage.validate(self.edl,self.sources))


    def test_invalid_source_times(self):
        self.assertEqual(montage.validate(self.edl,self.sources),[])
        for start,end in [(-1,3),(1,1),(0,99),(False,2),(0,float('nan')),(0,float('inf'))]:
            with self.subTest(start=start,end=end):
                edl = copy.deepcopy(self.edl)
                edl['targets']['tg-story']['clips'][0].update({'in':start,'out':end})
                self.assertTrue(montage.validate(edl,self.sources))


    def test_titles_cover_whole_timeline(self):
        story = self.edl['targets']['tg-story']
        for cues in ([{'start':0,'end':3,'text':'Jakarta'}],[{'start':0,'end':5,'text':'Jakarta'},{'start':6,'end':11,'text':'Bali'}]):
            story['titles'] = cues
            self.assertTrue(any('titles must cover' in e for e in montage.validate(self.edl,self.sources)))
        story['titles'] = [{'start':0,'end':5,'text':'Jakarta'},{'start':5,'end':11,'text':'Bali'}]
        self.assertEqual(montage.validate(self.edl,self.sources),[])




if __name__ == '__main__':
    unittest.main()
