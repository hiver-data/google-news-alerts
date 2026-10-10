#!/usr/bin/env python3
"""Daily news alerts with durable local dedupe and optional webhook delivery."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import yaml

API = "https://api.apify.com/v2"
TOPICS = {"TOP", "WORLD", "NATION", "BUSINESS", "TECHNOLOGY", "ENTERTAINMENT", "SPORTS", "SCIENCE", "HEALTH"}


def actor_input(config):
    queries = config.get("queries", [])
    topics = config.get("topics", [])
    if not isinstance(queries, list) or any(not isinstance(q, str) or not q.strip() for q in queries):
        raise ValueError("queries must be a list of nonempty strings")
    if not isinstance(topics, list) or any(t not in TOPICS for t in topics):
        raise ValueError("topics must contain supported Google News sections")
    if not queries and not topics:
        raise ValueError("provide queries or topics")
    country, language = config.get("country", "US"), config.get("language", "en")
    if not isinstance(country, str) or not re.fullmatch(r"[A-Z]{2}", country):
        raise ValueError("country must be two uppercase letters")
    if not isinstance(language, str) or not re.fullmatch(r"[a-z]{2}", language):
        raise ValueError("language must be two lowercase letters")
    window = config.get("timeWindow", "day")
    if window not in {"hour", "day", "week", "month", "quarter", "year"}:
        raise ValueError("invalid timeWindow")
    monitor = config.get("monitorId", "google-news-alerts")
    if not isinstance(monitor, str) or not re.fullmatch(r"[a-zA-Z0-9-]{1,60}", monitor):
        raise ValueError("monitorId must be 1-60 letters, digits or dashes")
    maximum = config.get("maxItems", 20)
    if type(maximum) is not int or not 1 <= maximum <= 100:
        raise ValueError("maxItems must be an integer from 1 to 100")
    only_new = config.get("onlyNew", True)
    if type(only_new) is not bool:
        raise ValueError("onlyNew must be a boolean")
    return dict(queries=queries, topics=topics, country=country, language=language,
                timeWindow=window, monitorId=monitor, onlyNew=only_new, maxItems=maximum,
                maxItemsPerQuery=maximum, resolvePublisherUrl=True, addSnippets=True)


def request_json(url, token=None, payload=None, method=None):
    headers = {"Content-Type": "application/json", "User-Agent": "hiver-google-news-alerts/1"}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = json.dumps(payload).encode() if payload is not None else None
    with urlopen(Request(url, data=body, headers=headers, method=method), timeout=35) as response:
        raw = response.read()
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError:
        if token:  # Apify responses must be JSON; Slack webhooks return plain "ok".
            raise
        return raw.decode("utf-8")


def fetch_articles(inputs, token, api=request_json, clock=time.monotonic, pause=time.sleep):
    run = api(API + "/acts/hiver~google-news-scraper/runs?timeout=180&memory=1024", token, inputs)["data"]
    deadline = clock() + 240
    while run["status"] not in {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}:
        if clock() >= deadline:
            raise RuntimeError("Actor polling deadline exceeded; inspect the run in Apify Console")
        pause(5)
        run = api(API + "/actor-runs/" + run["id"], token)["data"]
    if run["status"] != "SUCCEEDED":
        raise RuntimeError("Actor ended with " + run["status"])
    rows, offset = [], 0
    while True:
        page = api(API + "/datasets/" + run["defaultDatasetId"] + "/items?format=json&limit=1000&offset=" + str(offset), token)
        if not isinstance(page, list):
            raise ValueError("dataset response must be a list")
        rows.extend(page)
        if len(page) < 1000:
            return rows
        offset += len(page)


def identity(row):
    # Match Actor's title+publisher identity as a stable backstop if URL decoding changes.
    title = re.sub(r"\W+", " ", row.get("title", "").lower()).strip()
    publisher = re.sub(r"\W+", " ", row.get("publisher", "").lower()).strip()
    return hashlib.sha256((title + "|" + publisher).encode()).hexdigest()


def valid_article(row):
    return (isinstance(row, dict) and not row.get("error") and row.get("success") is not False
            and isinstance(row.get("title"), str) and bool(row["title"].strip())
            and isinstance(row.get("publisher"), str)
            and isinstance(row.get("url"), str) and urlsplit(row["url"]).scheme in {"https", "http"}
            and bool(urlsplit(row["url"]).hostname))


def fresh_articles(rows, seen):
    keys = set(seen)
    result = []
    for row in rows:
        if valid_article(row) and identity(row) not in keys:
            keys.add(identity(row))
            result.append(row)
    return result


def escape(text):
    return re.sub(r"([\\`*_[\]<>])", r"\\\1", str(text)).replace("\n", " ").replace("\r", " ")


def digest(rows):
    lines = ["# Google News digest", ""]
    for row in rows:
        # url is the article URL, not publisherUrl (the publisher homepage).
        url = row["url"].replace("(", "%28").replace(")", "%29").replace(" ", "%20")
        note = "" if row.get("urlResolved") else " — unresolved Google/feed link"
        lines.append("- [" + escape(row["title"]) + "](" + url + ") — " + escape(row.get("publisher", "")) + " · " + escape(row.get("publishedAt", "")) + note)
    if not rows:
        lines.append("No new articles.")
    return "\n".join(lines) + "\n"


def save_state(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def deliver(rows, output, webhook=None, kind="slack", api=request_json):
    text = digest(rows)
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_text(text, encoding="utf-8")
    if webhook and rows:
        if urlsplit(webhook).scheme != "https":
            raise ValueError("webhook must use HTTPS")
        # One article per request: avoid Slack/Discord message-length limits.
        for row in rows:
            text = digest([row])
            # Truncate titles rather than URLs for unusually large article titles.
            if len(text) > 1900:
                short = dict(row, title=row["title"][:200])
                text = digest([short])
            if len(text) > 1900:
                raise ValueError("article URL exceeds webhook message budget; use digest mode")
            payload = {"content": text, "allowed_mentions": {"parse": []}} if kind == "discord" else {"text": text, "unfurl_links": False, "unfurl_media": False}
            api(webhook, payload=payload)


def run(config, state_path, output, token, webhook=None, kind="slack", fetch=fetch_articles, send=deliver):
    inputs = actor_input(config)
    state_path = Path(state_path)
    state = json.loads(state_path.read_text()) if state_path.exists() else {"seen": [], "pending": []}
    if not isinstance(state, dict) or not isinstance(state.get("seen"), list) or not isinstance(state.get("pending"), list):
        raise ValueError("invalid state; do not silently discard dedupe history")
    pending = state["pending"]
    if not pending:
        pending = fresh_articles(fetch(inputs, token), state["seen"])
        state["pending"] = pending
        save_state(state_path, state)  # persisted before notification
    send(pending, output, webhook, kind)
    state["seen"] = sorted(set(state["seen"]) | {identity(r) for r in pending})
    state["pending"] = []
    save_state(state_path, state)
    return len(pending)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="news.yml")
    parser.add_argument("--state", default="state/seen.json")
    parser.add_argument("--output", default="digest.md")
    parser.add_argument("--webhook-kind", choices=["slack", "discord"], default="slack")
    args = parser.parse_args()
    token = os.environ.get("APIFY_TOKEN")
    if not token:
        parser.error("set APIFY_TOKEN in your environment or repository secrets")
    try:
        config = yaml.safe_load(Path(args.config).read_text())
        if not isinstance(config, dict):
            raise ValueError("YAML must be a mapping")
        count = run(config, args.state, args.output, token, os.environ.get("NEWS_WEBHOOK"), args.webhook_kind)
    except Exception as error:
        # Never print URLs/errors that could contain webhook or API secrets.
        raise SystemExit("News alerts failed (" + type(error).__name__ + "). State retained; inspect configuration/run in Apify Console.") from None
    print(str(count) + " new articles; digest written to " + args.output)


if __name__ == "__main__":
    main()
