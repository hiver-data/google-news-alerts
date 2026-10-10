# Google News alerts → Slack, Discord or a Markdown digest

A free Python example for daily Google News searches and topic monitoring. It calls [hiver's Google News Scraper](https://apify.com/hiver/google-news-scraper?utm_source=github&utm_campaign=google-news-alerts) using your own Apify account, keeps article identities across runs, and sends only new articles. The code is free; Actor runs and any GitHub Actions usage beyond your allowance are not.

## Quick start

Use a copy of this repository you control. Python 3.12+:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -v
```

Edit `news.yml`: `queries` accepts Google News search strings; `topics` accepts TOP, WORLD, NATION, BUSINESS, TECHNOLOGY, ENTERTAINMENT, SPORTS, SCIENCE or HEALTH. Set a two-letter uppercase `country`, lowercase `language`, and `timeWindow` (hour, day, week, month, quarter or year). `maxItems` defaults to 20 and this example limits it to 100 to keep daily runs small. Set a distinct `monitorId` per watchlist/repository.

Set `APIFY_TOKEN` in your local shell through your usual secret manager (never commit it). Then:

```sh
python alerts.py
```

The first successful run sends all returned articles and establishes a baseline, not a silent warm-up. Without a webhook, it writes `digest.md`. With `NEWS_WEBHOOK` set to a Slack incoming webhook, the same command also posts each new article. For Discord:

```sh
python alerts.py --webhook-kind discord
```

Alerts link to each row's `url` (the article), not `publisherUrl` (the homepage). When decoding fails, the returned feed link is kept and explicitly labelled unresolved. This example does not fetch article pages itself or bypass source refusals.

## Install the daily GitHub Actions workflow

The publisher App cannot create active workflow files, so this repo ships a template you activate in your own repository:

```sh
mkdir -p .github/workflows
cp workflow-template/google-news-alerts.yml .github/workflows/google-news-alerts.yml
```

Commit the copied file. In Settings → Secrets and variables → Actions, create `APIFY_TOKEN` from your own Apify account and, optionally, `NEWS_WEBHOOK`. Change the command to `--webhook-kind discord` for Discord. Allow Actions read/write repository contents; protected branches may require a separate unprotected automation branch. The template runs daily at 07:17 UTC or manually via Actions → Google News alerts → Run workflow. GitHub schedules can be delayed and schedules on inactive public repositories may be disabled.

It commits `state/seen.json` and `digest.md` back to the repository, even on delivery failure so pending articles survive. Keep concurrency serialization enabled and do not manually run another process against the same state/monitor simultaneously. Public repositories expose queries, digest and saved pending article rows; use a private copy for sensitive watchlists. Tokens and webhook URLs stay in repository secrets. Treat a failed state commit/push as a failed run: repair branch permissions before continuing. A digest artifact is also uploaded. No workflow is active in this upstream repo.

## Dedupe and failure semantics

The Actor source already implements cross-run `onlyNew`/`monitorId`. It opens a named Apify key-value store `google-news-scraper-<monitorId>`, loads `seen`, skips existing title/publisher identities, marks emitted rows seen and retains that history for 120 days. The monitor is scoped to your Apify account. The template enables it, and keeps local SHA-256 title/publisher identities as a backstop if an article URL changes or a dataset repeats. The local file is not pruned; distinct stories with identical title/publisher can be suppressed, matching the Actor's identity limitation.

The script saves a pending batch before notification. If delivery fails, the next run replays pending rows without starting another Actor. It marks them locally seen only after successful delivery. Webhooks cannot guarantee exactly-once delivery: if a service accepts a message but the connection fails, or a multi-article batch fails partway through, replay may duplicate messages. Tests prove normal cross-run dedupe, not exactly-once delivery across crashes.

There is one upstream gap: `onlyNew` marks rows seen when the Actor emits them, before this script downloads them. If download fails before the pending file is written, recover that run's dataset from Apify Console. For local-state-only retry semantics, set `onlyNew: false`; the Actor may return previously seen articles (and charge for them), but the local backstop still prevents ordinary repeated notifications. Keep overlapping windows (e.g. week for a daily run) if delayed runs matter. Clearing only the local file does not reset the upstream monitor; changing `monitorId` starts a fresh upstream baseline. Neither mode guarantees alerts for all news published during an outage or beyond item limits.

## What was measured (not a permanent accuracy claim)

Board bench dated **2026-10-10 05:24:02 UTC**, Actor build **0.1.11**, two US/English and DE/German cases:

| Metric | hiver | automation-lab/google-news-scraper | data_xplorer/google-news-scraper-fast |
| --- | ---: | ---: | ---: |
| Hand-checked facts matched | 14/14 | 4/14 | 4/14 |
| Six-field completeness | 86% | 83% | 67% |
| Store user price per 1,000 output articles in these runs | $1.63 | $2.67 | $4.00 |

These are dated small-case results, not universal coverage, reliability or current-price guarantees. The accuracy edge depends on rivals' link decoding: in the earlier **2026-10-10 03:46 UTC** bench (hiver 0.1.9), data_xplorer decoded its links and matched **14/14**, while hiver matched **12/14**. Do not claim a fixed accuracy multiplier. Check the [Actor Store page](https://apify.com/hiver/google-news-scraper?utm_source=github&utm_campaign=google-news-alerts) for current pricing and limits. The alert code itself has not run a new paid bench.

## Offline tests and provenance

```sh
python -m unittest discover -s tests -v
```

Fixtures are the two actual sample rows from the board's 2026-10-10 05:24:02 UTC `RESULT.json` for build 0.1.11: Reuters and airliners.de. Their original titles, publisher URLs, article URLs and timestamps are preserved in `tests/fixtures/bench-samples.json`. Mocked HTTP responses use those rows; no test calls Apify, Slack, Discord or publisher sites. Tests cover duplicates within/across runs, resolved article URLs, URL decoding changes, no-new note rows, failed delivery replay, corrupt state, input validation, mocked Actor start/poll/dataset flow and a polling deadline.

## License

MIT. This is an hiver example for its own Actor, not an official Google API or a Google-affiliated project.
