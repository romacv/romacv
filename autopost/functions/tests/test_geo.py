import unittest
from unittest import mock

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


if __name__ == '__main__':
    unittest.main()
