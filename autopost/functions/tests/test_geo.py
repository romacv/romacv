import unittest
from datetime import datetime, timezone
from unittest import mock

import drafter
import geo
import platforms


class GeoTest(unittest.TestCase):
    def setUp(self):
        geo.CACHE.clear()

    def test_query_prefers_place_with_city_then_city(self):
        both = {'places': [{'name': 'Tai Hang', 'city': 'Hong Kong', 'confidence': 'high'}]}
        city = {'places': [{'name': '', 'city': 'Hong Kong', 'confidence': 'high'}]}
        guess = {'places': [{'name': 'Tai Hang', 'city': 'Hong Kong', 'confidence': 'medium'}]}
        self.assertEqual(geo.query(both), 'Tai Hang, Hong Kong')
        self.assertEqual(geo.query(city), 'Hong Kong')
        self.assertIsNone(geo.query(guess))
        self.assertIsNone(geo.query({'places': []}))

    def test_falls_back_to_city_and_caches(self):
        with mock.patch.object(geo, 'search', side_effect=[None, '123']) as search:
            self.assertEqual(geo.location({}, 'Tai Hang, Hong Kong', 'instagram'), '123')
            self.assertEqual(geo.location({}, 'Tai Hang, Hong Kong', 'instagram'), '123')
        self.assertEqual(search.call_count, 2)

    def test_fail_soft_and_fixed_map(self):
        with mock.patch.object(geo, 'search', side_effect=RuntimeError('x')):
            self.assertIsNone(geo.location({}, 'Hong Kong', 'threads'))
        self.assertEqual(geo.location({'locations': {'instagram': {'hong kong': 99}}}, 'Hong Kong', 'instagram'), '99')

    def publish(self, found, reject=False):
        posted = []
        store = mock.Mock()
        store.checkpoint.side_effect = lambda slug, step, action: action()
        def request(method, url, fields=None, **kwargs):
            if method == 'POST':
                posted.append(dict(fields))
                if reject and 'location_id' in fields:
                    raise platforms.Error('bad location', status_code=400)
                return {'id': '1'}
            return {'shortcode': 'abc'}
        platforms.CONTEXT.set(({}, store, None))
        spec = {'slug': 'x', 'location': 'Hong Kong', 'media': {}, 'copy': {'threads': 'hi'}}
        with mock.patch.object(geo, 'location', **found), mock.patch.object(platforms, 'request', request), \
                mock.patch.object(platforms, 'quota'), mock.patch.object(platforms, 'ready'):
            platforms.post_social(spec, {'steps': {}}, 'threads', {}, ('t', 'u', 'b'))
        return [p for p in posted if 'creation_id' not in p][-1]

    def test_location_id_joins_params(self):
        self.assertEqual(self.publish({'return_value': '77'})['location_id'], '77')

    def test_failed_lookup_still_publishes(self):
        self.assertNotIn('location_id', self.publish({'side_effect': RuntimeError('x')}))

    def test_rejected_location_retries_without_it(self):
        self.assertNotIn('location_id', self.publish({'return_value': '77'}, reject=True))


PLACE = {'name': 'Tai Hang', 'address': 'Hong Kong', 'lat': 22.28, 'lng': 114.19, 'maps_url': 'https://maps.google.com/?cid=1'}


