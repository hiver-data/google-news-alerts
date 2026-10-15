"""Offline HTTP-contract tests. No Google or Apify traffic, tokens or charges."""
import asyncio
import copy
import importlib.metadata
import inspect
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import decode_links
from hiver_gnewsdecoder import gnewsdecoder, gnews_decoder_async
import hiver_gnewsdecoder as shim

FIXTURES = Path(__file__).parent / 'fixtures'
BOARD = json.loads((FIXTURES / 'board-737RxMYPBd1ccXsKk.json').read_text())
LINKS = BOARD['input']['articleUrls']
ROWS = BOARD['rows']


class Http:
    def __init__(self, rows=ROWS, status='SUCCEEDED'):
        self.rows, self.status, self.calls = rows, status, []

    def __call__(self, request, timeout):
        payload = json.loads(request.data) if request.data else None
        self.calls.append((request, timeout, payload))
        path = urlsplit(request.full_url).path
        if path.endswith('/runs'):
            data = {'data': {'id': 'fixture', 'status': self.status, 'defaultDatasetId': 'fixture-dataset'}}
        elif '/actor-runs/' in path:
            data = {'data': {'id': 'fixture', 'status': 'SUCCEEDED', 'defaultDatasetId': 'fixture-dataset'}}
        elif '/datasets/' in path:
            q = parse_qs(urlsplit(request.full_url).query)
            offset, limit = int(q['offset'][0]), int(q['limit'][0])
            data = self.rows[offset:offset+limit]
        else:
            raise AssertionError(request.full_url)
        return io.BytesIO(json.dumps(data).encode())


def expected(row):
    if row['status'] == 'decoded':
        return {'success': True, 'decoded_url': row['url']}
    return {'success': False, 'message': row['status'].removeprefix('not_decoded: ')}


