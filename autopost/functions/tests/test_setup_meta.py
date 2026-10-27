import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.parse


SETUP = Path(__file__).resolve().parents[2] / 'setup/setup.sh'


class SetupMetaTests(unittest.TestCase):
    def setUp(self):
        source = SETUP.read_text().split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        self.namespace = {}
        exec(compile(source.split('\ncommands = ', 1)[0], str(SETUP), 'exec'), self.namespace)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.keys = Path(self.directory.name)
        self.keys.chmod(0o700)
        self.namespace['KEYS'] = self.keys
        self.output = io.StringIO()
        self.addCleanup(patch.stopall)
        patch('time.time', return_value=200000).start()
        self.network = patch('urllib.request.urlopen', side_effect=self.respond).start()
        self.issued = 100000
        self.expires = self.issued + 5184000
        self.username = 'romacv777'
        self.calls = []

    def write(self, name, value):
        path = self.keys / name
        path.write_text(json.dumps(value) if isinstance(value, dict) else value)
        path.chmod(0o600)

    def respond(self, request, timeout):
        self.assertEqual(timeout, 30)
        url = urllib.parse.urlsplit(request.full_url)
        params = urllib.parse.parse_qs(url.query)
        self.calls.append((url, params))
        if url.path.endswith('/me'):
            instagram = url.netloc == 'graph.instagram.com'
            self.assertEqual(params['fields'], ['user_id,username' if instagram else 'id,username'])
            if instagram:
                self.assertEqual(url.path, '/v25.0/me')
            reply = {'user_id' if instagram else 'id': '123', 'username': self.username}
        elif url.path.endswith('/debug_token'):
            reply = {'data': {'is_valid': True, 'user_id': '123', 'issued_at': self.issued, 'expires_at': self.expires}}
        elif url.path == '/access_token':
            self.assertEqual(params['grant_type'], ['th_exchange_token'])
            self.assertEqual(params['client_secret'], ['test-app-secret'])
            reply = {'access_token': 'test-exchanged-token', 'expires_in': 5184000}
        else:
            self.fail('Unexpected endpoint')
        return io.StringIO(json.dumps(reply))

    def run_meta(self):
        with contextlib.redirect_stdout(self.output):
            self.namespace['meta']()

    def test_long_lived_token_written_privately_without_app_secret_or_refresh(self):
        self.write('threads_token.txt', 'test-token')
        self.run_meta()
        path = self.keys / 'threads.json'
        self.assertEqual(json.loads(path.read_text()), {'access_token': 'test-token', 'user_id': '123', 'issued_at': self.issued, 'expires_at': self.expires})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.keys / 'threads_token.txt').exists())
        self.assertEqual(len(self.calls), 2)
        self.assertNotIn('test-token', self.output.getvalue())

    def test_short_lived_exchange_requires_named_secret(self):
        self.issued = 199000
        self.expires = 202600
        self.write('threads_token.txt', 'test-token')
        with self.assertRaisesRegex(self.namespace['IdsError'], 'threads_app_secret.txt'):
            self.run_meta()
        self.assertFalse((self.keys / 'threads.json').exists())
        self.assertTrue((self.keys / 'threads_token.txt').exists())
        self.write('threads_app_secret.txt', 'test-app-secret')
        self.run_meta()
        saved = json.loads((self.keys / 'threads.json').read_text())
        self.assertEqual(saved, {'access_token': 'test-exchanged-token', 'user_id': '123', 'issued_at': 200000, 'expires_at': 5384000})
        self.assertFalse((self.keys / 'threads_token.txt').exists())
        self.assertNotIn('test-app-secret', self.output.getvalue())
        self.assertNotIn('test-exchanged-token', self.output.getvalue())

    def test_different_existing_token_refused_before_network(self):
        self.write('threads_token.txt', 'test-token')
        self.write('threads.json', {'access_token': 'other-token'})
        with self.assertRaisesRegex(self.namespace['IdsError'], 'refusing to overwrite'):
            self.run_meta()
        self.network.assert_not_called()
        self.assertEqual(json.loads((self.keys / 'threads.json').read_text()), {'access_token': 'other-token'})
        self.assertTrue((self.keys / 'threads_token.txt').exists())

    def test_same_existing_token_is_idempotent(self):
        self.write('threads_token.txt', 'test-token')
        self.run_meta()
        before = (self.keys / 'threads.json').read_bytes()
        self.write('threads_token.txt', 'test-token')
        self.run_meta()
        self.assertEqual((self.keys / 'threads.json').read_bytes(), before)
        self.assertFalse((self.keys / 'threads_token.txt').exists())

    def test_wrong_username_expired_token_and_unsafe_files_are_preserved(self):
        self.write('threads_token.txt', 'test-token')
        self.username = 'another-user'
        with self.assertRaisesRegex(self.namespace['IdsError'], 'romacv777'):
            self.run_meta()
        self.username = 'romacv777'
        self.expires = 199999
        with self.assertRaisesRegex(self.namespace['IdsError'], 'expired'):
            self.run_meta()
        (self.keys / 'threads_token.txt').chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, 'mode 600'):
            self.run_meta()
        self.assertFalse((self.keys / 'threads.json').exists())
        self.assertTrue((self.keys / 'threads_token.txt').exists())

    def test_network_errors_do_not_disclose_credentials(self):
        self.write('threads_token.txt', 'test-token')
        self.network.side_effect = RuntimeError('https://example/?access_token=test-token')
        with self.assertRaises(self.namespace['IdsError']) as error:
            self.run_meta()
        self.assertNotIn('test-token', str(error.exception))
        self.assertFalse((self.keys / 'threads.json').exists())

    def test_no_raw_tokens_skips_network(self):
        self.run_meta()
        self.network.assert_not_called()

    def test_fresh_instagram_dashboard_token_uses_documented_lifetime_without_refresh(self):
        self.write('instagram_token.txt', 'test-instagram-token')
        self.run_meta()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(json.loads((self.keys / 'instagram.json').read_text()), {'access_token': 'test-instagram-token', 'user_id': '123', 'issued_at': 200000, 'expires_at': 5384000})
        self.assertFalse((self.keys / 'instagram_token.txt').exists())
        self.assertNotIn('test-instagram-token', self.output.getvalue())

    def test_instagram_existing_metadata_preserves_fresh_token_without_refresh(self):
        value = {'access_token': 'test-instagram-token', 'user_id': '123', 'issued_at': 199900, 'expires_at': 5383900}
        self.write('instagram_token.txt', 'test-instagram-token')
        self.write('instagram.json', value)
        self.run_meta()
        self.assertEqual(json.loads((self.keys / 'instagram.json').read_text()), value)
        self.assertFalse((self.keys / 'instagram_token.txt').exists())
        self.assertEqual(len(self.calls), 1)

    def test_atomic_write_race_does_not_remove_raw_token(self):
        self.write('threads_token.txt', 'test-token')
        original_link = self.namespace['os'].link

        def raced_link(source, destination):
            self.write('threads.json', {'access_token': 'other-token'})
            original_link(source, destination)

        with patch('os.link', side_effect=raced_link):
            with self.assertRaisesRegex(self.namespace['IdsError'], 'refusing to overwrite'):
                self.run_meta()
        self.assertEqual(json.loads((self.keys / 'threads.json').read_text()), {'access_token': 'other-token'})
        self.assertTrue((self.keys / 'threads_token.txt').exists())

    def test_changed_raw_file_is_not_removed(self):
        self.write('threads_token.txt', 'test-token')
        original_write = self.namespace['write_id']

        def write_and_change_raw(name, value):
            original_write(name, value)
            self.write('threads_token.txt', 'replacement-token')

        self.namespace['write_id'] = write_and_change_raw
        self.run_meta()
        self.assertEqual((self.keys / 'threads_token.txt').read_text(), 'replacement-token')

    def test_symlink_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            link = Path(directory) / 'link'
            link.symlink_to(self.keys)
            self.namespace['KEYS'] = link
            with self.assertRaisesRegex(self.namespace['IdsError'], 'without symlinks'):
                self.run_meta()
        self.network.assert_not_called()


if __name__ == '__main__':
    unittest.main()
