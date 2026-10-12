#!/usr/bin/env python3
"""Google News links in, publisher article URLs out (CSV).

Decodes news.google.com/rss/articles/..., /articles/... and /read/... links, or every item of a
Google News RSS feed URL, with hiver's Google News Scraper on Apify (your own Apify account).
The Actor paces its requests to Google; a link Google refuses comes back as a free
`not_decoded: Google rate-limited, retry later` row instead of failing the run, so you can re-send it later.

    export APIFY_TOKEN=...            # Apify Console > Settings > API & Integrations
    python decode_links.py links.txt  # one link per line; blank lines and # comments ignored
    python decode_links.py --feed "https://news.google.com/rss/search?q=federal+reserve&hl=en-US&gl=US&ceid=US:en"

Writes decoded_links.csv (inputUrl, url, publisherDomain, status, title, publisher, publishedAt).
Standard library only.
"""
import argparse
import csv
import json
import os
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API = "https://api.apify.com/v2"
ACTOR = "hiver~google-news-scraper"
PAGE = 1000  # dataset rows per request
COLUMNS = ["inputUrl", "url", "publisherDomain", "status", "title", "publisher", "publishedAt"]


def request_json(url, token, payload=None):
    headers = {"Content-Type": "application/json", "User-Agent": "hiver-google-news-decoder/1",
               "Authorization": "Bearer " + token}
    body = json.dumps(payload).encode() if payload is not None else None
    with urlopen(Request(url, data=body, headers=headers), timeout=35) as response:
        return json.loads(response.read())


def read_links(lines):
    """One link per line; strips whitespace, skips blanks and # comments, keeps order."""
    return [s.strip() for s in lines if s.strip() and not s.strip().startswith("#")]


def default_max_items(links, requested=None):
    """Row cap for the run: what the user asked for, else every pasted link (feeds add rows, so at least 100)."""
    if requested is not None:
        return max(1, min(5000, requested))
    return min(5000, max(100, len(links)))


def run_url(n_links, max_charge):
    # Room for paced decoding (about 1-5 s per link) plus start-up, capped at one hour.
    timeout = min(3600, 120 + 5 * n_links)
    query = {"timeout": timeout, "memory": 512, "maxTotalChargeUsd": max_charge}
    return API + "/acts/" + ACTOR + "/runs?" + urlencode(query), timeout


def decode(links, token, max_items, max_charge, api=request_json, clock=time.monotonic, pause=time.sleep):
    url, timeout = run_url(max(len(links), max_items), max_charge)
    run = api(url, token, {"articleUrls": links, "maxItems": max_items})["data"]
    deadline = clock() + timeout + 60
    while run["status"] not in {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}:
        if clock() >= deadline:
            raise RuntimeError("polling deadline exceeded; check run " + run["id"] + " in Apify Console")
        pause(5)
        run = api(API + "/actor-runs/" + run["id"], token)["data"]
    if run["status"] != "SUCCEEDED":
        raise RuntimeError("Actor run " + run["id"] + " ended " + run["status"] + ": " + str(run.get("statusMessage")))
    rows, offset = [], 0
    while True:
        page = api(API + "/datasets/" + run["defaultDatasetId"] + "/items?format=json&clean=1&limit=" + str(PAGE) + "&offset=" + str(offset), token)
        rows.extend(page)
        if len(page) < PAGE:
            return rows
        offset += len(page)


def write_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    decoded = sum(1 for r in rows if r.get("status") == "decoded")
    return decoded, len(rows) - decoded


def main():
    p = argparse.ArgumentParser(description="Decode Google News links to publisher article URLs (CSV).")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("links_file", nargs="?", help="text file, one Google News link per line ('-' = stdin)")
    src.add_argument("--feed", action="append", help="a Google News RSS feed URL (repeatable); every item is decoded")
    p.add_argument("--max-items", type=int, default=None,
                   help="rows to return at most (default: the number of links, at least 100; at most 5000)")
    p.add_argument("--max-charge", type=float, default=0.50, help="stop the run once it has cost this many USD (default 0.50)")
    p.add_argument("--out", default="decoded_links.csv")
    args = p.parse_args()
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        sys.exit("Set APIFY_TOKEN (Apify Console > Settings > API & Integrations).")
    if args.feed:
        links = args.feed
    else:
        handle = sys.stdin if args.links_file == "-" else open(args.links_file, encoding="utf-8")
        with handle:
            links = read_links(handle)
    if not links:
        sys.exit("No links given.")
    rows = decode(links, token, default_max_items(links, args.max_items), args.max_charge)
    decoded, free = write_csv(rows, args.out)
    print(f"{decoded} decoded, {free} not decoded (free rows) -> {args.out}")


if __name__ == "__main__":
    main()
