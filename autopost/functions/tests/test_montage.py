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

    def test_invalid_source_times(self):
        self.assertEqual(montage.validate(self.edl,self.sources),[])
        for start,end in [(-1,3),(1,1),(0,99),(False,2),(0,float('nan')),(0,float('inf'))]:
            with self.subTest(start=start,end=end):
                edl = copy.deepcopy(self.edl)
                edl['targets']['tg-story']['clips'][0].update({'in':start,'out':end})
                self.assertTrue(montage.validate(edl,self.sources))

    def test_story_total_includes_cover_frame(self):
        self.sources['long'] = 100
        self.edl['targets']['tg-story']['clips'] = [{'source':'long','in':0,'out':60}]
        self.assertTrue(any('cover frame' in e for e in montage.validate(self.edl,self.sources)))
        self.edl['targets']['tg-story']['clips'][0]['out'] = 59.9
        self.assertEqual(montage.validate(self.edl,self.sources),[])

    def test_cover_copy_and_language(self):
        self.edl['cover']['time'] = 100
        self.assertTrue(montage.validate(self.edl,self.sources))
        self.edl['cover']['time'] = 1
        self.edl['copy']['tg-story'] = 'English copy instead of Russian.'
        self.assertTrue(any('language' in e for e in montage.validate(self.edl,self.sources)))
        self.edl['copy'].pop('threads')
        self.assertTrue(any('Copy required' in e for e in montage.validate(self.edl,self.sources)))


if __name__ == '__main__':
    unittest.main()
