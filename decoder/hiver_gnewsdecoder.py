"""Function-level googlenewsdecoder 0.2.1 shapes using one Apify run per batch.

No googlenewsdecoder dependency, direct Google requests, or automatic retries.
"""
import asyncio
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import decode_links

__all__ = ["gnewsdecoder", "gnews_decoder_async"]


def _error(message):
    return {"success": False, "message": message}


def _key(url):
    """Match the Actor's article-id de-duplication across paths and queries."""
    try:
        parsed = urlsplit(url)
        parts = parsed.path.rstrip('/').split('/')
        if parsed.hostname == 'news.google.com' and len(parts) >= 3 and parts[-2] in {'articles', 'read'}:
            return ('article', parts[-1])
    except ValueError:
        pass
    return ('literal', url)


def _result(row):
    if row.get('status') == 'decoded' and row.get('url'):
        return {'success': True, 'decoded_url': row['url']}
    status = str(row.get('status') or 'No decoded URL returned by Actor.')
    return _error(status.removeprefix('not_decoded: '))


def gnewsdecoder(source_url, interval=None, proxy=None, timeout=10.0, *, apify_token=None, max_charge=0.50):
    """Return one dict for a string, ordered dicts (including duplicates) for a list.

    interval/proxy are ignored; pacing is Actor-side and refusals are not retried.
    timeout is per Apify HTTP operation (None disables the HTTP timeout).
    apify_token defaults to APIFY_TOKEN; max_charge is the run's USD cap.
    """
    scalar = isinstance(source_url, str)
    if not scalar and not isinstance(source_url, list):
        raise TypeError('source_url must be a string or a list of strings')
    links = [source_url] if scalar else list(source_url)
    if not all(isinstance(link, str) for link in links):
        raise TypeError('source_url must contain only strings')
    if len(links) > 5000:
        raise ValueError('At most 5000 links per call; split larger lists explicitly')
    if timeout is not None and timeout <= 0:
        raise ValueError('timeout must be positive or None')
    if max_charge <= 0:
        raise ValueError('max_charge must be positive')
    if not links:
        return []
    results = [_error('not_google_news_link') for _ in links]
    valid = [link for link in links if _key(link)[0] == 'article']
    if valid:
        token = apify_token or os.environ.get('APIFY_TOKEN')
        if not token:
            raise ValueError('Set APIFY_TOKEN or pass apify_token (your own Apify account)')

        def api(url, token, payload=None):
            headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token,
                       'User-Agent': 'hiver-gnewsdecoder/0.1'}
            body = json.dumps(payload).encode() if payload is not None else None
            with urlopen(Request(url, data=body, headers=headers), timeout=timeout) as response:
                return json.loads(response.read())

        try:
            rows = decode_links.decode(valid, token, len(valid), max_charge, api=api)
            by_key = {_key(row['inputUrl']): _result(row) for row in rows if row.get('inputUrl')}
            for index, link in enumerate(links):
                if _key(link) in by_key:
                    results[index] = dict(by_key[_key(link)])
                elif _key(link)[0] == 'article':
                    results[index] = _error('No decoded URL returned by Actor.')
        except HTTPError as exc:
            # Never echo response bodies or credentials, and never retry a failed POST.
            message = 'Apify HTTP ' + str(exc.code) + '; no automatic retry.'
            for index, link in enumerate(links):
                if _key(link)[0] == 'article':
                    results[index] = _error(message)
        except (URLError, TimeoutError, RuntimeError, OSError, ValueError, KeyError, TypeError):
            for index, link in enumerate(links):
                if _key(link)[0] == 'article':
                    results[index] = _error('Apify request/run failed; check Apify Console before resubmitting (no automatic retry).')
    return results[0] if scalar else results


async def gnews_decoder_async(source_url, interval=None, proxy=None, timeout=10.0, concurrency=8,
                              *, apify_token=None, max_charge=0.50):
    """Same results via asyncio.to_thread; concurrency is accepted but ignored.

    Cancelling the await does not abort an already-started Apify run.
    """
    return await asyncio.to_thread(gnewsdecoder, source_url, interval, proxy, timeout,
                                   apify_token=apify_token, max_charge=max_charge)
