import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import alerts

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/bench-samples.json').read_text())
ROWS = FIXTURE['rows']
CONFIG = {'queries': ['Reuters/Ipsos poll AI risks'], 'monitorId': 'offline-test'}

class AlertsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR'))
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / 'seen.json'
        self.output = Path(self.tmp.name) / 'digest.md'

    def run_alerts(self, rows=ROWS, **kwargs):
        return alerts.run(CONFIG, self.state, self.output, 'offline-token', fetch=lambda *_: copy.deepcopy(rows), **kwargs)

    def test_same_article_twice_one_alert_across_runs(self):
        delivered = []
        def send(rows, *_): delivered.extend(rows)
        self.assertEqual(self.run_alerts([ROWS[0]], send=send), 1)
        self.assertEqual(self.run_alerts([ROWS[0]], send=send), 0)
        self.assertEqual(len(delivered), 1)

    def test_duplicates_within_run(self):
        self.assertEqual(self.run_alerts([ROWS[0], ROWS[0]]), 1)

    def test_real_article_url_not_publisher_homepage(self):
        self.run_alerts([ROWS[0]])
        digest = self.output.read_text()
        self.assertIn('](' + ROWS[0]['url'] + ')', digest)
        self.assertNotIn('](' + ROWS[0]['publisherUrl'] + ')', digest)

    def test_url_decoding_change_still_dedupes(self):
        self.run_alerts([ROWS[0]])
        changed = dict(ROWS[0], url='https://news.google.com/rss/articles/offline', urlResolved=False)
        self.assertEqual(self.run_alerts([changed]), 0)

    def test_new_article_in_later_run(self):
        self.assertEqual(self.run_alerts([ROWS[0]]), 1)
        self.assertEqual(self.run_alerts(ROWS), 1)
        self.assertEqual(len(json.loads(self.state.read_text())['seen']), 2)

    def test_note_and_error_rows_not_alerts(self):
        self.assertEqual(self.run_alerts([{'note': 'No new articles.'}, dict(ROWS[0], error='refused'), dict(ROWS[0], success=False)]), 0)

    def test_unresolved_link_explicit(self):
        row = dict(ROWS[0], urlResolved=False, url='https://news.google.com/rss/articles/offline')
        self.assertIn('unresolved Google/feed link', alerts.digest([row]))

    def test_actor_only_new_and_monitor_input(self):
        data = alerts.actor_input(CONFIG)
        self.assertTrue(data['onlyNew'])
        self.assertEqual(data['monitorId'], 'offline-test')
        self.assertTrue(data['resolvePublisherUrl'])

    def test_topic_only_input(self):
        self.assertEqual(alerts.actor_input({'topics': ['TECHNOLOGY']})['topics'], ['TECHNOLOGY'])

    def test_invalid_config_before_fetch(self):
        for cfg in ({}, {'queries': 'AI'}, {'topics': ['INVALID']}, dict(CONFIG, monitorId='a/b'), dict(CONFIG, country='usa'), dict(CONFIG, timeWindow='forever'), dict(CONFIG, maxItems=0), dict(CONFIG, onlyNew='false')):
            with self.subTest(cfg=cfg), self.assertRaises(ValueError): alerts.actor_input(cfg)

    def test_delivery_failure_retains_pending_and_replays_without_fetch(self):
        def fail(*_): raise RuntimeError('offline delivery failure')
        with self.assertRaises(RuntimeError): self.run_alerts([ROWS[0]], send=fail)
        state = json.loads(self.state.read_text())
        self.assertEqual(len(state['pending']), 1)
        self.assertEqual(state['seen'], [])
        def never_fetch(*_): raise AssertionError('must replay pending first')
        self.assertEqual(alerts.run(CONFIG, self.state, self.output, 'offline-token', fetch=never_fetch), 1)
        self.assertEqual(json.loads(self.state.read_text())['pending'], [])

    def test_corrupt_state_fails_closed(self):
        self.state.write_text('{broken')
        with self.assertRaises(ValueError): self.run_alerts()

    def test_slack_and_discord_mock_payload(self):
        calls = []
        def api(url, **kwargs): calls.append(kwargs['payload'])
        for kind in ('slack', 'discord'):
            alerts.deliver([ROWS[0]], self.output, 'https://example.invalid/webhook', kind, api)
        self.assertIn(ROWS[0]['url'], calls[0]['text'])
        self.assertIn(ROWS[0]['url'], calls[1]['content'])
        self.assertEqual(calls[1]['allowed_mentions'], {'parse': []})

    def test_api_mock_start_poll_dataset(self):
        calls = []
        responses = [{'data': {'id': 'offline-run', 'status': 'RUNNING'}}, {'data': {'id': 'offline-run', 'status': 'SUCCEEDED', 'defaultDatasetId': 'offline-data'}}, ROWS]
        def api(*args): calls.append(args); return responses.pop(0)
        self.assertEqual(alerts.fetch_articles(alerts.actor_input(CONFIG), 'offline-token', api=api, pause=lambda _: None), ROWS)
        self.assertIn('/acts/hiver~google-news-scraper/runs?', calls[0][0])
        self.assertIn('/datasets/offline-data/items?', calls[2][0])

    def test_api_failed_run_no_dataset(self):
        def api(*_): return {'data': {'id': 'offline', 'status': 'FAILED'}}
        with self.assertRaises(RuntimeError): alerts.fetch_articles(alerts.actor_input(CONFIG), 'offline-token', api=api)

    def test_api_deadline(self):
        times = iter([0, 241])
        def api(*_): return {'data': {'id': 'offline', 'status': 'RUNNING'}}
        with self.assertRaises(RuntimeError): alerts.fetch_articles(alerts.actor_input(CONFIG), 'offline-token', api=api, clock=lambda: next(times))

    def test_webhook_https_required(self):
        with self.assertRaises(ValueError): alerts.deliver(ROWS, self.output, 'http://example.invalid', api=lambda *_: None)

    def test_slack_plain_ok_response(self):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'ok'
        with patch('alerts.urlopen', return_value=response):
            self.assertEqual(alerts.request_json('https://example.invalid', payload={'text': 'test'}), 'ok')

    def test_apify_malformed_response_rejected(self):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'not-json'
        with patch('alerts.urlopen', return_value=response), self.assertRaises(ValueError):
            alerts.request_json(alerts.API + '/offline', token='offline-token')

    def test_no_network_in_full_two_run_digest(self):
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network forbidden')):
            self.assertEqual(self.run_alerts(), 2)
            self.assertEqual(self.run_alerts(), 0)
            self.assertIn('No new articles.', self.output.read_text())

if __name__ == '__main__':
    unittest.main()