class PlaceTest(unittest.TestCase):
    def test_story_gets_location_area_over_the_drawn_pill(self):
        spec = {'place': {**PLACE, 'boxes': {'tg-story': {'pill': [270, 480, 810, 576], 'foot': [200, 480, 880, 800]}}}}
        area = platforms.location_area(spec)
        self.assertEqual(area['type'], {'type': 'location', 'latitude': 22.28, 'longitude': 114.19})
        self.assertEqual((area['position']['x_percentage'], area['position']['y_percentage']), (50.0, 27.5))

    def test_story_area_without_box_avoids_links_and_text(self):
        spec = {'place': {**PLACE, 'boxes': {'tg-story': {'pill': None, 'foot': [100, 300, 900, 700]}}}}
        self.assertGreater(platforms.location_area(spec)['position']['y_percentage'], 36.5)
        self.assertIsNone(platforms.location_area({}))

    def test_carousel_never_carries_location_id(self):
        posted = []
        store = mock.Mock()
        store.checkpoint.side_effect = lambda slug, step, action: action()
        def request(method, url, fields=None, **kwargs):
            if method == 'POST':
                posted.append(dict(fields))
                return {'id': '1'}
            return {'shortcode': 'abc'}
        platforms.CONTEXT.set(({}, store, None))
        images = [{'path': 'a.jpg', 'alt': 'a'}, {'path': 'b.jpg', 'alt': 'b'}]
        spec = {'slug': 'x', 'location': 'Hong Kong', 'place': PLACE, 'media': {'images': images}, 'copy': {'ig-post': 'hi'}}
        urls = {'image0': 'u0', 'image1': 'u1'}
        with mock.patch.object(geo, 'location', return_value='77') as lookup, mock.patch.object(platforms, 'request', request), \
                mock.patch.object(platforms, 'quota'), mock.patch.object(platforms, 'ready'), mock.patch.object(platforms, 'ready_all'), \
                mock.patch.object(platforms, 'probe'), mock.patch.object(platforms, 'media_files', return_value={'image0': None, 'image1': None}):
            platforms.post_social(spec, {'steps': {}}, 'ig-post', urls, ('t', 'u', 'b'))
        self.assertFalse(any('location_id' in p for p in posted))
        lookup.assert_not_called()

    def test_place_lookup_failure_is_soft_and_found_is_cached(self):
        geo.FOUND.clear()
        with mock.patch.object(geo, 'fetch', side_effect=RuntimeError('x')):
            self.assertIsNone(geo.place('Hong Kong'))
        with mock.patch.object(geo, 'fetch', return_value=PLACE) as fetch:
            self.assertEqual(geo.place('Hong Kong'), PLACE)
            self.assertEqual(geo.place(' hong  kong '), PLACE)
        self.assertEqual(fetch.call_count, 1)
        geo.FOUND.clear()


    def test_channel_copy_is_validated_without_place_line_and_fits(self):
        spec = {'slug': 'x', 'kind': 'personal', 'at': datetime.now(timezone.utc).isoformat(), 'targets': ['tg-channel'], 'media': {}, 'copy': {'tg-channel': 'Привет'}, 'approval': {}, 'place': PLACE}
        self.assertEqual(platforms.text_for(spec, 'tg-channel'), 'Привет\n\nTai Hang: https://maps.google.com/?cid=1')
        self.assertEqual(drafter.validate(spec, copy_only=True), [])
        spec['copy']['tg-channel'] = 'Я' * 4090
        self.assertEqual(platforms.text_for(spec, 'tg-channel'), 'Я' * 4090)

    def test_threads_link_respects_bytes(self):
        sent = []
        def run(text):
            store = mock.Mock()
            store.checkpoint.side_effect = lambda slug, step, action: action()
            platforms.CONTEXT.set(({}, store, None))
            spec = {'slug': 'x', 'location': 'HK', 'place': PLACE, 'media': {}, 'copy': {'threads': text}}
            with mock.patch.object(geo, 'location', return_value=None), mock.patch.object(platforms, 'request', lambda m, u, f=None, **k: sent.append(dict(f or {})) or {'id': '1', 'shortcode': 'a'}), \
                    mock.patch.object(platforms, 'quota'), mock.patch.object(platforms, 'ready'):
                platforms.post_social(spec, {'steps': {}}, 'threads', {}, ('t', 'u', 'b'))
            return [p for p in sent if 'text' in p][-1]['text']
        self.assertTrue(run('Факт.').endswith(PLACE['maps_url']))
        sent.clear()
        self.assertEqual(run('Я' * 240), 'Я' * 240)

    def test_closing_question_forms(self):
        for text in ('Факт.\nА вы?', 'Факт.\n«Вы бы?»', 'Факт.\nВы бы? (да)?)', 'Факт.\nА вы? \U0001F642', 'Факт.\nА вы?\n#one'):
            self.assertTrue(platforms.closing_question(text), text)
        self.assertFalse(platforms.closing_question('Факт.\nА вы?', 'А вы?'))
        self.assertFalse(platforms.closing_question('Факт.'))


if __name__ == '__main__':
    unittest.main()
