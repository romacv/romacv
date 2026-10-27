from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from fake_firestore import FakeFirestore
from store import Store
import main as app
import platforms as p


class TokenSourceTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(FakeFirestore())
        self.now = 2000000
        self.config = {'webhook_url':'test','github_pat':'test','roman_chat_id':1}
        p.configure(self.config,self.store,'.')

    def token(self, issued, scopes=None):
        return {'access_token':str(issued),'user_id':'1','issued_at':issued,'expires_at':self.now+86400,'scopes':scopes or []}

    def daily(self):
        with patch.object(app.time,'time',return_value=self.now), patch.object(p,'telegram',return_value={'url':'test'}), patch('urllib.request.urlopen',side_effect=OSError()):
            app.maintenance(self.config,self.store,Mock())

    def test_newer_candidate_wins_for_each_provider(self):
        for provider in ('threads','instagram'):
            with self.subTest(provider=provider):
                original = self.token(300)
                saved = self.token(100)
                self.config[provider] = original
                self.store.put('tokens/'+provider,saved)
                self.assertEqual(p.key(provider+'-token.json'),original)
                saved['refreshed_at'] = 400
                self.store.put('tokens/'+provider,saved)
                self.assertEqual(p.key(provider+'-token.json'),saved)
                saved['refreshed_at'] = 300
                self.store.put('tokens/'+provider,saved)
                self.assertEqual(p.key(provider+'-token.json'),saved)

    def test_missing_candidates_and_business(self):
        self.assertEqual(p.key('threads-token.json'),{})
        self.config['threads'] = self.token(100)
        self.assertEqual(p.key('threads-token.json'),self.config['threads'])
        self.store.put('tokens/business',{'business_connection_id':'1'})
        self.assertEqual(p.key('telegram-business.json'),{'business_connection_id':'1'})

    def test_refresh_uses_winner_and_preserves_its_scopes(self):
        for provider in ('threads','instagram'):
            with self.subTest(provider=provider):
                self.config.clear()
                self.config.update(webhook_url='test',github_pat='test')
                original = self.token(self.now-86400,['threads_basic','threads_delete'])
                self.config[provider] = original
                self.store.put('tokens/'+provider,self.token(100,['threads_basic']))
                with patch.object(p,'request',return_value={'access_token':'refreshed','expires_in':5184000}) as request, patch.object(app,'tell'):
                    self.daily()
                self.assertEqual(request.call_args.args[2]['access_token'],original['access_token'])
                saved = self.store.get('tokens/'+provider)
                self.assertEqual(saved['scopes'],original['scopes'])
                self.assertEqual(saved['refreshed_at'],self.now)
                self.assertEqual(p.key(provider+'-token.json'),saved)
                self.store.db.document('tokens/'+provider).delete()

    def test_delete_uses_scopes_of_winning_token(self):
        self.config['threads'] = self.token(300,['threads_basic','threads_delete'])
        self.store.put('tokens/threads',self.token(100,['threads_basic']))
        with patch.object(p,'personal_identity',return_value={'id':'1'}):
            self.assertEqual(p.social_session('threads',True)[0],'300')
            self.store.put('tokens/threads',self.token(400,['threads_basic']))
            with self.assertRaisesRegex(p.Error,'Missing Threads permissions'):
                p.social_session('threads',True)

    def test_expired_tokens_skip_refresh_and_notify_once_per_day(self):
        for provider in ('threads','instagram'):
            self.config[provider] = {**self.token(100),'expires_at':self.now}
        with patch.object(p,'request') as request, patch.object(app,'tell') as tell:
            self.daily()
            self.daily()
            request.assert_not_called()
            messages = [call.args[1] for call in tell.call_args_list if 'нужен новый токен' in call.args[1]]
            self.assertEqual(messages,[provider+': нужен новый токен: Generate, setup.sh meta, secret' for provider in ('instagram','threads')])
            self.now += 86400
            self.daily()
            self.assertEqual(sum('нужен новый токен' in call.args[1] for call in tell.call_args_list),4)
