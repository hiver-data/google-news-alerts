# Google News link decoder (Python, standard library only)

Turn `news.google.com/rss/articles/...` links into the publisher's own article URL, as a CSV. It calls
[hiver's Google News Scraper](https://apify.com/hiver/google-news-scraper?utm_source=github&utm_campaign=google-news-decoder)
on Apify with your own Apify token. The script is free; the Actor charges $0.80 per 1,000 decoded links
on Apify's free plan (less on paid plans). Links it cannot decode come back as rows with the reason and
are not charged.

Why not decode locally? Libraries such as `googlenewsdecoder` work, but each link needs requests to
Google, and after a burst Google answers HTTP 429. The Actor paces its requests and returns links Google
refused as free `not_decoded: Google rate-limited, retry later` rows instead of failing the run, so the
script never dies halfway through a batch; re-send those links later. It does not rotate IPs or otherwise
get around a refusal.

## `googlenewsdecoder` fails at import?

Since selectolax 1.0.0 (released 2026-10-03) removed its old Modest backend, a fresh
`pip install googlenewsdecoder` (0.2.1, which asks for `selectolax>=0.4.12` with no upper bound) stops at
`from googlenewsdecoder import gnewsdecoder` with `ImportError: Modest backend is deprecated since
selectolax 1.0`. The free fix is to keep selectolax below 1.0:

```sh
pip install googlenewsdecoder "selectolax<1.0"
```

We checked it on 2026-10-14 in a clean Python 3.13 virtualenv: with selectolax 1.0.0 the import fails;
with 0.4.13 it imports and decodes a Google News link. This script does not need that library at all:
it uses only Python's standard library and sends the links to the Actor.

## Use

```sh
export APIFY_TOKEN=...                  # Apify Console > Settings > API & Integrations
python decode_links.py links.txt        # one link per line; blank lines and # comments are ignored
python decode_links.py --feed "https://news.google.com/rss/search?q=federal+reserve&hl=en-US&gl=US&ceid=US:en"
```

Accepted: `/rss/articles/<id>`, `/articles/<id>` and `/read/<id>` links (with or without `?oc=5`), and
Google News RSS feed URLs (`/rss/search`, `/rss/topics`, `/rss/headlines`), whose items are decoded in
feed order. A link given twice is returned once. `--max-items` caps the rows (default: the number of links, at least 100, at most 5,000; error rows
count) and
`--max-charge` stops the run at a dollar amount (default $0.50).

Output `decoded_links.csv`:

| column | |
|---|---|
| `inputUrl` | the link you gave (or the feed item's link) |
| `url` | the publisher's article URL; empty when not decoded |
| `publisherDomain` | e.g. `cnbc.com` |
| `status` | `decoded`, `not_google_news_link` or `not_decoded: <reason>` |
| `title`, `publisher`, `publishedAt` | filled for feed items; empty for pasted links |

## What a run returns

The board's test run of the Actor (build 0.1.16, 2026-10-12, run `OWYXDZh9msX4hICmK`) got 8 links:
5 Google News article links, the first one again without `?oc=5`, a made-up id and a non-Google URL.
It returned 7 rows in 15 seconds and Apify charged 5 `decoded-link` events:

| `inputUrl` (shortened) | `url` | `status` |
|---|---|---|
| news.google.com/rss/articles/CBMiowFB...?oc=5 | https://www.scotusblog.com/2026/10/trump-announces-committee-investigation-of-fed-governor-lisa-cook/ | `decoded` |
| news.google.com/rss/articles/CBMipwFB...?oc=5 | https://www.aljazeera.com/economy/2026/10/9/trump-launches-probe-into-federal-reserve-governor-lisa-cook | `decoded` |
| news.google.com/articles/CBMiiAFB... | https://www.nbcnews.com/business/economy/trump-probe-fed-gov-lisa-cook-rcna602500 | `decoded` |
| news.google.com/rss/articles/CBMib0FV...?oc=5 | https://www.cnbc.com/2026/10/09/american-debt-delinquency.html | `decoded` |
| news.google.com/rss/articles/CBMi1wFB...?oc=5 | https://www.deutschlandfunk.de/bundesbank-vorstandsmitglied-theurer-sieht-noch-keine-systemische-staatsschuldenkrise-aber-warnsigna-100.html | `decoded` |
| news.google.com/rss/articles/CBMiNOTAREALID... | (empty) | `not_decoded: Google did not return a publisher URL` |
| example.com/news/story | (empty) | `not_google_news_link` |

Google decides how fast it answers: in the board's run of the previous build 25 minutes earlier, with the
same 8 links, Google refused that worker from the start, so all 7 rows came back free and undecoded and the
run still succeeded. That is what the free rows are for.

## Tests

```sh
python -m unittest discover -s tests -v
```

The fixtures are real rows from two runs of the Actor's code made on 2026-10-11 (one RSS search feed,
12 links decoded; and five pasted links: two decoded, one duplicate collapsed, one bad id and one
non-Google URL as free rows). The tests use a fake Apify API built from those rows; they never call
Apify or Google.

Unofficial. Not affiliated with Google.
