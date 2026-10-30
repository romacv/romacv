import unittest
from datetime import datetime, timezone
from unittest import mock

import agents
import drafter
import geo
import platforms

PLACE = {'name': 'Tai Hang', 'address': 'Hong Kong', 'lat': 22.28, 'lng': 114.19, 'maps_url': 'https://maps.google.com/?cid=1'}


class PlaceTest(unittest.TestCase):
    def test_place_is_server_owned_after_a_model_edit(self):
        forged = {**PLACE, 'name': 'Evil', 'maps_url': 'https://evil.example/x'}
        stored = {'location': 'HK', 'place': {**PLACE, 'boxes': {'tg-story': {}}}}
        spec = {'location': 'HK', 'place': forged, 'copy': {'tg-channel': 'Привет'}, 'media': {}}
        agents.own_place(spec, stored)
        self.assertEqual(spec['place'], PLACE)
        self.assertNotIn('evil', platforms.text_for(spec, 'tg-channel'))
        spec = {'location': 'Bali', 'place': forged, 'copy': {'tg-channel': 'Привет'}, 'media': {}}
        with mock.patch.object(geo, 'place', return_value=None):
            agents.own_place(spec, stored)
        self.assertNotIn('place', spec)
        self.assertEqual(platforms.text_for(spec, 'tg-channel'), 'Привет')

    def test_validate_accepts_only_google_maps_links(self):
        base = {'slug': 'x', 'kind': 'personal', 'at': datetime.now(timezone.utc).isoformat(), 'targets': ['tg-channel'], 'media': {}, 'copy': {'tg-channel': 'Привет'}, 'approval': {}}
        for url, name, ok in (('https://maps.google.com/?cid=1', 'Tai Hang', True), ('https://www.google.com/maps/place/x', 'Tai Hang', True), ('https://evil.example/maps', 'Tai Hang', False), ('https://maps.google.com.evil.example/maps', 'Tai Hang', False), ('https://www.google.com/search', 'Tai Hang', False), ('https://maps.google.com/?cid=1', 'see https://evil.example', False)):
            self.assertEqual(not drafter.validate({**base, 'place': {**PLACE, 'maps_url': url, 'name': name}}, copy_only=True), ok, url)


if __name__ == '__main__':
    unittest.main()
