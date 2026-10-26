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

    def test_example(self):
        self.assertEqual(montage.validate(self.edl,self.sources),[])

    def test_invalid_source_times(self):
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

    def test_subtitle_lines_and_times(self):
        for cue in [{'start':0,'end':1,'lines':['x'*33]}, {'start':0,'end':1,'lines':['a','b','c']}, {'start':0,'end':1,'lines':['{override}']}, {'start':0,'end':99,'lines':['text']}, {'start':0,'end':1,'lines':['a\nb']}]:
            with self.subTest(cue=cue):
                edl = copy.deepcopy(self.edl)
                edl['targets']['tg-story']['subtitles'] = [cue]
                self.assertTrue(montage.validate(edl,self.sources))
        self.edl['targets']['tg-story']['subtitles'] *= 2
        self.assertTrue(montage.validate(self.edl,self.sources))

    def test_malformed_structures_return_errors(self):
        for value in (None, [], 'text', {'targets':{'tg-story':None}}, {'targets':{'tg-story':{'clips':None,'subtitles':[]}}}):
            self.assertTrue(montage.validate(value,self.sources))
        for field,value in [('source',[]),('source','missing'),('in',{}),('out',None)]:
            edl = copy.deepcopy(self.edl)
            edl['targets']['tg-story']['clips'][0][field] = value
            self.assertTrue(montage.validate(edl,self.sources))
        self.edl['cover'] = {'target':[],'time':0}
        self.assertTrue(montage.validate(self.edl,self.sources))

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