class ShimTests(unittest.TestCase):
    def call(self, links=LINKS, http=None, **kwargs):
        http = http or Http()
        with patch.object(shim, 'urlopen', http):
            return gnewsdecoder(links, apify_token='offline-test-token', **kwargs), http

    def test_board_batch_exact_order_duplicates_and_errors(self):
        result, http = self.call()
        self.assertEqual(result, [expected(r) for r in ROWS[:5]] + [expected(ROWS[0])] + [expected(r) for r in ROWS[5:]])
        self.assertEqual(len(result), len(LINKS))
        self.assertEqual(sum(c[0].get_method() == 'POST' for c in http.calls), 1)
        valid = [url for url in LINKS if 'news.google.com/' in url]
        self.assertEqual(http.calls[0][2], {'articleUrls': valid, 'maxItems': len(valid)})

    def test_scalar_one_dict_one_run(self):
        result, http = self.call(LINKS[0])
        self.assertEqual(result, expected(ROWS[0]))
        self.assertEqual(sum(c[0].get_method() == 'POST' for c in http.calls), 1)

    def test_reordered_dataset_not_zipped_by_position(self):
        self.assertEqual(self.call(http=Http(list(reversed(ROWS))))[0], self.call()[0])

    def test_read_path_and_query_alias_reuse_article_id(self):
        alias = LINKS[0].replace('/rss/articles/', '/read/').split('?')[0] + '?hl=fr&oc=5'
        result, _ = self.call([alias, LINKS[0], alias])
        self.assertEqual(result, [expected(ROWS[0])] * 3)
        self.assertIsNot(result[0], result[1])

    def test_missing_row_is_failure_not_wrong_company_url(self):
        result, _ = self.call(http=Http(ROWS[1:]))
        self.assertFalse(result[0]['success'])
        self.assertFalse(result[5]['success'])
        self.assertEqual(result[1], expected(ROWS[1]))

    def test_free_refusal_reason_no_retry(self):
        row = dict(ROWS[0], url=None, status='not_decoded: Google rate-limited, retry later')
        result, http = self.call(LINKS[0], Http([row]))
        self.assertEqual(result, {'success': False, 'message': 'Google rate-limited, retry later'})
        self.assertEqual(len(http.calls), 2)

    def test_pagination_and_polling_still_only_one_run(self):
        original = decode_links.decode
        def no_wait(*args, **kwargs):
            return original(*args, **kwargs, pause=lambda seconds: None)
        with patch.object(decode_links, 'PAGE', 2), patch.object(decode_links, 'decode', side_effect=no_wait):
            result, http = self.call(http=Http(status='RUNNING'))
        self.assertEqual(result, self.call()[0])
        self.assertEqual(sum(c[0].get_method() == 'POST' for c in http.calls), 1)
        self.assertEqual(len([c for c in http.calls if '/datasets/' in c[0].full_url]), 4)

    def test_memory_charge_cap_and_timeout(self):
        _, http = self.call(timeout=3.5, max_charge=0.03)
        query = parse_qs(urlsplit(http.calls[0][0].full_url).query)
        self.assertEqual(query['memory'], ['512'])
        self.assertEqual(query['maxTotalChargeUsd'], ['0.03'])
        self.assertTrue(all(c[1] == 3.5 for c in http.calls))
        self.assertNotIn('offline-test-token', http.calls[0][0].full_url)

    def test_none_timeout_and_ignored_proxy_interval(self):
        result, http = self.call(timeout=None, proxy='http://unused.invalid', interval=99)
        self.assertEqual(result, self.call()[0])
        self.assertTrue(all(c[1] is None for c in http.calls))

    def test_environment_token(self):
        http = Http()
        with patch.dict(os.environ, {'APIFY_TOKEN': 'offline-env-token'}), patch.object(shim, 'urlopen', http):
            self.assertTrue(gnewsdecoder(LINKS[0])['success'])
        self.assertEqual(http.calls[0][0].get_header('Authorization'), 'Bearer offline-env-token')

    def test_missing_token_before_network(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(shim, 'urlopen') as request:
            with self.assertRaisesRegex(ValueError, 'APIFY_TOKEN'):
                gnewsdecoder(LINKS[0])
            request.assert_not_called()

    def test_empty_invalid_and_feed_do_not_launch(self):
        with patch.object(shim, 'urlopen') as request:
            self.assertEqual(gnewsdecoder([]), [])
            for url in ['', 'https://example.com/news/story', 'https://news.google.com/rss/search?q=test']:
                self.assertEqual(set(gnewsdecoder(url)), {'success', 'message'})
                self.assertFalse(gnewsdecoder(url)['success'])
            request.assert_not_called()

    def test_argument_validation(self):
        for value in [None, (), 5, [None], [5]]:
            with self.assertRaises(TypeError):
                self.call(value)
        for kwargs in [{'timeout': 0}, {'max_charge': 0}]:
            with self.assertRaises(ValueError):
                self.call(**kwargs)
        with self.assertRaises(ValueError):
            self.call([LINKS[0]] * 5001)

    def test_http_401_429_no_retry_no_secret_in_message(self):
        for code in [401, 429]:
            with patch.object(shim, 'urlopen', side_effect=HTTPError('x', code, 'offline-test-token', {}, None)) as request:
                result = gnewsdecoder(LINKS, apify_token='offline-test-token')
            self.assertEqual(request.call_count, 1)
            self.assertIn(str(code), result[0]['message'])
            self.assertNotIn('offline-test-token', str(result))

    def test_connection_failure_no_retry(self):
        with patch.object(shim, 'urlopen', side_effect=URLError('offline-test-token')) as request:
            result = gnewsdecoder(LINKS[0], apify_token='offline-test-token')
        self.assertEqual(request.call_count, 1)
        self.assertFalse(result['success'])
        self.assertNotIn('offline-test-token', str(result))

    def test_actor_failure_no_retry(self):
        result, http = self.call(http=Http(status='FAILED'))
        self.assertFalse(result[0]['success'])
        self.assertEqual(len(http.calls), 1)

    def test_async_batch_and_scalar(self):
        for links in [LINKS, LINKS[0]]:
            http = Http()
            with patch.object(shim, 'urlopen', http):
                result = asyncio.run(gnews_decoder_async(links, 99, 'http://ignored', None, 1, apify_token='offline-test-token'))
            self.assertEqual(result, self.call(links)[0])
            self.assertEqual(sum(c[0].get_method() == 'POST' for c in http.calls), 1)

    def test_real_query_captures_in_labelled_reconstructed_envelopes(self):
        provenance = json.loads((FIXTURES / 'query-provenance.json').read_text())
        for name, url in provenance['expected'].items():
            # Parse real double-encoded Google captures independently; not an Actor test.
            capture = (FIXTURES / name).read_text()
            frame = next(json.loads(line) for line in capture.splitlines() if line.startswith('[["wrb.fr"'))
            decoded_url = json.loads(frame[0][2])[1]
            self.assertEqual(decoded_url, url)
            reconstructed_row = dict(ROWS[0], url=decoded_url)
            result, _ = self.call(LINKS[0], Http([reconstructed_row]))
            self.assertEqual(result, {'success': True, 'decoded_url': url})

    def test_no_dependency_on_googlenewsdecoder_import(self):
        script = '''import sys
class Block:
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'googlenewsdecoder', 'selectolax', 'httpx'}:
            raise ImportError('deliberately unavailable')
sys.meta_path.insert(0, Block())
from hiver_gnewsdecoder import gnewsdecoder
assert gnewsdecoder([]) == []
'''
        subprocess.run([sys.executable, '-c', script], check=True)

    def test_installed_reference_function_contract_if_available(self):
        try:
            version = importlib.metadata.version('googlenewsdecoder')
        except importlib.metadata.PackageNotFoundError:
            self.skipTest('optional googlenewsdecoder reference is not installed')
        self.assertEqual(version, '0.2.1')
        # Its import may be broken with selectolax>=1: shim must still work.
        try:
            import googlenewsdecoder as reference
        except ImportError:
            self.assertEqual(gnewsdecoder([]), [])
            return
        self.assertEqual(list(inspect.signature(reference.gnewsdecoder).parameters),
                         list(inspect.signature(gnewsdecoder).parameters)[:4])
        with patch.object(reference.GoogleDecoder, 'decode_google_news_urls', return_value=self.call()[0]):
            self.assertEqual(reference.gnewsdecoder(LINKS), self.call()[0])
        with patch.object(reference.GoogleDecoder, 'decode_google_news_url', return_value=expected(ROWS[0])):
            self.assertEqual(reference.gnewsdecoder(LINKS[0]), self.call(LINKS[0])[0])


if __name__ == '__main__':
    unittest.main()
