#!/usr/bin/env python3
"""
Google Alerts Daily Digest - Fetch Script
Runs daily via GitHub Actions, outputs JSON to docs/data/YYYY-MM-DD.json
"""

import sys
import json
import hashlib
import re
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import feedparser
import requests
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
REPO_ROOT = Path(__file__).parent.parent


def load_config() -> dict:
    with open(REPO_ROOT / "config.yml") as f:
        return yaml.safe_load(f)


def load_feeds(feeds_file: Path) -> list[dict]:
    if not feeds_file.exists():
        log.warning(f"feeds.json not found at {feeds_file}, no feeds to process")
        return []
    with open(feeds_file) as f:
        data = json.load(f)
    return data.get("feeds", [])


# ── State management ──────────────────────────────────────────────────────────

def article_id(url: str) -> str:
    return hashlib.md5(url.strip().encode()).hexdigest()


def load_processed_ids(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        log.warning("Could not read processed_ids file, starting fresh")
        return {}


def save_processed_ids(path: Path, processed: dict) -> None:
    cutoff = datetime.now(JST) - timedelta(days=30)
    pruned = {
        aid: ts for aid, ts in processed.items()
        if datetime.fromisoformat(ts) >= cutoff
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(pruned, f, ensure_ascii=False)
    log.info(f"Saved {len(pruned)} processed IDs (pruned {len(processed) - len(pruned)} old)")


# ── RSS fetching ───────────────────────────────────────────────────────────────

def parse_entry_date(entry) -> Optional[datetime]:
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def fetch_feed(feed_cfg: dict, lookback_hours: int, max_articles: int,
               processed_ids: dict) -> list[dict]:
    url = feed_cfg["url"]
    keyword = feed_cfg.get("keyword", "")
    category = feed_cfg.get("category", "その他")
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)

    log.info(f"Fetching feed: {keyword} ({url[:60]}...)")
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except Exception as e:
        log.error(f"Failed to fetch {url}: {e}")
        return []

    if parsed.bozo and not parsed.entries:
        log.warning(f"Feed parse error for {keyword}: {parsed.bozo_exception}")
        return []

    articles = []
    for entry in parsed.entries:
        pub_date = parse_entry_date(entry)
        if pub_date and pub_date < cutoff:
            continue

        link = entry.get("link", "")
        if not link:
            continue

        aid = article_id(link)
        if aid in processed_ids:
            continue

        raw_title = entry.get("title", "")
        title = re.sub(r"<[^>]+>", " ", raw_title).strip()

        raw_snippet = entry.get("summary", entry.get("title", ""))
        snippet = re.sub(r"<[^>]+>", " ", raw_snippet).strip()[:500]

        articles.append({
            "id": aid,
            "title": title,
            "url": link,
            "source": entry.get("source", {}).get("title", parsed.feed.get("title", "")),
            "published_iso": pub_date.astimezone(JST).isoformat() if pub_date else None,
            "snippet": snippet,
            "keyword": keyword,
            "category": category,
        })

        if len(articles) >= max_articles:
            break

    log.info(f"  -> {len(articles)} new articles from {keyword}")
    return articles


# ── Output ─────────────────────────────────────────────────────────────────────

def load_or_create_daily_file(path: Path) -> dict:
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return {
        "date": path.stem,
        "generated_at": datetime.now(JST).isoformat(),
        "article_count": 0,
        "articles": [],
    }


def save_daily_file(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data["article_count"] = len(data["articles"])
    data["generated_at"] = datetime.now(JST).isoformat()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log.info(f"Wrote {data['article_count']} articles to {path}")


def write_date_index(data_dir: Path) -> None:
    dates = sorted(
        [p.stem for p in data_dir.glob("????-??-??.json")],
        reverse=True,
    )
    index_path = data_dir / "index.json"
    with open(index_path, "w") as f:
        json.dump({"dates": dates, "updated_at": datetime.now(JST).isoformat()}, f)
    log.info(f"Updated date index: {len(dates)} dates available")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    config = load_config()
    settings = config["settings"]

    feeds_path = REPO_ROOT / settings["feeds_file"]
    feeds = load_feeds(feeds_path)
    if not feeds:
        log.info("No feeds configured. Add feeds via the admin page and re-run.")
        sys.exit(0)

    processed_ids_path = REPO_ROOT / settings["processed_ids_file"]
    processed_ids = load_processed_ids(processed_ids_path)
    log.info(f"Loaded {len(processed_ids)} previously processed article IDs")

    today_str = datetime.now(JST).strftime("%Y-%m-%d")
    output_path = REPO_ROOT / settings["output_dir"] / f"{today_str}.json"
    daily_data = load_or_create_daily_file(output_path)

    existing_ids = {a["id"] for a in daily_data["articles"]}
    new_processed = {}
    total_new = 0

    for feed_cfg in feeds:
        articles = fetch_feed(
            feed_cfg,
            lookback_hours=settings["lookback_hours"],
            max_articles=settings["max_articles_per_feed"],
            processed_ids={**processed_ids, **new_processed},
        )

        for article in articles:
            if article["id"] in existing_ids:
                continue

            daily_data["articles"].append(article)
            existing_ids.add(article["id"])
            new_processed[article["id"]] = datetime.now(JST).isoformat()
            total_new += 1

            log.info(f"Fetched: {article['title'][:60]}...")

    save_daily_file(output_path, daily_data)

    processed_ids.update(new_processed)
    save_processed_ids(processed_ids_path, processed_ids)

    log.info(f"Done. {total_new} new articles fetched.")

    write_date_index(REPO_ROOT / settings["output_dir"])


if __name__ == "__main__":
    main()
