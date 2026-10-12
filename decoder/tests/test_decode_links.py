import copy
import csv
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

import decode_links

FIXTURE = json.loads((Path(__file__).parent / "fixtures/local-run-rows.json").read_text())
LINK_ROWS = FIXTURE["links_run"]["rows"]
FEED_ROWS = FIXTURE["feed_run"]["rows"]


class FakeApi:
    """Answers the three Apify calls the script makes, from real rows; records every call."""

    def __init__(self, rows, statuses=("RUNNING", "SUCCEEDED"), page_size=1000):
        self.rows, self.statuses, self.page_size, self.calls = rows, list(statuses), page_size, []

    def __call__(self, url, token, payload=None):
        self.calls.append((url, token, copy.deepcopy(payload)))
        path = urlsplit(url).path
        if path.endswith("/runs"):
            return {"data": {"id": "run1", "status": self.statuses.pop(0), "defaultDatasetId": "ds1"}}
        if "/actor-runs/" in path:
            return {"data": {"id": "run1", "status": self.statuses.pop(0), "defaultDatasetId": "ds1",
                             "statusMessage": "test"}}
        if "/datasets/ds1/items" in path:
            offset = int(parse_qs(urlsplit(url).query)["offset"][0])
            return copy.deepcopy(self.rows[offset:offset + self.page_size])
        raise AssertionError("unexpected call " + url)


def run(api, links, max_items=20):
    return decode_links.decode(links, "offline-token", max_items, 0.5, api=api, clock=lambda: 0, pause=lambda s: None)


class DecodeLinksTests(unittest.TestCase):
    def test_sends_links_as_articleUrls_with_cap_memory_and_spend_limit(self):
        api = FakeApi(LINK_ROWS)
        links = FIXTURE["links_run"]["input"]["articleUrls"]
        run(api, links)
        url, token, payload = api.calls[0]
        self.assertEqual(urlsplit(url).path, "/v2/acts/hiver~google-news-scraper/runs")
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["memory"], ["512"])
        self.assertEqual(query["maxTotalChargeUsd"], ["0.5"])
        self.assertEqual(payload, {"articleUrls": links, "maxItems": 20})
        self.assertEqual(token, "offline-token")

    def test_rows_keep_input_order_and_free_error_rows(self):
        rows = run(FakeApi(LINK_ROWS), FIXTURE["links_run"]["input"]["articleUrls"])
        self.assertEqual([r["status"] for r in rows],
                         ["decoded", "decoded", "not_decoded: Google did not return a publisher URL",
                          "not_google_news_link"])
        self.assertTrue(rows[0]["url"].startswith("https://www.scotusblog.com/"))
        self.assertIsNone(rows[3]["url"])

    def test_csv_columns_and_counts(self):
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as tmp:
            out = Path(tmp) / "out.csv"
            decoded, free = decode_links.write_csv(LINK_ROWS, out)
            self.assertEqual((decoded, free), (2, 2))
            table = list(csv.DictReader(io.StringIO(out.read_text(encoding="utf-8"))))
        self.assertEqual(list(table[0]), decode_links.COLUMNS)
        self.assertEqual(table[1]["publisherDomain"], "cnbc.com")

    def test_feed_rows_carry_title_publisher_date(self):
        rows = run(FakeApi(FEED_ROWS), ["https://news.google.com/rss/search?q=federal%20reserve&hl=en-US&gl=US&ceid=US:en"], 12)
        self.assertEqual(len(rows), 12)
        self.assertTrue(all(r["status"] == "decoded" and r["title"] and r["publishedAt"] for r in rows))

    def test_pages_through_large_datasets(self):
        api = FakeApi(FEED_ROWS, page_size=5)
        original = decode_links.PAGE
        decode_links.PAGE = 5
        try:
            rows = run(api, ["https://news.google.com/rss/search?q=x"], 12)
        finally:
            decode_links.PAGE = original
        self.assertEqual(rows, FEED_ROWS)
        offsets = [parse_qs(urlsplit(c[0]).query)["offset"][0] for c in api.calls if "/items" in c[0]]
        self.assertEqual(offsets, ["0", "5", "10"])

    def test_failed_run_raises_with_status(self):
        with self.assertRaisesRegex(RuntimeError, "FAILED"):
            run(FakeApi(LINK_ROWS, statuses=("RUNNING", "FAILED")), ["https://news.google.com/rss/articles/abc"])

    def test_polling_deadline(self):
        api = FakeApi(LINK_ROWS, statuses=["RUNNING"] * 10)
        ticks = iter(range(0, 100000, 1000))
        with self.assertRaisesRegex(RuntimeError, "deadline"):
            decode_links.decode(["https://news.google.com/rss/articles/abc"], "t", 1, 0.5, api=api,
                                clock=lambda: next(ticks), pause=lambda s: None)

    def test_default_row_cap_covers_every_pasted_link(self):
        self.assertEqual(decode_links.default_max_items(["l"] * 3), 100)
        self.assertEqual(decode_links.default_max_items(["l"] * 750), 750)
        self.assertEqual(decode_links.default_max_items(["l"] * 9000), 5000)
        self.assertEqual(decode_links.default_max_items(["l"] * 750, 20), 20)

    def test_read_links_skips_blanks_and_comments(self):
        self.assertEqual(decode_links.read_links(["# my links\n", "\n", "  https://news.google.com/rss/articles/a  \n"]),
                         ["https://news.google.com/rss/articles/a"])


if __name__ == "__main__":
    unittest.main()
