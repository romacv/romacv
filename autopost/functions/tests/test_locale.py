import json
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import drafter
import media


class LocaleTests(unittest.TestCase):
    def test_drafter_receives_current_local_time_with_offset(self):
        import anthropic
        instant = datetime(2026,10,26,20,30,tzinfo=timezone.utc)
        for zone in ('Asia/Makassar','America/New_York'):
            with self.subTest(zone=zone):
                client = Mock()
                client.messages.create.return_value = Mock(content=[Mock(type='text',text='{}')])
                payload = {'text':'завтра в 9'}
                with patch.object(anthropic,'Anthropic',return_value=client), patch.object(drafter,'datetime') as clock:
                    clock.now.side_effect = lambda tz:instant.astimezone(tz)
                    drafter.ask({'anthropic_key':'unused','timezone':zone},payload,[],lambda value:[])
                supplied = json.loads(client.messages.create.call_args.kwargs['messages'][0]['content'][0]['text'])
                self.assertEqual(supplied['timezone'],zone)
                self.assertEqual(supplied['current_time'],instant.astimezone(ZoneInfo(zone)).isoformat())
                self.assertEqual(supplied['text'],'завтра в 9')
                self.assertEqual(payload,{'text':'завтра в 9'})
                clock.now.assert_called_once_with(ZoneInfo(zone))

    def test_speech_accepts_both_languages_and_word_offsets(self):
        from google.cloud import speech
        for filename in ('voice.ogg','montage.mp4'):
            with self.subTest(filename=filename):
                client = Mock()
                client.long_running_recognize.return_value.result.return_value = Mock(results=[])
                bucket = Mock(name='bucket')
                bucket.name = 'test-bucket'
                store = Mock()
                config = {'prepared_audio':{filename:{'audio':True,'duration':3.2,'object':'prepared/audio.flac'}}}
                with patch.object(speech,'SpeechClient',return_value=client):
                    self.assertEqual(media.transcribe(bucket,store,Path(filename),'source/' + filename,config),{'text':'','words':[]})
                request = client.long_running_recognize.call_args.kwargs
                self.assertIsInstance(request['config'],speech.RecognitionConfig)
                self.assertEqual(request['config'].language_code,'ru-RU')
                self.assertEqual(list(request['config'].alternative_language_codes),['en-US'])
                self.assertTrue(request['config'].enable_word_time_offsets)
                self.assertTrue(request['config'].enable_automatic_punctuation)
                self.assertEqual(request['audio'].uri,'gs://test-bucket/prepared/audio.flac')
                store.reserve_speech.assert_called_once_with(4,'source/' + filename)
